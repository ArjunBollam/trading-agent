"""Code-run protection: the daily loss limit, stale orders, code exits and broker stops.

Runs on every watch tick (every 30 s by default) and again at the start of each
decision cycle, before the AI is asked anything. No LLM is involved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from .app import App
from .exits import exit_reason, pnl_pct, protective_stop_price
from .orders import ExecResult
from .performance import closed_on, closed_trades, loss_streak


@dataclass
class ProtectReport:
    flattened: bool = False
    exits: list[ExecResult] = field(default_factory=list)
    stops: list[ExecResult] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)

    @property
    def changed_positions(self) -> bool:
        return self.flattened or any(r.status in ("submitted", "dry_run") for r in self.exits)


def reconcile(app: App, options, recent):
    """Flag positions that paperbot didn't open (a manual trade in the same account, or a bug),
    once each. Keep managing them, or leave them alone, per risk.manage_external_positions."""
    ours = {o.symbol for o in recent if o.side == "buy" and o.filled_qty > 0 and o.client_order_id.startswith("pb-")}
    external = [p for p in options if p.symbol not in ours]
    for p in external:
        if p.symbol not in app.reconciled:
            app.reconciled.add(p.symbol)
            managed = app.cfg.risk.manage_external_positions
            app.journal.log("external_position", symbol=p.symbol, qty=p.qty, managed=managed)
            app.alerts.send("Position not opened by paperbot",
                            f"{p.symbol} x{p.qty} has no paperbot entry in the last 7 days. "
                            + ("paperbot will still manage it with stops and exits."
                               if managed else "paperbot will leave it alone (manage_external_positions: false)."),
                            "default", ["question"])
    if app.cfg.risk.manage_external_positions:
        return options
    return [p for p in options if p.symbol in ours]


def track_excursions(app: App, positions, quotes) -> None:
    """Remember each position's worst and best mark (MAE/MFE, as % of entry) and log them when it
    closes. Sweeney's method: winners' worst dips show where a stop belongs. In memory, so a
    restart begins tracking from the current mark."""
    held = {p.symbol: p for p in positions if p.option and p.qty > 0}
    for sym, p in held.items():
        move = pnl_pct(p, quotes.get(sym))
        entry, worst, best = app.excursions.get(sym, [p.avg_entry_price, move, move])
        app.excursions[sym] = [entry, min(worst, move), max(best, move)]
    for sym in [s for s in app.excursions if s not in held]:
        entry, worst, best = app.excursions.pop(sym)
        app.journal.log("excursion", symbol=sym, entry=entry, mae_pct=round(worst, 1), mfe_pct=round(best, 1))


def protect(app: App, slot: str) -> ProtectReport:
    cfg, broker = app.cfg, app.broker
    now = app.now().astimezone(cfg.schedule.tz)
    today = now.date()
    rep = ProtectReport()

    with app.lock:
        app.gate.ensure_session(broker, today)
        account = broker.account()
        positions = [p for p in broker.positions() if p.qty > 0]
        open_orders = [o for o in broker.open_orders() if o.is_open]
        recent = broker.orders_since(now - timedelta(days=7))

        # 1. Daily loss limit: sell everything and stop for the day.
        if app.gate.daily_loss_breached(account):
            if positions or open_orders:
                app.funnel.flatten_all("daily_loss")
                rep.flattened = True
                app.alerts.send(
                    "Daily loss limit hit",
                    f"Down ${app.gate.drawdown_usd(account):,.0f} (limit ${app.gate.daily_loss_limit_usd(account):,.0f}). "
                    "Sold everything. No new trades today.",
                    "urgent", ["rotating_light"],
                )
            if not app.killswitch.entries_halted(today):
                app.killswitch.halt("daily loss limit breached", scope="day", today=today)
            return rep

        # 1a. Soft loss limit: say so once; the gate halves per-trade risk from here on.
        if app.gate.soft_limit_hit(account) and not app.journal.events(today, "soft_loss_limit"):
            app.journal.log("soft_loss_limit", drawdown=app.gate.drawdown_usd(account))
            app.alerts.send("Soft loss limit", f"Down ${app.gate.drawdown_usd(account):,.0f} today. New trades now risk "
                            f"half as much until tomorrow; the hard limit is {cfg.risk.daily_loss_limit_pct:g}%.",
                            "high", ["warning"])

        # 1b. Losing streak: stop opening trades for the rest of the day.
        streak = loss_streak(closed_on(closed_trades(recent), today, cfg.schedule.tz))
        if streak >= cfg.risk.max_consecutive_losses and not app.killswitch.entries_halted(today):
            app.killswitch.halt(f"{streak} losing trades in a row", scope="day", today=today)
            app.alerts.send("Losing streak: paused for today",
                            f"{streak} losing trades in a row. No new trades until tomorrow; exits keep running.",
                            "high", ["pause_button"])

        # 2. Stale entries and orphaned sells.
        held = {p.symbol: p for p in positions}
        entry_ttl = cfg.orders.entry_ttl_minutes * 60
        for o in open_orders:
            age = (now - o.submitted_at).total_seconds() if o.submitted_at else 0
            if o.side == "buy" and age >= entry_ttl:
                app.funnel.cancel(o, f"entry not filled within {cfg.orders.entry_ttl_minutes} min")
                rep.cancelled.append(o.id)
            elif o.side == "sell" and o.symbol not in held:
                app.funnel.cancel(o, "position no longer held")
                rep.cancelled.append(o.id)

        # 3. Code exits: expiry, end of day, stop-loss, take-profit, time stop.
        options = reconcile(app, [p for p in positions if p.option], recent)
        quotes = broker.option_quotes([p.symbol for p in options]) if options else {}
        track_excursions(app, options, quotes)  # before exits, so the tick that triggers one is counted

        def opened_at(symbol: str):
            fills = [o.filled_at for o in recent if o.symbol == symbol and o.side == "buy" and o.filled_at]
            return max(fills) if fills else None

        exiting = set()
        for p in options:
            close_all = app.gate.effective(cfg.exits.close_all_at, now) if cfg.exits.close_all_at else None
            reason = exit_reason(p, quotes.get(p.symbol), today, cfg.exits, now=now, opened_at=opened_at(p.symbol),
                                 close_all_at=close_all)
            if reason is None and app.gate.earnings_exit_due(p.underlying, now):
                reason = "earnings"  # don't hold short-dated options through an earnings gap
            if reason:
                rep.exits.append(app.funnel.exit(p.symbol, None, reason, slot, discretionary=False))
                exiting.add(p.symbol)

        # 4. Keep one stop order at the broker per position, sized right, ratcheting up only.
        if cfg.exits.broker_stop and not app.funnel.stops_disabled:
            open_orders = [o for o in broker.open_orders() if o.is_open]
            managed = {p.symbol for p in options}
            for p in broker.positions():
                if not p.option or p.qty <= 0 or p.symbol in exiting or p.symbol not in managed:
                    continue
                sells = [o for o in open_orders if o.symbol == p.symbol and o.side == "sell"]
                if any(not o.is_stop for o in sells):
                    continue  # an exit is already working
                stops = [o for o in sells if o.is_stop]
                current = max((o.stop_price for o in stops if o.stop_price is not None), default=None)
                sized_right = len(stops) == 1 and stops[0].qty - stops[0].filled_qty == p.qty
                # A price-only move waits for a meaningful step; a size change is replaced at once.
                want = protective_stop_price(p, cfg.exits, quotes.get(p.symbol), current, min_step=sized_right)
                if sized_right and stops[0].stop_price is not None and abs(stops[0].stop_price - want) < 0.005:
                    continue
                rep.stops.append(app.funnel.place_stop(p, slot, price=want, replacing=stops))
    return rep
