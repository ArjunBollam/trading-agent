"""In-memory broker, scripted Claude, and recording alert channel for offline tests."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from paperbot.broker import BrokerError, DuplicateOrderError
from paperbot.models import (
    Account, Contract, MarketClock, OptionDetail, OptionSymbol, Order, OrderRequest, Position, Quote, StockSnap,
)

ET = ZoneInfo("America/New_York")
MONDAY_1040 = datetime(2026, 10, 26, 10, 40, tzinfo=ET)
CALL = "SPY261030C00580000"  # 4 days to expiration on MONDAY_1040
PUT = "SPY261030P00580000"


class Clock:
    def __init__(self, dt: datetime = MONDAY_1040):
        self.dt = dt

    def __call__(self) -> datetime:
        return self.dt

    def advance(self, **kw: float) -> None:
        self.dt += timedelta(**kw)


class FakeBroker:
    def __init__(self, clock: Clock, equity: float = 100_000):
        self.now = clock
        self.market_open = True
        self.acct = Account("PA123", equity, equity, equity)
        self.pos: dict[str, Position] = {}
        self.orders: dict[str, Order] = {}
        self.quotes: dict[str, Quote] = {}
        self.stocks: dict[str, StockSnap] = {}
        self.chains: dict[str, list[Contract]] = {}
        self.submitted: list[OrderRequest] = []
        self.cancelled: list[str] = []
        self.closed_all = 0
        self.calls: list[str] = []
        self.hold_fills = False  # True: orders rest until fill_at_limit()
        self.open_interest: dict[str, int] = {}  # default 1000 when not set
        self.deltas: dict[str, float] = {}
        self.thetas: dict[str, float] = {}  # default -2% of the mid per day
        self.early_close: dict[date, time] = {}
        self._n = 0

    # ---- test helpers --------------------------------------------------------------------

    def set_quote(self, symbol: str, bid: float, ask: float) -> None:
        self.quotes[symbol] = Quote(symbol, bid, ask)
        self._match()

    def add_position(self, symbol: str, qty: int, entry: float, opened_minutes_ago: float = 60) -> None:
        self.pos[symbol] = Position(symbol, qty, entry, entry, "us_option")
        self._n += 1
        t = self.now() - timedelta(minutes=opened_minutes_ago)
        oid = f"seed{self._n}"
        self.orders[oid] = Order(oid, f"pb-entry-seed-{self._n}", symbol, "buy", qty, qty, "limit", "filled",
                                 entry, None, t, t, entry)

    def fill_at_limit(self, order_id: str) -> None:
        o = self.orders[order_id]
        self._fill(o, o.limit_price or self.quotes[o.symbol].mid)

    def set_equity(self, equity: float) -> None:
        self.acct = dataclasses.replace(self.acct, equity=equity)

    # ---- Broker protocol -----------------------------------------------------------------

    def clock(self) -> MarketClock:
        self.calls.append("clock")
        now = self.now()
        return MarketClock(now, self.market_open, now + timedelta(hours=20), now + timedelta(hours=5))

    def account(self) -> Account:
        self.calls.append("account")
        return self.acct

    def positions(self) -> list[Position]:
        out = []
        for p in self.pos.values():
            q = self.quotes.get(p.symbol)
            out.append(dataclasses.replace(p, current_price=q.mid) if q and q.valid else p)
        return out

    def open_orders(self) -> list[Order]:
        return [o for o in self.orders.values() if o.is_open]

    def orders_since(self, after: datetime) -> list[Order]:
        return [o for o in self.orders.values() if o.submitted_at and o.submitted_at >= after]

    def get_order(self, order_id: str) -> Order:
        return self.orders[order_id]

    def submit_order(self, req: OrderRequest) -> Order:
        if any(o.client_order_id == req.client_order_id for o in self.orders.values()):
            raise DuplicateOrderError("client_order_id must be unique")
        if req.position_intent == "sell_to_close" and req.symbol not in self.pos:
            raise BrokerError("position does not exist")
        self._n += 1
        oid = f"o{self._n}"
        self.orders[oid] = Order(oid, req.client_order_id, req.symbol, req.side, req.qty, 0, req.type, "new",
                                 req.limit_price, req.stop_price, self.now())
        self.submitted.append(req)
        self._match()
        return self.orders[oid]

    def cancel_order(self, order_id: str) -> None:
        o = self.orders[order_id]
        if o.is_open:
            self.orders[order_id] = dataclasses.replace(o, status="canceled")
            self.cancelled.append(order_id)

    def close_all(self) -> None:
        self.closed_all += 1
        for oid, o in list(self.orders.items()):
            if o.is_open:
                self.orders[oid] = dataclasses.replace(o, status="canceled")
        self.pos.clear()

    def session_close(self, day: date) -> time | None:
        return self.early_close.get(day, time(16, 0))

    def option_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        return {s: self.quotes[s] for s in symbols if s in self.quotes}

    def option_detail(self, symbol: str) -> OptionDetail | None:
        q = self.quotes.get(symbol)
        if q is None:
            return None
        chain_delta = next((c.delta for cs in self.chains.values() for c in cs if c.symbol == symbol), None)
        occ = OptionSymbol.parse(symbol)
        default = (0.5 if occ.right == "C" else -0.5) if occ else None
        delta = self.deltas.get(symbol, chain_delta if chain_delta is not None else default)
        if occ and occ.right == "P" and delta is not None and delta > 0:
            delta = -delta
        theta = self.thetas.get(symbol, -0.02 * q.mid)
        return OptionDetail(q, delta, 0.2, self.open_interest.get(symbol, 1000), theta)

    def option_chain(self, underlying: str, exp_gte: date, exp_lte: date, strike_gte: float, strike_lte: float):
        return [c for c in self.chains.get(underlying, [])
                if exp_gte <= c.expiry <= exp_lte and strike_gte <= c.strike <= strike_lte]

    def stock_snapshots(self, symbols: list[str]) -> dict[str, StockSnap]:
        return {s: self.stocks[s] for s in symbols if s in self.stocks}

    def stock_bars(self, symbol: str, timeframe: str, limit: int, start=None) -> list[dict]:
        return [{"t": "2026-10-26T14:00:00Z", "o": 579, "h": 581, "l": 578, "c": 580.5, "v": 1000}][:limit]

    def news(self, symbols: list[str], limit: int) -> list[dict]:
        return [{"headline": f"{symbols[0]} rallies", "summary": "test"}][:limit]

    # ---- matching ------------------------------------------------------------------------

    def _match(self) -> None:
        if self.hold_fills:
            return
        for o in list(self.orders.values()):
            if not o.is_open:
                continue
            q = self.quotes.get(o.symbol)
            if q is None or not q.valid:
                continue
            if o.type == "market":
                self._fill(o, q.ask if o.side == "buy" else q.bid)
            elif o.type == "limit":
                if o.side == "buy" and o.limit_price >= q.ask:
                    self._fill(o, q.ask)
                elif o.side == "sell" and o.limit_price <= q.bid:
                    self._fill(o, q.bid)
            elif o.type == "stop" and o.side == "sell" and q.bid <= o.stop_price:
                self._fill(o, q.bid)

    def _fill(self, o: Order, price: float) -> None:
        now = self.now()
        self.orders[o.id] = dataclasses.replace(o, status="filled", filled_qty=o.qty, filled_at=now,
                                                filled_avg_price=price)
        p = self.pos.get(o.symbol)
        if o.side == "buy":
            if p:
                qty = p.qty + o.qty
                avg = (p.avg_entry_price * p.qty + price * o.qty) / qty
                self.pos[o.symbol] = dataclasses.replace(p, qty=qty, avg_entry_price=avg)
            else:
                self.pos[o.symbol] = Position(o.symbol, o.qty, price, price, "us_option")
        elif p:
            left = p.qty - o.qty
            if left > 0:
                self.pos[o.symbol] = dataclasses.replace(p, qty=left)
            else:
                del self.pos[o.symbol]


def seed_market(broker: FakeBroker) -> None:
    """SPY at 580 with a tight-spread call and put, plus the other underlyings."""
    for sym, px in {"SPY": 580.0, "QQQ": 500.0, "AAPL": 230.0, "MSFT": 420.0, "NVDA": 140.0}.items():
        broker.stocks[sym] = StockSnap(sym, px, px * 0.99, px * 0.995, px * 1.01, px * 0.99, 1e6)
    broker.chains["SPY"] = []
    for sym, bid, ask in ((CALL, 2.00, 2.10), (PUT, 1.90, 2.00)):
        occ = OptionSymbol.parse(sym)
        broker.chains["SPY"].append(Contract(sym, "SPY", occ.right, occ.strike, occ.expiry, bid, ask, 0.18, 0.5))
        broker.set_quote(sym, bid, ask)


# ---- scripted Claude ------------------------------------------------------------------------


@dataclass
class Block:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)


@dataclass
class Resp:
    content: list
    stop_reason: str
    model: str = "claude-opus-5-5"
    usage: Any = None


def decision(*proposals: dict, view: str = "test view") -> Resp:
    return Resp([Block("text", text=json.dumps({"market_view": view, "proposals": list(proposals)}))], "end_turn")


def tool_call(name: str, args: dict, id: str = "tu_1") -> Resp:
    return Resp([Block("tool_use", id=id, name=name, input=args)], "tool_use")


class FakeLLM:
    def __init__(self, *responses: Any, on_call=None):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.on_call = on_call
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.on_call:
            self.on_call()
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class RecordingChannel:
    def __init__(self, commands: list[str] | None = None, fail: bool = False):
        self.sent: list[tuple[str, str, str]] = []
        self.commands = list(commands or [])
        self.fail = fail

    def send(self, title: str, message: str, priority: str, tags: list[str]) -> None:
        if self.fail:
            raise RuntimeError("network down")
        self.sent.append((title, message, priority))

    def poll_commands(self) -> list[str]:
        out, self.commands = self.commands, []
        return out


# ---- scripted OpenAI-compatible provider (xAI / Groq) ----------------------------------------


def oa_text(text: str, finish: str = "stop", refusal: str | None = None) -> Any:
    msg = SimpleNamespace(content=text, refusal=refusal, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=finish)], model="fake", usage=None)


def oa_decision(*proposals: dict, view: str = "test view") -> Any:
    return oa_text(json.dumps({"market_view": view, "proposals": list(proposals)}))


def oa_tool_call(name: str, args: dict | str, id: str = "call_1") -> Any:
    arguments = args if isinstance(args, str) else json.dumps(args)
    tc = SimpleNamespace(id=id, type="function", function=SimpleNamespace(name=name, arguments=arguments))
    msg = SimpleNamespace(content=None, refusal=None, tool_calls=[tc])
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="tool_calls")], model="fake", usage=None)


class FakeOpenAI:
    def __init__(self, *responses: Any):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
