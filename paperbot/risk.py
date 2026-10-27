"""Hard limits: the kill switch and the one risk check every order goes through.

The gate rejects; it never shrinks an order to make it fit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Literal

from .config import Config
from .models import Account, OptionSymbol, Order, OrderRequest, Position, Quote
from .performance import closed_on, closed_trades, loss_streak

Kind = Literal["entry", "exit", "stop"]

REGULAR_CLOSE = time(16, 0)


class KillSwitch:
    """File-based kill switch, read straight from disk on every tick so it works even if
    everything else is broken.

    HALT         The AI stops being consulted. Code exits and broker stops keep running.
    HALT_ALL     Everything stops, including exit management.
    HALT_<date>  Written by the daily loss limit or a phone "flatten". Expires on its own.
    """

    def __init__(self, log_dir: Path):
        self.dir = Path(log_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _read(self, name: str) -> str | None:
        path = self.dir / name
        if path.exists():
            return path.read_text().strip() or name
        return None

    def all_halted(self) -> str | None:
        return self._read("HALT_ALL")

    def entries_halted(self, today: date) -> str | None:
        return self._read("HALT_ALL") or self._read("HALT") or self._read(f"HALT_{today.isoformat()}")

    def halt(self, reason: str, scope: Literal["manual", "all", "day"], today: date | None = None) -> Path:
        name = {"manual": "HALT", "all": "HALT_ALL", "day": f"HALT_{(today or date.today()).isoformat()}"}[scope]
        path = self.dir / name
        path.write_text(reason + "\n")
        return path

    def resume(self) -> list[str]:
        """Clears HALT and HALT_ALL. A daily halt is deliberately left alone."""
        removed = []
        for name in ("HALT", "HALT_ALL"):
            path = self.dir / name
            if path.exists():
                path.unlink()
                removed.append(name)
        return removed


@dataclass(frozen=True)
class OrderContext:
    """Fresh broker state, fetched right before an order is checked."""

    now: datetime
    account: Account
    positions: list[Position]
    open_orders: list[Order]
    recent_orders: list[Order]
    quote: Quote | None = None
    delta: float | None = None  # entries only
    open_interest: int | None = None  # entries only
    theta: float | None = None  # entries only, $ per share per day

    def position(self, symbol: str) -> Position | None:
        return next((p for p in self.positions if p.symbol == symbol and p.qty > 0), None)

    def open_for(self, symbol: str, side: str) -> list[Order]:
        return [o for o in self.open_orders if o.symbol == symbol and o.side == side and o.is_open]

    def opened_at(self, symbol: str) -> datetime | None:
        fills = [o.filled_at for o in self.recent_orders if o.symbol == symbol and o.side == "buy" and o.filled_at]
        return max(fills) if fills else None


class RiskGate:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.r = cfg.risk
        self.tz = cfg.schedule.tz
        self._session: tuple[date, time] | None = None  # (day, close) from the exchange calendar

    # ---- account-level limits ---------------------------------------------------------------

    @staticmethod
    def start_of_day_equity(account: Account) -> float:
        return account.last_equity or account.equity

    def daily_loss_limit_usd(self, account: Account) -> float:
        return self.start_of_day_equity(account) * self.r.daily_loss_limit_pct / 100

    def drawdown_usd(self, account: Account) -> float:
        return max(0.0, self.start_of_day_equity(account) - account.equity)

    def daily_loss_breached(self, account: Account) -> bool:
        return self.drawdown_usd(account) >= self.daily_loss_limit_usd(account)

    # ---- session times: everything shifts earlier on early-close days -------------------------

    def ensure_session(self, broker, day: date) -> None:
        """Fetch today's close from the exchange calendar once a day (1:00 pm on half days)."""
        if self._session is None or self._session[0] != day:
            self.set_session_close(day, broker.session_close(day))

    def set_session_close(self, day: date, close: time | None) -> None:
        self._session = (day, close or REGULAR_CLOSE)

    def session_close(self, when: datetime) -> time:
        local = when.astimezone(self.tz)
        if self._session and self._session[0] == local.date():
            return self._session[1]
        return REGULAR_CLOSE

    def effective(self, t: time, when: datetime) -> time:
        """A configured time of day, moved earlier by however early the market closes today."""
        local = when.astimezone(self.tz)
        close = self.session_close(local)
        if close >= REGULAR_CLOSE:
            return t
        early = datetime.combine(local.date(), REGULAR_CLOSE) - datetime.combine(local.date(), close)
        return (datetime.combine(local.date(), t) - early).time()

    def in_trading_window(self, now: datetime) -> bool:
        t = now.astimezone(self.tz).time()
        return self.cfg.schedule.trading_start <= t < self.effective(self.cfg.schedule.trading_end, now)

    def entries_allowed(self, now: datetime) -> bool:
        t = now.astimezone(self.tz).time()
        return self.cfg.schedule.trading_start <= t < self.effective(self.cfg.schedule.no_new_entries_after, now)

    def earnings_block(self, underlying: str, today: date):
        """The earnings event that blocks new entries in this underlying today, if any."""
        for e in self.r.earnings:
            if e.symbol == underlying:
                last = e.last_session_before()
                start = last
                for _ in range(self.r.earnings_block_days):  # step back in weekdays, not calendar days
                    start -= timedelta(days=1)
                    while start.weekday() >= 5:
                        start -= timedelta(days=1)
                if start <= today <= last:
                    return e
        return None

    def earnings_exit_due(self, underlying: str, now: datetime) -> bool:
        """True on the last session before an earnings gap, from earnings_exit_time on."""
        local = now.astimezone(self.tz)
        return any(e.symbol == underlying and e.last_session_before() == local.date()
                   and local.time() >= self.effective(self.r.earnings_exit_time, now) for e in self.r.earnings)

    def blackout_at(self, now: datetime):
        local = now.astimezone(self.tz)
        return next((b for b in self.r.blackout if b.covers(local)), None)

    def risk_to_stop(self, premium_usd: float) -> float:
        """Loss if a position stops out, padded for stops that fill past their price."""
        return premium_usd * self.cfg.exits.stop_loss_pct / 100 * self.r.stop_slippage_factor

    def open_risk_usd(self, ctx: OrderContext) -> float:
        """How much further equity falls if every position and pending entry hits its stop.

        Equity already includes unrealized P&L, so a held position only adds the distance
        from its current price down to its stop.
        """
        keep = 1 - self.cfg.exits.stop_loss_pct / 100
        held = sum(
            p.qty * p.multiplier * max(0.0, p.current_price - p.avg_entry_price * keep)
            for p in ctx.positions
            if p.option
        ) * self.r.stop_slippage_factor
        pending = sum(
            (o.qty - o.filled_qty) * 100 * (o.limit_price or 0)
            for o in ctx.open_orders
            if o.side == "buy" and o.is_open
        )
        return held + self.risk_to_stop(pending)

    def soft_limit_hit(self, account: Account) -> bool:
        soft = self.r.soft_loss_limit_pct
        return soft is not None and self.drawdown_usd(account) >= self.start_of_day_equity(account) * soft / 100

    def risk_per_trade_pct(self, account: Account) -> float:
        """Halved once the day's drawdown passes the soft limit: smaller bets on a bad day."""
        return self.r.max_risk_per_trade_pct / (2 if self.soft_limit_hit(account) else 1)

    def max_qty(self, price: float, ctx: OrderContext) -> int:
        """Most contracts at this price that every size limit would accept right now."""
        if price <= 0:
            return 0
        per_contract = price * 100
        risk_each = self.risk_to_stop(per_contract)
        caps = [
            self.r.max_contracts_per_order,
            self.r.max_position_usd // per_contract,
            (self.start_of_day_equity(ctx.account) * self.risk_per_trade_pct(ctx.account) / 100) // risk_each,
            max(0.0, self.remaining_risk_budget(ctx)) // risk_each,
            ctx.account.options_buying_power // per_contract,
        ]
        return max(0, int(min(caps)))

    def remaining_risk_budget(self, ctx: OrderContext) -> float:
        return self.daily_loss_limit_usd(ctx.account) - self.drawdown_usd(ctx.account) - self.open_risk_usd(ctx)

    # ---- the one check ----------------------------------------------------------------------

    def check(self, req: OrderRequest, ctx: OrderContext, kind: Kind, discretionary: bool = False) -> tuple[bool, str]:
        """Every order goes through here. Returns (ok, reason)."""
        if not isinstance(req.qty, int) or req.qty < 1:
            return False, f"qty must be a positive whole number, got {req.qty!r}"
        if kind == "entry":
            return self._check_entry(req, ctx)
        return self._check_close(req, ctx, kind, discretionary)

    def _check_entry(self, req: OrderRequest, ctx: OrderContext) -> tuple[bool, str]:
        r = self.r
        occ = OptionSymbol.parse(req.symbol)
        if occ is None:
            return False, f"{req.symbol} is not an option symbol; only options can be bought"
        if occ.underlying not in r.universe:
            return False, f"{occ.underlying} is not in the universe {r.universe}"
        if req.side != "buy" or req.position_intent != "buy_to_open":
            return False, "entries must be buy_to_open"
        if req.type != "limit" or not req.limit_price or req.limit_price <= 0:
            return False, "entries must be limit orders with a positive price"
        if req.qty > r.max_contracts_per_order:
            return False, f"qty {req.qty} > max_contracts_per_order {r.max_contracts_per_order}"

        today = ctx.now.astimezone(self.tz).date()
        dte = occ.dte(today)
        if not r.min_dte <= dte <= r.max_dte:
            return False, f"{dte} days to expiration is outside {r.min_dte}-{r.max_dte}"
        if not self.entries_allowed(ctx.now):
            return False, f"no new entries outside {self.cfg.schedule.trading_start}-{self.cfg.schedule.no_new_entries_after}"
        if window := self.blackout_at(ctx.now):
            return False, f"entry blackout {window.start:%H:%M}-{window.end:%H:%M} ({window.reason or 'scheduled'})"

        q = ctx.quote
        if q is None or not q.valid:
            return False, "no valid quote"
        age = q.age_seconds(ctx.now)
        if age is not None and age > r.max_quote_age_seconds:
            return False, f"quote is {age:.0f}s old (max_quote_age_seconds {r.max_quote_age_seconds})"
        if q.spread_pct > r.max_spread_pct:
            return False, f"spread {q.spread_pct:.1f}% > max_spread_pct {r.max_spread_pct}%"
        if req.limit_price > q.ask:
            return False, f"limit {req.limit_price} is above the ask {q.ask}"
        if r.delta_min is not None or r.delta_max is not None:
            if ctx.delta is None:
                return False, "delta unknown"
            lo, hi = r.delta_min or 0.0, r.delta_max or 1.0
            if not lo <= abs(ctx.delta) <= hi:
                return False, f"|delta| {abs(ctx.delta):.2f} outside {lo:.2f}-{hi:.2f}"
        if r.max_theta_pct_per_day is not None:
            if ctx.theta is None:
                return False, "theta unknown"
            decay = abs(ctx.theta) / q.mid * 100
            if decay > r.max_theta_pct_per_day:
                return False, f"time decay {decay:.1f}%/day > max_theta_pct_per_day {r.max_theta_pct_per_day:g}"
        # Open interest is yesterday's proxy for liquidity; a tight live spread is direct evidence.
        # SPY/QQQ list a strike every $1 and an expiry every day, so liquid contracts can show low OI.
        if r.min_open_interest and q.spread_pct > r.oi_waived_below_spread_pct:
            if ctx.open_interest is None or ctx.open_interest < r.min_open_interest:
                return False, (f"open interest {ctx.open_interest} < min_open_interest {r.min_open_interest} "
                               f"and spread {q.spread_pct:.1f}% > {r.oi_waived_below_spread_pct:g}%")
        if event := self.earnings_block(occ.underlying, today):
            return False, f"{occ.underlying} reports earnings {event.date} ({event.time}); no new entries until after"

        if ctx.open_for(req.symbol, "buy"):
            return False, f"an entry order for {req.symbol} is already working"

        committed = {p.symbol for p in ctx.positions if p.option and p.qty > 0}
        committed |= {o.symbol for o in ctx.open_orders if o.side == "buy" and o.is_open}
        if req.symbol not in committed and len(committed) >= r.max_positions:
            return False, f"already at max_positions {r.max_positions}"
        # The universe moves together (S&P 500 vs Nasdaq-100 correlation ~0.98), so several calls,
        # or several puts, are one bet. Cap how many point the same way.
        same_way = {s for s in committed if s != req.symbol and (o := OptionSymbol.parse(s)) and o.right == occ.right}
        if req.symbol not in committed and len(same_way) >= r.max_same_direction_positions:
            side = "calls" if occ.right == "C" else "puts"
            return False, f"already {len(same_way)} {side} (max_same_direction_positions {r.max_same_direction_positions})"

        order_usd = req.qty * 100 * req.limit_price
        held = ctx.position(req.symbol)
        held_usd = held.qty * 100 * (q.mid if q.valid else held.current_price) if held else 0.0
        if held_usd + order_usd > r.max_position_usd:
            return False, f"position would be ${held_usd + order_usd:,.0f} > max_position_usd ${r.max_position_usd:,.0f}"

        entries_today = [
            o for o in ctx.recent_orders
            if o.side == "buy" and o.client_order_id.startswith("pb-entry-")
            and o.submitted_at and o.submitted_at.astimezone(self.tz).date() == today
            and (o.filled_qty > 0 or o.is_open)
        ]
        if len(entries_today) >= r.max_entries_per_day:
            return False, f"already {len(entries_today)} entries today (max_entries_per_day {r.max_entries_per_day})"
        if r.entry_cooldown_minutes and entries_today:
            last = max(o.submitted_at for o in entries_today)
            waited = (ctx.now - last).total_seconds() / 60
            if waited < r.entry_cooldown_minutes:
                return False, f"last entry {waited:.0f} min ago; entry cooldown {r.entry_cooldown_minutes} min"

        streak = loss_streak(closed_on(closed_trades(ctx.recent_orders), today, self.tz))
        if streak >= r.max_consecutive_losses:
            return False, f"{streak} losing trades in a row today (max_consecutive_losses {r.max_consecutive_losses})"

        # The cooldown starts when an exit is sent, not only when it fills: otherwise an exit and a
        # new entry on the same underlying in one cycle (a put-to-call flip) slip past it.
        # Resting protective stops don't count until they actually fill.
        cooldown_start = ctx.now - timedelta(minutes=r.reentry_cooldown_minutes)
        seen = {o.id: o for o in ctx.recent_orders + ctx.open_orders}
        for o in seen.values():
            sold = OptionSymbol.parse(o.symbol)
            if o.side != "sell" or not sold or sold.underlying != occ.underlying:
                continue
            if o.filled_qty > 0 and o.filled_at:
                when = o.filled_at
            elif o.is_open and not o.is_stop and o.submitted_at:
                when = o.submitted_at
            else:
                continue
            if when >= cooldown_start:
                # Broker timestamps are UTC; this text goes into the AI's prompt, so say it in market time.
                return False, (f"{occ.underlying} was sold {when.astimezone(self.tz):%H:%M} ET; "
                               f"re-entry cooldown {r.reentry_cooldown_minutes} min")

        new_risk = self.risk_to_stop(order_usd)
        per_trade_cap = self.start_of_day_equity(ctx.account) * self.risk_per_trade_pct(ctx.account) / 100
        if new_risk > per_trade_cap:
            halved = " (halved: past the soft loss limit)" if self.soft_limit_hit(ctx.account) else ""
            return False, (f"risk to stop ${new_risk:,.0f} > max_risk_per_trade_pct {r.max_risk_per_trade_pct}% "
                           f"(${per_trade_cap:,.0f}){halved}")

        # Size against what's left of today's loss budget (risk padded by stop_slippage_factor).
        budget = self.remaining_risk_budget(ctx)
        if new_risk > budget:
            return False, f"risk to stop ${new_risk:,.0f} > remaining daily loss budget ${max(budget, 0):,.0f}"

        if order_usd > ctx.account.options_buying_power:
            return False, f"order ${order_usd:,.0f} > options buying power ${ctx.account.options_buying_power:,.0f}"
        return True, "ok"

    def _check_close(self, req: OrderRequest, ctx: OrderContext, kind: Kind, discretionary: bool) -> tuple[bool, str]:
        # Closing is allowed for anything held, whitelist or not: you should always be able to get out.
        if req.side != "sell" or req.position_intent != "sell_to_close":
            return False, "exits must be sell_to_close"
        pos = ctx.position(req.symbol)
        if pos is None:
            return False, f"{req.symbol} is not held"
        resting = sum(o.qty - o.filled_qty for o in ctx.open_for(req.symbol, "sell") if o.client_order_id != req.client_order_id)
        if req.qty > pos.qty - resting:
            return False, f"qty {req.qty} > {pos.qty - resting} available to sell ({pos.qty} held, {resting} in resting sells)"

        if kind == "stop":
            if req.type != "stop" or not req.stop_price or req.stop_price <= 0:
                return False, "protective stops must be stop orders with a positive stop price"
            q = ctx.quote
            if q is not None and q.valid and req.stop_price >= q.bid:
                return False, f"bid {q.bid} is already at or below the stop {req.stop_price}; the code exit handles it"

        if discretionary:
            opened = ctx.opened_at(req.symbol)
            if opened is not None:
                held_min = (ctx.now - opened).total_seconds() / 60
                if held_min < self.r.min_hold_minutes:
                    return False, f"held {held_min:.0f} min < min_hold_minutes {self.r.min_hold_minutes}"
        return True, "ok"
