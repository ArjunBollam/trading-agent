"""Candles and trades for the Charts tab and the alerts, from Alpaca, kept in memory so a chart can switch interval or
type without fetching again.

On the free data plan the whole market's (SIP) candles and trades are only available once they're 15 minutes old.
So a chart's candles come from the SIP up to 16 minutes ago, and the newest minutes from IEX (one exchange): the
prices are live and right, but those minutes' volume is IEX's share only. The response says from when."""

from __future__ import annotations

import threading
import time as clock
from datetime import date, datetime, time, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from .charting import Print
from .features import Bar

SIP_DELAY = timedelta(minutes=16)
# trade conditions that don't set the last price (average-price, out-of-sequence, corrected, official open/close...)
NOT_LAST_SALE = frozenset({"B", "C", "G", "H", "M", "N", "P", "Q", "R", "V", "W", "Z", "4", "7", "9"})


def _time(x) -> datetime:
    return x if isinstance(x, datetime) else datetime.fromisoformat(str(x).replace("Z", "+00:00"))


def _bars(rows, tz: ZoneInfo) -> list[Bar]:
    return [Bar(_time(r["t"]).astimezone(tz), float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"]), float(r["v"])) for r in rows]


def weekdays(end: date, n: int) -> list[date]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


class AlpacaMarket:
    """The raw calls: 1-minute and daily candles and trades, from the SIP or IEX."""

    def __init__(self, api_key: str, secret_key: str):
        from alpaca.data.historical.stock import StockHistoricalDataClient

        if not api_key or not secret_key:
            raise ValueError("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set")
        self._client = StockHistoricalDataClient(api_key, secret_key, raw_data=True)

    def bars(self, symbol: str, timeframe: str, start: datetime, end: datetime, feed: str, tz: ZoneInfo) -> list[Bar]:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        if start >= end:
            return []
        tf = TimeFrame.Minute if timeframe == "1Min" else TimeFrame.Day
        data = self._client.get_stock_bars(StockBarsRequest(symbol_or_symbols=symbol, timeframe=tf, start=start, end=end,
                                                            feed=DataFeed(feed), adjustment=Adjustment.SPLIT))
        return _bars(data.get(symbol, []) if isinstance(data, dict) else [], tz)

    def trades(self, symbol: str, start: datetime, end: datetime, feed: str) -> list[dict]:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockTradesRequest

        if start >= end:
            return []
        data = self._client.get_stock_trades(StockTradesRequest(symbol_or_symbols=symbol, start=start, end=end, feed=DataFeed(feed)))
        return list(data.get(symbol, [])) if isinstance(data, dict) else []


class Market:
    """What the charts and alerts ask for, cached: finished sessions for good, today's for a few seconds."""

    def __init__(self, source, tz: ZoneInfo, now: Callable[[], datetime] | None = None, fresh_seconds: float = 4.0,
                 night_source=None):
        self.source, self.tz = source, tz
        self.night_source = night_source  # Robinhood's 24-hour market candles (read-only), for overnight charts and alerts
        self._night: dict[str, tuple[float, list[Bar]]] = {}
        self.now = now or (lambda: datetime.now(tz))
        self.fresh = fresh_seconds
        self._days: dict[tuple[str, date], list[Bar]] = {}  # finished sessions' 1-minute candles
        self._today: dict[str, tuple[float, list[Bar], datetime | None]] = {}  # (fetched at, candles, IEX from)
        self._daily: dict[str, tuple[float, list[Bar]]] = {}
        self._tapes: dict[tuple[str, date], tuple[list[Print], datetime | None, float]] = {}
        self._lock = threading.RLock()

    # ----- 1-minute candles -----

    def minutes(self, symbol: str, sessions: int = 1) -> tuple[list[Bar], datetime | None]:
        """1-minute candles (4:00 to 20:00) for the last `sessions` weekdays up to now, and the time from which the
        volume is IEX's only (None if none is)."""
        symbol = symbol.upper()
        now = self.now().astimezone(self.tz)
        days = weekdays(now.date(), max(sessions, 1) + 2 + sessions // 20)  # a holiday or two in between
        with self._lock:
            past = [d for d in days if d < now.date() and (symbol, d) not in self._days]
            if past:
                start = datetime.combine(past[0], time(0), tzinfo=self.tz)
                end = min(datetime.combine(past[-1] + timedelta(days=1), time(0), tzinfo=self.tz), now - SIP_DELAY)
                got = self.source.bars(symbol, "1Min", start, end, "sip", self.tz)
                for d in past:
                    if datetime.combine(d + timedelta(days=1), time(0), tzinfo=self.tz) <= now - SIP_DELAY:
                        self._days[(symbol, d)] = [b for b in got if b.t.date() == d]
                    else:  # yesterday evening, still inside the delay: keep it out of the cache for now
                        self._days.pop((symbol, d), None)
            out: list[Bar] = []
            for d in days:
                if d < now.date():
                    out += self._days.get((symbol, d), [])
            iex_from = None
            if days[-1] == now.date():
                today, iex_from = self._today_minutes(symbol, now)
                out += today
        kept = sorted({b.t.date() for b in out})[-max(sessions, 1):]
        return ([b for b in out if b.t.date() >= kept[0]] if kept else []), iex_from

    def _today_minutes(self, symbol: str, now: datetime) -> tuple[list[Bar], datetime | None]:
        hit = self._today.get(symbol)
        if hit and clock.monotonic() - hit[0] < self.fresh and hit[1] and hit[1][-1].t.date() == now.date():
            return hit[1], hit[2]
        midnight = datetime.combine(now.date(), time(0), tzinfo=self.tz)
        cut = max(midnight, (now - SIP_DELAY).replace(second=0, microsecond=0))
        sip = self.source.bars(symbol, "1Min", midnight, cut, "sip", self.tz) if cut > midnight else []
        iex = self.source.bars(symbol, "1Min", cut, now + timedelta(minutes=1), "iex", self.tz)
        last = sip[-1].t if sip else None
        bars = sip + [b for b in iex if last is None or b.t > last]
        iex_from = next((b.t for b in bars if b.t >= cut), None) if iex else None
        self._today[symbol] = (clock.monotonic(), bars, iex_from)
        return bars, iex_from

    # ----- daily candles -----

    def daily(self, symbol: str, years: float = 5) -> list[Bar]:
        """Split-adjusted daily candles, with today's from today's regular-hours minutes."""
        symbol = symbol.upper()
        now = self.now().astimezone(self.tz)
        with self._lock:
            hit = self._daily.get(symbol)
            if not hit or clock.monotonic() - hit[0] > 300:
                start = datetime.combine(now.date() - timedelta(days=int(365.25 * years) + 7), time(0), tzinfo=self.tz)
                rows = self.source.bars(symbol, "1Day", start, now - SIP_DELAY, "sip", self.tz)
                hit = (clock.monotonic(), [Bar(datetime.combine(b.t.date(), time(0), tzinfo=self.tz), b.o, b.h, b.l, b.c, b.v) for b in rows])
                self._daily[symbol] = hit
            out = [b for b in hit[1] if b.t.date() < now.date()]
        if now.weekday() < 5:
            minutes, _ = self.minutes(symbol, 1)
            rth = [b for b in minutes if b.t.date() == now.date() and time(9, 30) <= b.t.time() < time(16)]
            if rth:
                out.append(Bar(datetime.combine(now.date(), time(0), tzinfo=self.tz), rth[0].o, max(b.h for b in rth),
                               min(b.l for b in rth), rth[-1].c, sum(b.v for b in minutes if b.t.date() == now.date())))
        return out

    # ----- trades -----

    def prints(self, symbol: str, day: date) -> list[Print]:
        """IEX trades for one day, 4:00 to 20:00 (live on the free plan), topped up with only the new ones."""
        symbol = symbol.upper()
        now = self.now().astimezone(self.tz)
        start = datetime.combine(day, time(4, 0), tzinfo=self.tz)
        end = min(datetime.combine(day, time(20, 0), tzinfo=self.tz), now)
        with self._lock:
            got, last, at = self._tapes.get((symbol, day), ([], None, 0.0))
            if last is not None and (last >= end - timedelta(seconds=1) or clock.monotonic() - at < self.fresh):
                return got
            frm = start if last is None else last + timedelta(microseconds=1)
            while frm < end:
                stop = min(frm + timedelta(minutes=60), end)
                for r in self.source.trades(symbol, frm, stop, "iex"):
                    if NOT_LAST_SALE & set(r.get("c") or ()) or float(r.get("s") or 0) <= 0:
                        continue
                    got.append(Print(_time(r["t"]).astimezone(self.tz), float(r["p"]), float(r["s"])))
                frm = stop
            got.sort(key=lambda p: p.t)
            self._tapes[(symbol, day)] = (got, end, clock.monotonic())
            return got

    def night(self, symbol: str, sessions: int = 2, minutes: int = 5) -> list[Bar]:
        """1- or 5-minute candles around the clock (Robinhood's 24-hour market: Sunday 20:00 to Friday 20:00), covering
        the last `sessions` days of trading time. Fetched again at most every 20 seconds, only the newest part."""
        from .trendlines import market_clock

        if self.night_source is None:
            raise ValueError("overnight candles come from Robinhood: set ROBINHOOD_MCP_URL and sign in with `paperbot robinhood-login`")
        symbol = symbol.upper()
        now = self.now().astimezone(self.tz)
        start = now
        while market_clock(now, "24h") - market_clock(start, "24h") < sessions * 86400 and now - start < timedelta(days=21):
            start -= timedelta(hours=1)
        with self._lock:
            hit = self._night.get((symbol, minutes))
            if hit and clock.monotonic() - hit[0] < 20 and hit[1] and hit[1][0].t <= start + timedelta(minutes=5):
                bars = hit[1]
            else:
                got: dict[datetime, Bar] = {b.t: b for b in (hit[1] if hit else [])}
                frm = start if not got or min(got) > start + timedelta(minutes=5) else max(got) - timedelta(minutes=30)
                while frm < now:
                    # a call's answer must stay under Robinhood's 1 MB: a week of 5-minute candles, 12 hours of 1-minute
                    stop = min(frm + (timedelta(days=7) if minutes == 5 else timedelta(hours=12)), now + timedelta(minutes=5))
                    for b in self.night_source.long_bars(symbol, minutes, frm, stop, "24_5"):
                        t = b.t.astimezone(self.tz)
                        got[t] = Bar(t, b.o, b.h, b.l, b.c, b.v)
                    frm = stop
                bars = [got[t] for t in sorted(got)]
                self._night[(symbol, minutes)] = (clock.monotonic(), bars)
        return [b for b in bars if b.t >= start - timedelta(minutes=5)]

    def last_price(self, symbol: str) -> tuple[float | None, float | None]:
        """(last price, the previous regular session's close)."""
        bars, _ = self.minutes(symbol, 2)
        if not bars:
            return None, None
        today = bars[-1].t.date()
        before = [b for b in bars if b.t.date() < today and time(9, 30) <= b.t.time() < time(16)]
        return bars[-1].c, (before[-1].c if before else None)
