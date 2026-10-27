"""The order funnel: the only code that places or cancels orders.

Every order is built here from fresh broker state, checked by RiskGate.check(), and
only then sent. flatten_all() is the one path that skips the gate, because it only
ever closes positions.
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections import deque
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Literal

from .broker import Broker, BrokerError, DuplicateOrderError, wait_until_closed
from .config import Config
from .exits import protective_stop_price
from .journal import Journal
from .models import OptionSymbol, Order, OrderRequest, Position, Quote, round_to_tick
from .notify import Alerts
from .risk import Kind, KillSwitch, OrderContext, RiskGate

Status = Literal["submitted", "rejected", "dry_run", "skipped", "duplicate", "error"]


@dataclass(frozen=True)
class ExecResult:
    status: Status
    kind: Kind
    symbol: str
    qty: int
    detail: str
    request: OrderRequest | None = None
    order: Order | None = None


def client_order_id(kind: Kind, slot: str, symbol: str, side: str, qty: int, extra: str = "") -> str:
    """Deterministic id: the same order retried in the same cycle can't be filled twice,
    because Alpaca rejects a repeated client_order_id."""
    digest = hashlib.sha1(f"{kind}|{slot}|{symbol}|{side}|{qty}|{extra}".encode()).hexdigest()[:12]
    return f"pb-{kind}-{slot}-{digest}"


class OrderFunnel:
    def __init__(self, broker: Broker, gate: RiskGate, journal: Journal, alerts: Alerts, cfg: Config,
                 now: Callable[[], datetime], lock: threading.RLock, dry_run: bool = False,
                 killswitch: KillSwitch | None = None):
        self.broker = broker
        self.gate = gate
        self.journal = journal
        self.alerts = alerts
        self.cfg = cfg
        self.now = now
        self.lock = lock
        self.dry_run = dry_run
        self.killswitch = killswitch
        self._actions: deque[datetime] = deque()  # order submissions and cancels, for runaway protection
        self._errors: deque[datetime] = deque()
        self.stop_rejections = 0
        self.stops_disabled: str | None = None  # set if the broker keeps rejecting stop orders

    def context(self, symbol: str | None = None, entry: bool = False) -> OrderContext:
        now = self.now()
        delta = open_interest = theta = None
        if symbol and entry:  # entries also need greeks and open interest for the filters
            detail = self.broker.option_detail(symbol)
            quote = detail.quote if detail else None
            delta, open_interest, theta = (detail.delta, detail.open_interest, detail.theta) if detail else (None,) * 3
        else:
            quote = self.broker.option_quotes([symbol]).get(symbol) if symbol else None
        return OrderContext(
            now=now,
            account=self.broker.account(),
            positions=self.broker.positions(),
            open_orders=self.broker.open_orders(),
            recent_orders=self.broker.orders_since(now - timedelta(days=7)),
            quote=quote,
            delta=delta,
            open_interest=open_interest,
            theta=theta,
        )

    # ---- entries ------------------------------------------------------------------------------

    def enter(self, symbol: str, qty: int, reason: str, slot: str) -> ExecResult:
        """Buy to open at a limit near the mid of a quote fetched right now."""
        with self.lock:
            symbol = symbol.strip().upper()
            ctx = self.context(symbol, entry=True)
            q = ctx.quote
            limit = None
            if q is not None and q.valid:
                occ = OptionSymbol.parse(symbol)
                underlying = occ.underlying if occ else symbol
                raw = q.mid + self.cfg.orders.entry_offset * (q.ask - q.mid)
                limit = min(round_to_tick(raw, underlying, "down"), q.ask)
            req = OrderRequest(
                symbol=symbol, side="buy", qty=qty, type="limit", time_in_force="day",
                client_order_id=client_order_id("entry", slot, symbol, "buy", qty),
                position_intent="buy_to_open", limit_price=limit,
            )
            if limit is None:
                return self._rejected(req, "entry", "no valid quote", reason)
            ok, why = self.gate.check(req, ctx, kind="entry")
            if not ok:
                return self._rejected(req, "entry", why, reason)
            return self._submit(req, "entry", reason, quote=q)

    # ---- exits ------------------------------------------------------------------------------

    def exit(self, symbol: str, qty: int | None, reason: str, slot: str, discretionary: bool) -> ExecResult:
        """Sell to close. First try a limit at the bid; if an earlier exit for this contract
        didn't fill within exit_order_ttl_seconds, send a market order."""
        with self.lock:
            ctx = self.context(symbol)
            pos = ctx.position(symbol)
            if pos is None:
                return ExecResult("rejected", "exit", symbol, qty or 0, f"{symbol} is not held")
            qty = qty or pos.qty
            sells = ctx.open_for(symbol, "sell")
            working = [o for o in sells if not o.is_stop]
            stops = [o for o in sells if o.is_stop]

            escalate = self._had_unfilled_exit(ctx, symbol)
            if working:
                ttl = self.cfg.exits.exit_order_ttl_seconds
                if all(self._age_s(o, ctx.now) < ttl for o in working):
                    return ExecResult("skipped", "exit", symbol, qty, "an exit order is already working")
                escalate = True

            q = ctx.quote
            use_market = escalate or q is None or not q.valid
            order_type = "market" if use_market else "limit"
            req = OrderRequest(
                symbol=symbol, side="sell", qty=qty, type=order_type, time_in_force="day",
                client_order_id=client_order_id("exit", slot, symbol, "sell", qty, order_type),
                position_intent="sell_to_close",
                limit_price=None if use_market else round_to_tick(q.bid, pos.underlying, "down"),
            )
            # Check as if the resting orders we're about to cancel were already gone, so a
            # rejected exit never leaves the position without its broker stop.
            to_cancel = working + stops
            ctx_after = dataclasses.replace(ctx, open_orders=[o for o in ctx.open_orders if o not in to_cancel])
            ok, why = self.gate.check(req, ctx_after, kind="exit", discretionary=discretionary)
            if not ok:
                return self._rejected(req, "exit", why, reason)

            for o in to_cancel:
                final = self.cancel(o, f"making room for exit ({reason})")
                if final.is_open:
                    return ExecResult("error", "exit", symbol, qty, f"could not cancel resting order {o.id}")
            if to_cancel and not self.dry_run:
                # A resting order may have filled while we cancelled it.
                still = next((p for p in self.broker.positions() if p.symbol == symbol and p.qty > 0), None)
                if still is None:
                    return ExecResult("skipped", "exit", symbol, qty, "position closed while cancelling resting orders")
                if still.qty < req.qty:
                    req = dataclasses.replace(
                        req, qty=still.qty,
                        client_order_id=client_order_id("exit", slot, symbol, "sell", still.qty, order_type),
                    )
            return self._submit(req, "exit", reason, quote=ctx.quote)

    def place_stop(self, position: Position, slot: str, price: float | None = None,
                   replacing: list[Order] | None = None) -> ExecResult:
        """Keep a real stop order at the broker, so a stop-loss triggers between our checks.

        Any stops in `replacing` are cancelled only after the new one passes the gate, so a
        rejected replacement never leaves the position unprotected.
        """
        with self.lock:
            replacing = replacing or []
            ctx = self.context(position.symbol)
            price = price if price is not None else protective_stop_price(position, self.cfg.exits)
            req = OrderRequest(
                symbol=position.symbol, side="sell", qty=position.qty, type="stop", time_in_force="gtc",
                client_order_id=client_order_id("stop", slot, position.symbol, "sell", position.qty, f"{price:.2f}"),
                position_intent="sell_to_close", stop_price=price,
            )
            ctx_after = dataclasses.replace(ctx, open_orders=[o for o in ctx.open_orders if o not in replacing])
            ok, why = self.gate.check(req, ctx_after, kind="stop")
            if not ok:
                return self._rejected(req, "stop", why, "protective stop", alert=False)
            for o in replacing:
                final = self.cancel(o, f"replacing protective stop with {price:.2f}")
                if final.is_open:
                    return ExecResult("error", "stop", position.symbol, position.qty, f"could not cancel stop {o.id}")
                if final.filled_qty > 0:  # it triggered while we replaced it; the next tick reconciles
                    return ExecResult("skipped", "stop", position.symbol, position.qty, "old stop filled while replacing")
            return self._submit(req, "stop", "protective stop")

    # ---- cancels and flatten ----------------------------------------------------------------

    def cancel(self, order: Order, why: str) -> Order:
        with self.lock:
            if self.dry_run:
                self.journal.log("cancel_dry_run", order_id=order.id, symbol=order.symbol, why=why)
                return dataclasses.replace(order, status="canceled")
            self._count(self._actions, self.cfg.risk.max_order_actions_per_10min, "order actions")
            try:
                self.broker.cancel_order(order.id)
            except BrokerError as e:
                self.journal.log("cancel_error", order_id=order.id, symbol=order.symbol, error=str(e))
            final = wait_until_closed(self.broker, order.id)
            self.journal.log("order_cancelled", order_id=order.id, symbol=order.symbol, side=order.side,
                             type=order.type, why=why, final_status=final.status, filled_qty=final.filled_qty)
            return final

    def flatten_all(self, reason: str) -> None:
        """Cancel every order and close every position at market. Skips the gate on purpose."""
        with self.lock:
            self.journal.log("flatten", reason=reason, dry_run=self.dry_run)
            if not self.dry_run:
                self.broker.close_all()

    # ---- internals --------------------------------------------------------------------------

    def _submit(self, req: OrderRequest, kind: Kind, reason: str, quote: Quote | None = None) -> ExecResult:
        fields = dict(kind=kind, symbol=req.symbol, side=req.side, qty=req.qty, type=req.type,
                      limit_price=req.limit_price, stop_price=req.stop_price,
                      client_order_id=req.client_order_id, reason=reason)
        if quote is not None and quote.valid:  # the decision price, for slippage measurement
            fields.update(bid=quote.bid, ask=quote.ask, mid=round(quote.mid, 4))
        if self.dry_run:
            self.journal.log("order_dry_run", **fields)
            return ExecResult("dry_run", kind, req.symbol, req.qty, "dry run: not sent", req)
        if self.killswitch is not None and (why := self.killswitch.all_halted()):
            return ExecResult("rejected", kind, req.symbol, req.qty, f"HALT_ALL: {why}", req)
        if self._count(self._actions, self.cfg.risk.max_order_actions_per_10min, "order actions"):
            return ExecResult("rejected", kind, req.symbol, req.qty, "runaway protection tripped", req)
        try:
            order = self.broker.submit_order(req)
        except DuplicateOrderError as e:
            self.journal.log("order_duplicate", **fields, error=str(e))
            return ExecResult("duplicate", kind, req.symbol, req.qty, "already sent this cycle", req)
        except BrokerError as e:
            self.journal.log("order_error", **fields, error=str(e))
            self._count(self._errors, self.cfg.risk.max_order_errors_per_10min, "order errors")
            if kind == "stop":
                self._stop_rejected(str(e))
            else:
                self.alerts.send("Order error", f"{kind} {req.side} {req.qty} {req.symbol}: {e}", "high", ["warning"])
            return ExecResult("error", kind, req.symbol, req.qty, str(e), req)
        if kind == "stop":
            self.stop_rejections = 0
        self.journal.log("order_submitted", **fields, order_id=order.id, status=order.status)
        if kind != "stop":
            price = f" @ {req.limit_price:.2f}" if req.limit_price else " at market"
            priority = "high" if reason in ("stop_loss", "daily_loss") else "default"
            self.alerts.send(f"{kind.title()}: {req.side} {req.qty} {req.symbol}", f"{req.type}{price}. {reason}",
                             priority, ["chart_with_upwards_trend" if req.side == "buy" else "outbox_tray"])
        return ExecResult("submitted", kind, req.symbol, req.qty, "sent", req, order)

    def _count(self, events: deque[datetime], limit: int, what: str) -> bool:
        """Record one event; trip the runaway breaker if the 10-minute count reaches the limit.
        Returns True if the breaker is (now) tripped. Knight Capital lost $440M in 45 minutes to
        a loop of erroneous orders; this caps how far a bug here could run."""
        now = self.now()
        while events and (now - events[0]).total_seconds() > 600:
            events.popleft()
        if len(events) >= limit:
            if self.killswitch is not None and not self.killswitch.all_halted():
                why = f"runaway protection: {len(events)} {what} in 10 minutes"
                self.killswitch.halt(why, scope="all")
                self.journal.log("runaway_protection", what=what, count=len(events))
                self.alerts.send("Runaway protection: everything stopped",
                                 f"{len(events)} {what} in 10 minutes. paperbot stopped all activity (HALT_ALL). "
                                 "Broker stop orders stay at Alpaca. Check logs/, then run `paperbot resume`.",
                                 "urgent", ["rotating_light"])
            return True
        events.append(now)
        return False

    STOP_REJECTIONS_BEFORE_FALLBACK = 3

    def _stop_rejected(self, error: str) -> None:
        """Alpaca's docs disagree on whether options accept stop orders. If the broker keeps
        rejecting them, stop trying and rely on the code stop-loss checked every watch tick."""
        self.stop_rejections += 1
        if self.stop_rejections >= self.STOP_REJECTIONS_BEFORE_FALLBACK and not self.stops_disabled:
            self.stops_disabled = error
            self.journal.log("broker_stops_disabled", error=error)
            self.alerts.send("Broker stops unavailable",
                             f"Alpaca rejected {self.stop_rejections} stop orders in a row ({error[:120]}). "
                             "Using code stop-losses only, checked every watch tick.", "high", ["warning"])

    def _rejected(self, req: OrderRequest, kind: Kind, why: str, reason: str, alert: bool = True) -> ExecResult:
        self.journal.log("order_rejected", kind=kind, symbol=req.symbol, side=req.side, qty=req.qty,
                         type=req.type, limit_price=req.limit_price, stop_price=req.stop_price,
                         why=why, reason=reason)
        if alert and self.cfg.alerts.notify_rejections:
            self.alerts.send(f"Rejected {kind}: {req.symbol}", why, "low")
        return ExecResult("rejected", kind, req.symbol, req.qty, why, req)

    def _had_unfilled_exit(self, ctx: OrderContext, symbol: str) -> bool:
        today = ctx.now.astimezone(self.gate.tz).date()
        return any(
            o.symbol == symbol and o.client_order_id.startswith("pb-exit-") and not o.is_open
            and o.status in ("canceled", "expired") and o.submitted_at
            and o.submitted_at.astimezone(self.gate.tz).date() == today
            for o in ctx.recent_orders
        )

    @staticmethod
    def _age_s(order: Order, now: datetime) -> float:
        return (now - order.submitted_at).total_seconds() if order.submitted_at else 0.0
