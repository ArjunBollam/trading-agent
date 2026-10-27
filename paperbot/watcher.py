"""The fast loop: no LLM, runs every watch_interval_seconds.

Autobelay checked stops only every 10 minutes, inside the AI cycle. Here exits, the
loss limit and broker stops are checked every 30 seconds whether or not the AI is
busy, and a real stop order sits at the broker in between.
"""

from __future__ import annotations

from datetime import time, timedelta

from .app import App
from .exits import pnl_pct
from .features import compute, detect_signals, fetch_session_bars
from .performance import closed_on, closed_trades, one_line, stats, tokens_used
from .performance import ClosedTrade
from .protect import ProtectReport, protect


def status_text(app: App) -> str:
    acct = app.broker.account()
    sod = app.gate.start_of_day_equity(acct)
    positions = [p for p in app.broker.positions() if p.qty > 0]
    quotes = app.broker.option_quotes([p.symbol for p in positions if p.option])
    halted = app.killswitch.entries_halted(app.today())
    lines = [f"Equity ${acct.equity:,.0f} ({acct.equity - sod:+,.0f} today)."]
    lines.append(f"Halted: {halted}" if halted else "Running.")
    for p in positions:
        lines.append(f"{p.symbol} x{p.qty}: {pnl_pct(p, quotes.get(p.symbol)):+.1f}%")
    if not positions:
        lines.append("No positions.")
    return "\n".join(lines)


def handle_commands(app: App) -> None:
    for cmd in app.alerts.poll_commands():
        app.journal.log("phone_command", command=cmd)
        if cmd == "halt":
            app.killswitch.halt("halted from phone", scope="manual")
            app.alerts.send("Halted", "The AI won't open new trades. Exits and stops keep running. "
                            "Resume on the machine with: paperbot resume", "high")
        elif cmd == "flatten":
            app.funnel.flatten_all("phone")
            app.killswitch.halt("flattened from phone", scope="day", today=app.today())
            app.alerts.send("Flattened", "Closed every position and halted for the rest of today.", "high")
        elif cmd == "status":
            app.alerts.send("Status", status_text(app))
        else:
            app.alerts.send("Unknown command", f"{cmd!r}. Send halt, flatten or status.", "low")


def maybe_daily_summary(app: App) -> None:
    now = app.now().astimezone(app.cfg.schedule.tz)
    today = now.date()
    if now.time() < time(16, 0) or app.journal.events(today, "daily_summary"):
        return
    if not any(e.get("halt") != "market closed" for e in app.journal.events(today, "cycle_end")):
        return  # no cycle ran with the market open today (weekend, holiday, or started after the close)
    acct = app.broker.account()
    sod = app.gate.start_of_day_equity(acct)
    submitted = app.journal.events(today, "order_submitted")
    entries = sum(1 for e in submitted if e.get("kind") == "entry")
    exits = sum(1 for e in submitted if e.get("kind") == "exit")
    held = sum(1 for p in app.broker.positions() if p.qty > 0)
    trades = closed_on(closed_trades(app.broker.orders_since(now - timedelta(days=7))), today, app.cfg.schedule.tz)
    text = (f"Equity ${acct.equity:,.0f} ({acct.equity - sod:+,.0f} today). "
            f"{entries} entries, {exits} exits, {held} positions held overnight. Trades: {one_line(stats(trades))}. "
            f"AI tokens: {tokens_used(app.journal.events(today, 'llm_turn')):,}.")
    if now.weekday() == 4:  # Friday
        text += (" Weekend check: update next week's earnings and FOMC/CPI dates in config.yaml, "
                 "then run `paperbot check-config`.")
    app.journal.log("daily_summary", text=text)
    app.alerts.send("Daily summary", text, "low", ["bar_chart"])


def report_fills(app: App) -> None:
    """Push an alert when an order actually fills, with slippage against the mid at the time it
    was sent and, for exits, the trade's P&L. Fills from before startup are not replayed."""
    now = app.now()
    if app.last_fill_check is None:
        app.last_fill_check = now
        return
    if app.reported_fills is None:  # survive restarts: never re-alert a fill already in today's journal
        app.reported_fills = {e["order_id"] for e in app.journal.events(app.today(), "fill") if e.get("order_id")}
    orders = app.broker.orders_since(now - timedelta(days=7))
    # The broker can update an order after it fills (filled_at moves), so the time check alone could
    # report the same fill twice. The order id is the identity.
    new = sorted((o for o in orders if o.status == "filled" and o.filled_at and o.filled_at > app.last_fill_check
                  and o.filled_avg_price is not None and o.id not in app.reported_fills), key=lambda o: o.filled_at)
    if not new:
        return
    tz = app.cfg.schedule.tz
    days = {o.filled_at.astimezone(tz).date() for o in new} | {app.today()}
    sent = {e["client_order_id"]: e for d in days for e in app.journal.events(d, "order_submitted")}
    trades: dict[str, list[ClosedTrade]] = {}
    for t in closed_trades(orders):
        trades.setdefault(t.exit_client_order_id, []).append(t)
    for o in new:
        info = sent.get(o.client_order_id, {})
        mid = info.get("mid")
        slip = None
        if mid is not None:
            slip = round(o.filled_avg_price - mid if o.side == "buy" else mid - o.filled_avg_price, 4)
        pnl = sum(t.pnl for t in trades.get(o.client_order_id, [])) if o.side == "sell" else None
        kind = "stop" if o.client_order_id.startswith("pb-stop-") else info.get("kind", "other")
        app.reported_fills.add(o.id)
        app.journal.log("fill", symbol=o.symbol, side=o.side, qty=o.filled_qty, price=o.filled_avg_price,
                        client_order_id=o.client_order_id, order_id=o.id, kind=kind, mid_at_submit=mid,
                        slippage=slip, pnl=pnl)
        verb = "Bought" if o.side == "buy" else "Sold"
        text = f"{verb} {o.filled_qty} {o.symbol} @ {o.filled_avg_price:.2f}"
        if mid is not None:
            text += f" (mid was {mid:.2f})"
        if pnl is not None and trades.get(o.client_order_id):
            text += f". Trade P&L {pnl:+,.2f}"
        app.alerts.send(f"Filled ({kind})", text, "high" if pnl is not None and pnl < 0 else "default",
                        ["white_check_mark"])
    app.last_fill_check = max(o.filled_at for o in new)


def maybe_signals(app: App) -> None:
    """Real-time setup alerts. They're information only: nothing here places an order."""
    cfg = app.cfg
    now = app.now().astimezone(cfg.schedule.tz)
    if not cfg.alerts.signals or not app.gate.in_trading_window(now):
        return
    if app.last_signal_check and now - app.last_signal_check < timedelta(minutes=cfg.alerts.signal_interval_minutes):
        return
    app.last_signal_check = now
    seen = {(e.get("symbol"), e.get("kind")) for e in app.journal.events(now.date(), "signal")}
    stocks = app.broker.stock_snapshots(cfg.risk.universe)
    for sym in cfg.risk.universe:
        if sym not in stocks:
            continue
        f = compute(sym, fetch_session_bars(app.broker, sym, now, cfg.schedule.tz), stocks[sym].prev_close, now,
                    cfg.schedule.tz)
        if f is None:
            continue
        for sig in detect_signals(f, cfg.alerts.signal_first_half_hour_pct, now):
            if (sig.symbol, sig.kind) in seen:
                continue
            app.journal.log("signal", symbol=sig.symbol, kind=sig.kind, message=sig.message)
            app.alerts.send(f"Setup: {sig.symbol}", sig.message, "default", ["eyes"])
            if cfg.schedule.trigger_cycle_on_signal:
                app.cycle_requested = True  # don't wait for the next slot; the runner starts a cycle now


def watch_tick(app: App) -> ProtectReport | None:
    if app.killswitch.all_halted():
        return None
    handle_commands(app)
    if not app.broker.clock().is_open:
        maybe_daily_summary(app)
        return None
    rep = protect(app, slot=f"w{app.now():%Y%m%dT%H%M%S}")
    report_fills(app)  # after protection, so a data hiccup here can't delay exits
    maybe_signals(app)
    return rep
