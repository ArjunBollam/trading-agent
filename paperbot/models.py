"""Plain data types shared by every module.

The broker adapter converts alpaca-py objects into these, so nothing else in the
package touches the broker SDK directly.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal
from typing import Literal

from pydantic import BaseModel, Field

# OCC option symbol: root, YYMMDD, C/P, strike * 1000 (8 digits). e.g. SPY261030C00580000
_OCC_RE = re.compile(r"^([A-Z]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")

# Option tick sizes under the Penny Interval Program: $0.01 below $3 and $0.05 at or above,
# except a few names that trade in pennies at every price.
_PENNY_AT_ALL_PRICES = {"SPY", "QQQ", "IWM"}

OPEN_ORDER_STATUSES = {
    "new", "partially_filled", "accepted", "pending_new", "accepted_for_bidding",
    "held", "pending_replace", "pending_cancel", "calculated", "done_for_day", "stopped",
}


@dataclass(frozen=True)
class OptionSymbol:
    raw: str
    underlying: str
    expiry: date
    right: Literal["C", "P"]
    strike: float

    @classmethod
    def parse(cls, symbol: str) -> OptionSymbol | None:
        m = _OCC_RE.match(symbol.strip().upper())
        if not m:
            return None
        root, yy, mm, dd, right, strike = m.groups()
        try:
            expiry = date(2000 + int(yy), int(mm), int(dd))
        except ValueError:
            return None
        return cls(symbol.strip().upper(), root, expiry, right, int(strike) / 1000)

    def dte(self, today: date) -> int:
        return (self.expiry - today).days


def tick_size(price: float, underlying: str) -> float:
    if underlying in _PENNY_AT_ALL_PRICES or price < 3.0:
        return 0.01
    return 0.05


def round_to_tick(price: float, underlying: str, direction: Literal["up", "down", "nearest"]) -> float:
    tick = Decimal(str(tick_size(price, underlying)))
    steps = Decimal(str(price)) / tick
    mode = {"up": ROUND_UP, "down": ROUND_DOWN, "nearest": ROUND_HALF_UP}[direction]
    return float(steps.quantize(Decimal(1), rounding=mode) * tick)


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    timestamp: datetime | None = None  # when the broker last updated it; None if unknown

    def age_seconds(self, now: datetime) -> float | None:
        return (now - self.timestamp).total_seconds() if self.timestamp else None

    @property
    def valid(self) -> bool:
        return self.bid > 0 and self.ask > 0 and self.ask >= self.bid

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self) -> float:
        return (self.ask - self.bid) / self.mid * 100 if self.valid else math.inf


@dataclass(frozen=True)
class OptionDetail:
    """A fresh quote plus the greeks and open interest used by the entry filters."""

    quote: Quote
    delta: float | None = None
    iv: float | None = None
    open_interest: int | None = None
    theta: float | None = None  # $ per share per day


@dataclass(frozen=True)
class MarketClock:
    timestamp: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime


@dataclass(frozen=True)
class Account:
    account_number: str
    equity: float
    last_equity: float  # equity at the previous close: the start-of-day reference
    options_buying_power: float
    options_level: int | None = None  # Alpaca: 2+ is needed to buy calls and puts
    trading_blocked: bool = False


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: int
    avg_entry_price: float  # per share, i.e. the option premium
    current_price: float
    asset_class: str  # "us_option" or "us_equity"

    @property
    def option(self) -> OptionSymbol | None:
        return OptionSymbol.parse(self.symbol) if self.asset_class == "us_option" else None

    @property
    def underlying(self) -> str:
        opt = self.option
        return opt.underlying if opt else self.symbol

    @property
    def multiplier(self) -> int:
        return 100 if self.asset_class == "us_option" else 1


@dataclass(frozen=True)
class Order:
    id: str
    client_order_id: str
    symbol: str
    side: Literal["buy", "sell"]
    qty: int
    filled_qty: int
    type: str  # market | limit | stop | stop_limit
    status: str
    limit_price: float | None = None
    stop_price: float | None = None
    submitted_at: datetime | None = None
    filled_at: datetime | None = None
    filled_avg_price: float | None = None

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_ORDER_STATUSES

    @property
    def is_stop(self) -> bool:
        return self.type in ("stop", "stop_limit")


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: Literal["buy", "sell"]
    qty: int
    type: Literal["market", "limit", "stop"]
    time_in_force: Literal["day", "gtc"]
    client_order_id: str
    position_intent: Literal["buy_to_open", "sell_to_close"]
    limit_price: float | None = None
    stop_price: float | None = None


@dataclass(frozen=True)
class Contract:
    """One row of an option chain, as shown to the AI."""

    symbol: str
    underlying: str
    right: Literal["C", "P"]
    strike: float
    expiry: date
    bid: float
    ask: float
    iv: float | None = None
    delta: float | None = None
    theta: float | None = None  # $ per share per day

    @property
    def quote(self) -> Quote:
        return Quote(self.symbol, self.bid, self.ask)


@dataclass(frozen=True)
class StockSnap:
    symbol: str
    price: float
    prev_close: float
    day_open: float
    day_high: float
    day_low: float
    volume: float
    prev_high: float | None = None
    prev_low: float | None = None
    as_of: datetime | None = None  # time of the latest trade

    @property
    def change_pct(self) -> float:
        return (self.price - self.prev_close) / self.prev_close * 100 if self.prev_close else 0.0


@dataclass
class Snapshot:
    """Everything one decision cycle knows about the account and market."""

    taken_at: datetime
    account: Account
    positions: list[Position]
    open_orders: list[Order]
    recent_orders: list[Order]
    stocks: dict[str, StockSnap] = field(default_factory=dict)
    candidates: dict[str, list[Contract]] = field(default_factory=dict)
    quotes: dict[str, Quote] = field(default_factory=dict)
    features: dict = field(default_factory=dict)  # symbol -> features.IntradayFeatures
    stale: list[str] = field(default_factory=list)  # underlyings whose market data is too old


# ---- What the AI returns. These are the only shapes it can express. -------------------------


class Proposal(BaseModel):
    action: Literal["buy_to_open", "sell_to_close"]
    symbol: str = Field(description="OCC option symbol, e.g. SPY261030C00580000")
    qty: int = Field(description="Number of contracts")
    reason: str = Field(description="The concrete, current reason for this trade")


class Decision(BaseModel):
    market_view: str = Field(description="One or two sentences on what you see right now")
    proposals: list[Proposal] = Field(description="Empty list means hold")
