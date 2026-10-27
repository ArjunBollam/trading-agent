"""Broker interface and the Alpaca paper adapter.

This is the only module that imports alpaca-py. Paper trading is not a setting:
the adapter always builds the paper client, checks the URL it ended up with, and
refuses keys that look like live keys. There is no live-trading code path.

Only paperbot/orders.py may call submit_order() or close_all(); a test enforces it.
"""

from __future__ import annotations

import time as _time
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Any, Protocol

from .models import (
    Account,
    Contract,
    MarketClock,
    OptionDetail,
    OptionSymbol,
    Order,
    OrderRequest,
    Position,
    Quote,
    StockSnap,
)


class BrokerError(Exception):
    """The broker rejected a request."""


class DuplicateOrderError(BrokerError):
    """An order with this client_order_id already exists, so this one was not placed twice."""


class LiveTradingRefused(RuntimeError):
    pass


class Broker(Protocol):
    def clock(self) -> MarketClock: ...
    def account(self) -> Account: ...
    def positions(self) -> list[Position]: ...
    def open_orders(self) -> list[Order]: ...
    def orders_since(self, after: datetime) -> list[Order]: ...
    def get_order(self, order_id: str) -> Order: ...
    def submit_order(self, req: OrderRequest) -> Order: ...
    def cancel_order(self, order_id: str) -> None: ...
    def close_all(self) -> None: ...
    def session_close(self, day: date) -> time | None: ...
    def option_quotes(self, symbols: list[str]) -> dict[str, Quote]: ...
    def option_detail(self, symbol: str) -> OptionDetail | None: ...
    def option_chain(
        self, underlying: str, exp_gte: date, exp_lte: date, strike_gte: float, strike_lte: float
    ) -> list[Contract]: ...
    def stock_snapshots(self, symbols: list[str]) -> dict[str, StockSnap]: ...
    def stock_bars(self, symbol: str, timeframe: str, limit: int, start: datetime | None = None) -> list[dict]: ...
    def news(self, symbols: list[str], limit: int) -> list[dict]: ...


def _v(x: Any) -> str:
    return x.value if isinstance(x, Enum) else str(x)


def _f(x: Any) -> float | None:
    return None if x is None else float(x)


class AlpacaPaperBroker:
    def __init__(self, api_key: str, secret_key: str, options_feed: str = "indicative", stock_feed: str = "iex"):
        from alpaca.data.historical.news import NewsClient
        from alpaca.data.historical.option import OptionHistoricalDataClient
        from alpaca.data.historical.stock import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        if not api_key or not secret_key:
            raise ValueError("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set (paper keys, see .env.example)")
        if api_key.startswith("AK"):
            raise LiveTradingRefused("This looks like a live Alpaca key (starts with AK). paperbot only trades paper.")

        # Hardcoded: paper=True. Do not make this configurable.
        self._trading = TradingClient(api_key, secret_key, paper=True)
        base = _v(getattr(self._trading, "_base_url", ""))
        if not base.startswith("https://paper-api."):
            raise LiveTradingRefused(f"Trading client is not pointed at the paper API ({base!r})")

        self._options = OptionHistoricalDataClient(api_key, secret_key)
        self._stocks = StockHistoricalDataClient(api_key, secret_key)
        self._news = NewsClient(api_key, secret_key)
        self._options_feed = options_feed
        self._stock_feed = stock_feed

    # ---- account state ------------------------------------------------------------------

    def clock(self) -> MarketClock:
        c = self._trading.get_clock()
        return MarketClock(c.timestamp, bool(c.is_open), c.next_open, c.next_close)

    def account(self) -> Account:
        a = self._trading.get_account()
        obp = a.options_buying_power if a.options_buying_power is not None else a.buying_power
        return Account(
            account_number=str(a.account_number),
            equity=float(a.equity or 0),
            last_equity=float(a.last_equity or 0),
            options_buying_power=float(obp or 0),
            options_level=int(a.options_trading_level) if a.options_trading_level is not None else None,
            trading_blocked=bool(a.trading_blocked or a.account_blocked),
        )

    def positions(self) -> list[Position]:
        out = []
        for p in self._trading.get_all_positions():
            out.append(
                Position(
                    symbol=p.symbol,
                    qty=int(float(p.qty)),
                    avg_entry_price=float(p.avg_entry_price),
                    current_price=float(p.current_price or 0),
                    asset_class=_v(p.asset_class),
                )
            )
        return out

    def open_orders(self) -> list[Order]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        rows = self._trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))
        return [self._order(o) for o in rows]

    def orders_since(self, after: datetime) -> list[Order]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        rows = self._trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, after=after, limit=500))
        return [self._order(o) for o in rows]

    def get_order(self, order_id: str) -> Order:
        return self._order(self._trading.get_order_by_id(order_id))

    # ---- order entry (called only from orders.py) -----------------------------------------

    def submit_order(self, req: OrderRequest) -> Order:
        from alpaca.common.exceptions import APIError
        from alpaca.trading.enums import OrderSide, PositionIntent, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest, StopOrderRequest

        common = dict(
            symbol=req.symbol,
            qty=req.qty,
            side=OrderSide(req.side),
            time_in_force=TimeInForce(req.time_in_force),
            client_order_id=req.client_order_id,
            # sell_to_close means a sell can never open a short position.
            position_intent=PositionIntent(req.position_intent),
        )
        if req.type == "market":
            order_data = MarketOrderRequest(**common)
        elif req.type == "limit":
            order_data = LimitOrderRequest(**common, limit_price=req.limit_price)
        elif req.type == "stop":
            order_data = StopOrderRequest(**common, stop_price=req.stop_price)
        else:
            raise BrokerError(f"unsupported order type {req.type}")
        try:
            return self._order(self._trading.submit_order(order_data=order_data))
        except APIError as e:
            msg = str(e)
            if "client_order_id" in msg and "unique" in msg:
                raise DuplicateOrderError(msg) from e
            raise BrokerError(msg) from e

    def cancel_order(self, order_id: str) -> None:
        from alpaca.common.exceptions import APIError

        try:
            self._trading.cancel_order_by_id(order_id)
        except APIError as e:
            # 422 = already filled/cancelled; the caller re-reads the order to find out which.
            if getattr(e, "status_code", None) != 422:
                raise BrokerError(str(e)) from e

    def close_all(self) -> None:
        self._trading.close_all_positions(cancel_orders=True)

    # ---- historical data for the exit simulator (read-only) ------------------------------------

    def trading_days(self, start: date, end: date) -> list[date]:
        from alpaca.trading.requests import GetCalendarRequest

        return [d.date for d in self._trading.get_calendar(GetCalendarRequest(start=start, end=end))]

    def historical_contracts(self, underlying: str, exp_gte: date, exp_lte: date, strike_gte: float,
                             strike_lte: float) -> list[tuple[str, date, float, str]]:
        """(symbol, expiry, strike, C/P) for active and expired contracts."""
        from alpaca.trading.enums import AssetStatus
        from alpaca.trading.requests import GetOptionContractsRequest

        out = []
        for status in (AssetStatus.ACTIVE, AssetStatus.INACTIVE):
            resp = self._trading.get_option_contracts(GetOptionContractsRequest(
                underlying_symbols=[underlying], status=status, expiration_date_gte=exp_gte,
                expiration_date_lte=exp_lte, strike_price_gte=str(round(strike_gte, 2)),
                strike_price_lte=str(round(strike_lte, 2)), limit=1000))
            for c in resp.option_contracts or []:
                out.append((c.symbol, c.expiration_date, float(c.strike_price), "C" if _v(c.type) == "call" else "P"))
        return out

    def option_bars(self, symbols: list[str], start: datetime, end: datetime, timeframe: str) -> dict[str, list[dict]]:
        from alpaca.data.requests import OptionBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        amount = {"1Min": 1, "5Min": 5, "15Min": 15}[timeframe]
        bars = self._options.get_option_bars(OptionBarsRequest(
            symbol_or_symbols=symbols, start=start, end=end, timeframe=TimeFrame(amount, TimeFrameUnit.Minute)))
        return {s: [{"t": b.timestamp.isoformat(), "o": b.open, "h": b.high, "l": b.low, "c": b.close, "v": b.volume}
                    for b in rows] for s, rows in bars.data.items()}

    def account_configuration(self) -> dict:
        conf = self._trading.get_account_configurations()
        values = {k: getattr(conf, k) for k in type(conf).model_fields}
        return {k: v.value if isinstance(v, Enum) else v for k, v in values.items()}  # keep booleans as booleans

    def session_close(self, day: date) -> time | None:
        """Exchange close for that day (13:00 on half days), or None if the market is closed."""
        from alpaca.trading.requests import GetCalendarRequest

        days = self._trading.get_calendar(GetCalendarRequest(start=day, end=day))
        match = [d for d in days if d.date == day]
        return match[0].close.time() if match else None

    # ---- market data (read-only) ------------------------------------------------------------

    def option_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        from alpaca.data.enums import OptionsFeed
        from alpaca.data.requests import OptionLatestQuoteRequest

        if not symbols:
            return {}
        rows = self._options.get_option_latest_quote(
            OptionLatestQuoteRequest(symbol_or_symbols=symbols, feed=OptionsFeed(self._options_feed))
        )
        return {s: Quote(s, float(q.bid_price or 0), float(q.ask_price or 0), q.timestamp) for s, q in rows.items()}

    def option_detail(self, symbol: str) -> OptionDetail | None:
        from alpaca.data.enums import OptionsFeed
        from alpaca.data.requests import OptionSnapshotRequest

        snaps = self._options.get_option_snapshot(
            OptionSnapshotRequest(symbol_or_symbols=[symbol], feed=OptionsFeed(self._options_feed))
        )
        snap = snaps.get(symbol)
        if snap is None or snap.latest_quote is None:
            return None
        q = snap.latest_quote
        quote = Quote(symbol, float(q.bid_price or 0), float(q.ask_price or 0), q.timestamp)
        oi = None
        try:  # open interest is the previous day's figure, from the contracts endpoint
            oi_raw = self._trading.get_option_contract(symbol).open_interest
            oi = int(float(oi_raw)) if oi_raw is not None else None
        except Exception:
            oi = None
        greeks = snap.greeks
        return OptionDetail(quote, _f(greeks.delta) if greeks is not None else None, _f(snap.implied_volatility), oi,
                            _f(greeks.theta) if greeks is not None else None)

    def option_chain(
        self, underlying: str, exp_gte: date, exp_lte: date, strike_gte: float, strike_lte: float
    ) -> list[Contract]:
        from alpaca.data.enums import OptionsFeed
        from alpaca.data.requests import OptionChainRequest

        rows = self._options.get_option_chain(
            OptionChainRequest(
                underlying_symbol=underlying,
                feed=OptionsFeed(self._options_feed),
                expiration_date_gte=exp_gte,
                expiration_date_lte=exp_lte,
                strike_price_gte=strike_gte,
                strike_price_lte=strike_lte,
            )
        )
        out = []
        for sym, snap in rows.items():
            occ = OptionSymbol.parse(sym)
            q = snap.latest_quote
            if occ is None or q is None:
                continue
            greeks = snap.greeks
            out.append(
                Contract(
                    symbol=sym,
                    underlying=occ.underlying,
                    right=occ.right,
                    strike=occ.strike,
                    expiry=occ.expiry,
                    bid=float(q.bid_price or 0),
                    ask=float(q.ask_price or 0),
                    iv=_f(snap.implied_volatility),
                    delta=_f(greeks.delta) if greeks is not None else None,
                    theta=_f(greeks.theta) if greeks is not None else None,
                )
            )
        return out

    def stock_snapshots(self, symbols: list[str]) -> dict[str, StockSnap]:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockSnapshotRequest

        rows = self._stocks.get_stock_snapshot(
            StockSnapshotRequest(symbol_or_symbols=symbols, feed=DataFeed(self._stock_feed))
        )
        out = {}
        for sym, s in rows.items():
            day, prev, trade = s.daily_bar, s.previous_daily_bar, s.latest_trade
            price = float(trade.price) if trade else float(day.close if day else 0)
            out[sym] = StockSnap(
                symbol=sym,
                price=price,
                prev_close=float(prev.close) if prev else price,
                day_open=float(day.open) if day else price,
                day_high=float(day.high) if day else price,
                day_low=float(day.low) if day else price,
                volume=float(day.volume) if day else 0.0,
                prev_high=float(prev.high) if prev else None,
                prev_low=float(prev.low) if prev else None,
                as_of=trade.timestamp if trade else None,
            )
        return out

    def stock_bars(self, symbol: str, timeframe: str, limit: int, start: datetime | None = None) -> list[dict]:
        """Bars oldest first: the most recent `limit`, or the first `limit` from `start` if given."""
        from alpaca.common.enums import Sort
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        amount, unit = {
            "1Min": (1, TimeFrameUnit.Minute),
            "5Min": (5, TimeFrameUnit.Minute),
            "15Min": (15, TimeFrameUnit.Minute),
            "1Hour": (1, TimeFrameUnit.Hour),
            "1Day": (1, TimeFrameUnit.Day),
        }[timeframe]
        lookback = timedelta(days=60 if unit == TimeFrameUnit.Day else 5)
        bars = self._stocks.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame(amount, unit),
                start=start or datetime.now(timezone.utc) - lookback,
                limit=limit,
                sort=Sort.ASC if start else Sort.DESC,
                feed=DataFeed(self._stock_feed),
            )
        )
        rows = bars.data.get(symbol, [])
        ordered = rows if start else list(reversed(rows))
        return [
            {"t": b.timestamp.isoformat(), "o": b.open, "h": b.high, "l": b.low, "c": b.close, "v": b.volume}
            for b in ordered
        ]

    def news(self, symbols: list[str], limit: int) -> list[dict]:
        from alpaca.data.requests import NewsRequest

        result = self._news.get_news(NewsRequest(symbols=",".join(symbols), limit=limit, exclude_contentless=True))
        items = result.data.get("news", []) if isinstance(result.data, dict) else []
        return [
            {
                "time": n.created_at.isoformat(),
                "headline": n.headline,
                "summary": (n.summary or "")[:400],
                "symbols": n.symbols,
                "source": n.source,
            }
            for n in items
        ]

    # ---- conversion ------------------------------------------------------------------------

    @staticmethod
    def _order(o: Any) -> Order:
        return Order(
            id=str(o.id),
            client_order_id=o.client_order_id or "",
            symbol=o.symbol or "",
            side=_v(o.side),
            qty=int(float(o.qty or 0)),
            filled_qty=int(float(o.filled_qty or 0)),
            type=_v(o.type or o.order_type),
            status=_v(o.status),
            limit_price=_f(o.limit_price),
            stop_price=_f(o.stop_price),
            submitted_at=o.submitted_at,
            filled_at=o.filled_at,
            filled_avg_price=_f(o.filled_avg_price),
        )


def wait_until_closed(broker: Broker, order_id: str, timeout: float = 5.0, poll: float = 0.25) -> Order:
    """Poll an order after a cancel request until it is no longer open (or time runs out)."""
    deadline = _time.monotonic() + timeout
    order = broker.get_order(order_id)
    while order.is_open and _time.monotonic() < deadline:
        _time.sleep(poll)
        order = broker.get_order(order_id)
    return order
