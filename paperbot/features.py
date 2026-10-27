"""Intraday features computed by code from 1-minute bars, so the AI reasons over numbers
instead of guessing them, and so the watcher can push real-time setup alerts.

Covered (see docs/RESEARCH.md for the evidence and its limits):
  VWAP and distance from it, the 15-minute opening range and where price sits against it,
  the return from the prior close to 10:00 (intraday momentum), RSI(14), ATR(14), EMA(9/21)
  trend, and recent volume against the session average.

On the free IEX feed, volume is IEX's share of the market only, so VWAP is approximate.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)
# Trading sessions (market time, weekdays): pre-market, the regular session and after hours.
SESSIONS = {"pre": (time(4, 0), SESSION_OPEN), "regular": (SESSION_OPEN, SESSION_CLOSE), "post": (SESSION_CLOSE, time(20, 0))}
OPENING_RANGE_END = time(9, 45)
FIRST_HALF_HOUR_END = time(10, 0)
STRONG_OPEN_ALERT_CUTOFF = time(10, 30)  # after this, a "strong open" is old news, not a setup


@dataclass(frozen=True)
class Bar:
    t: datetime
    o: float
    h: float
    l: float  # noqa: E741
    c: float
    v: float


@dataclass(frozen=True)
class IntradayFeatures:
    symbol: str
    price: float
    bars: int
    vwap: float | None
    vs_vwap_pct: float | None
    or_high: float | None
    or_low: float | None
    or_state: str | None  # above | below | inside, once the opening range is complete
    first_half_hour_pct: float | None  # prior close -> 10:00, once it's past 10:00
    rsi14: float | None
    atr14_pct: float | None
    ema9: float | None
    ema21: float | None
    trend: str | None  # up | down | flat, from EMA 9 vs 21
    volume_15m_vs_avg: float | None
    gap_pct: float | None = None  # today's open vs the prior close
    gap_filled: bool | None = None  # has price traded back to the prior close today?
    vs_prev_high_pct: float | None = None
    vs_prev_low_pct: float | None = None

    def as_prompt(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None and k != "symbol"}


def parse_bars(rows: list[dict], day, tz: ZoneInfo) -> list[Bar]:
    """Today's regular-session bars, oldest first."""
    out = []
    for r in rows:
        t = r["t"] if isinstance(r["t"], datetime) else datetime.fromisoformat(str(r["t"]).replace("Z", "+00:00"))
        t = t.astimezone(tz)
        if t.date() == day and SESSION_OPEN <= t.time() < time(16, 0):
            out.append(Bar(t, float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"]), float(r["v"])))
    return sorted(out, key=lambda b: b.t)


def ema(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    gains = [max(0.0, b - a) for a, b in zip(closes, closes[1:], strict=False)]
    losses = [max(0.0, a - b) for a, b in zip(closes, closes[1:], strict=False)]
    avg_gain, avg_loss = sum(gains[:n]) / n, sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:], strict=True):  # Wilder smoothing
        avg_gain = (avg_gain * (n - 1) + g) / n
        avg_loss = (avg_loss * (n - 1) + l) / n
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def atr(bars: list[Bar], n: int = 14) -> float | None:
    if len(bars) < n + 1:
        return None
    trs = [max(b.h - b.l, abs(b.h - p.c), abs(b.l - p.c)) for p, b in zip(bars, bars[1:], strict=False)]
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def session_of(t: datetime) -> str | None:
    """pre, regular or post for a time in market time, by the clock alone (weekends and holidays
    aren't checked); None overnight."""
    return next((name for name, (start, end) in SESSIONS.items() if start <= t.time() < end), None)


def regular(b: Bar) -> bool:
    return session_of(b.t) == "regular"


def session_start(now: datetime, tz: ZoneInfo) -> datetime:
    return datetime.combine(now.astimezone(tz).date(), SESSION_OPEN, tzinfo=tz)


def fetch_session_bars(broker, symbol: str, now: datetime, tz: ZoneInfo) -> list[dict]:
    """Today's 1-minute bars from the 9:30 open (not "the last N", which can start mid-session)."""
    return broker.stock_bars(symbol, "1Min", 420, start=session_start(now, tz))


def compute(symbol: str, rows: list[dict], prev_close: float, now: datetime, tz: ZoneInfo,
            prev_high: float | None = None, prev_low: float | None = None) -> IntradayFeatures | None:
    now = now.astimezone(tz)
    bars = parse_bars(rows, now.date(), tz)
    if not bars:
        return None
    price = bars[-1].c
    closes = [b.c for b in bars]

    volume = sum(b.v for b in bars)
    vwap = sum((b.h + b.l + b.c) / 3 * b.v for b in bars) / volume if volume > 0 else None

    or_bars = [b for b in bars if b.t.time() < OPENING_RANGE_END]
    or_high = or_low = or_state = None
    if or_bars and now.time() >= OPENING_RANGE_END:
        or_high, or_low = max(b.h for b in or_bars), min(b.l for b in or_bars)
        or_state = "above" if price > or_high else "below" if price < or_low else "inside"

    first_half = None
    early = [b for b in bars if b.t.time() < FIRST_HALF_HOUR_END]
    if early and prev_close and now.time() >= FIRST_HALF_HOUR_END:
        first_half = (early[-1].c - prev_close) / prev_close * 100

    e9, e21 = ema(closes, 9), ema(closes, 21)
    trend = None
    if e9 is not None and e21 is not None:
        trend = "up" if e9 > e21 * 1.0005 else "down" if e9 < e21 * 0.9995 else "flat"

    a = atr(bars)
    recent = bars[-15:]
    vol_ratio = None
    if len(bars) >= 30 and volume > 0:
        vol_ratio = (sum(b.v for b in recent) / len(recent)) / (volume / len(bars))

    def r(x: float | None, nd: int = 2) -> float | None:
        return None if x is None else round(x, nd)

    # Gaps: small SPY gaps have filled the same day most of the time; large ones much less often.
    gap = filled = None
    if prev_close:
        gap = (bars[0].o - prev_close) / prev_close * 100
        if gap > 0:
            filled = min(b.l for b in bars) <= prev_close
        elif gap < 0:
            filled = max(b.h for b in bars) >= prev_close

    return IntradayFeatures(
        symbol=symbol, price=price, bars=len(bars),
        vwap=r(vwap), vs_vwap_pct=r((price - vwap) / vwap * 100 if vwap else None),
        or_high=r(or_high), or_low=r(or_low), or_state=or_state,
        first_half_hour_pct=r(first_half), rsi14=r(rsi(closes), 1),
        atr14_pct=r(a / price * 100 if a is not None and price else None, 3),
        ema9=r(e9), ema21=r(e21), trend=trend, volume_15m_vs_avg=r(vol_ratio),
        gap_pct=r(gap), gap_filled=filled,
        vs_prev_high_pct=r((price - prev_high) / prev_high * 100 if prev_high else None),
        vs_prev_low_pct=r((price - prev_low) / prev_low * 100 if prev_low else None),
    )


@dataclass(frozen=True)
class Signal:
    symbol: str
    kind: str  # orb_up | orb_down | strong_open_up | strong_open_down
    message: str


def detect_signals(f: IntradayFeatures, first_half_hour_threshold_pct: float, now: datetime) -> list[Signal]:
    """Setups worth a phone alert. Each kind fires at most once per symbol per day (the caller dedupes).
    `now` must be market time: the strong-open alert only fires before STRONG_OPEN_ALERT_CUTOFF."""
    out = []
    vwap_note = ""
    if f.vs_vwap_pct is not None:
        vwap_note = f", {'above' if f.vs_vwap_pct >= 0 else 'below'} VWAP {f.vwap}"
    if f.or_state == "above":
        out.append(Signal(f.symbol, "orb_up", f"{f.symbol} {f.price} broke above its opening range high {f.or_high}{vwap_note}."))
    elif f.or_state == "below":
        out.append(Signal(f.symbol, "orb_down", f"{f.symbol} {f.price} broke below its opening range low {f.or_low}{vwap_note}."))
    if (f.first_half_hour_pct is not None and abs(f.first_half_hour_pct) >= first_half_hour_threshold_pct
            and now.time() < STRONG_OPEN_ALERT_CUTOFF):
        kind = "strong_open_up" if f.first_half_hour_pct > 0 else "strong_open_down"
        out.append(Signal(f.symbol, kind, f"{f.symbol} moved {f.first_half_hour_pct:+.2f}% from yesterday's close "
                                          f"by 10:00. Research finds a weak tendency for the last half-hour to follow."))
    return out
