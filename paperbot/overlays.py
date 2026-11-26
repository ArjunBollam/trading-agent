"""Two more popular TradingView ideas, worked out here from their published rules, for looking at on the charts only:

- Support and resistance breaks: the last confirmed pivot high and low, and candles that close through one, either on
  rising volume (a break) or with a long wick against the move (a wick break). Rules and defaults as in LuxAlgo's
  "Support and Resistance Levels with Breaks" (15 bars each side, volume threshold 20%).
- Squeeze: Bollinger Bands inside Keltner Channels mean a quiet market. When they come back out, the squeeze has
  released, in the direction of momentum (a linear regression of price against its 20-candle middle). Rules and defaults
  as in LazyBear's "Squeeze Momentum Indicator", with the release read the way John Carter does.

Signals use closed candles only and never change later. Nothing here is alerted, and no order depends on it."""

from __future__ import annotations

from dataclasses import dataclass

from .features import Bar
from .patterns import ema_series


@dataclass(frozen=True)
class LevelBreak:
    index: int
    side: str  # long: closed above resistance | short: closed below support
    kind: str  # break (on volume) | wick (a long wick against the move)
    level: float
    volume: float  # the volume oscillator, %: fast volume average against the slow one
    gap: bool = False  # the session opened past the level: the overnight gap broke it, not this candle


@dataclass(frozen=True)
class Release:
    index: int
    side: str  # long | short: momentum above or below zero
    momentum: float
    candles: int  # how long the squeeze lasted
    gap: bool = False  # on the session's first candle, after a big overnight gap


def first_of_session(bars: list[Bar], i: int) -> bool:
    return i > 0 and bars[i].t.date() != bars[i - 1].t.date()


def overnight_gap(bars: list[Bar], i: int, length: int = 20) -> bool:
    """Whether candle `i` is its session's first and opened further from the last close than a usual candle's range."""
    if not first_of_session(bars, i):
        return False
    before = bars[max(0, i - length):i]
    return abs(bars[i].o - bars[i - 1].c) > sum(b.h - b.l for b in before) / len(before)


def pivot_levels(bars: list[Bar], left: int = 15, right: int = 15) -> list[tuple[float | None, float | None]]:
    """The resistance and support in force on each candle: the last pivot high and low. A pivot needs `right` candles
    after it, and counts from the candle after that."""
    highs, lows = [b.h for b in bars], [b.l for b in bars]
    found_hi: list[float | None] = [None] * len(bars)
    found_lo: list[float | None] = [None] * len(bars)
    for i in range(left + right, len(bars)):
        j = i - right
        if highs[j] >= max(highs[j - left:j]) and highs[j] > max(highs[j + 1:i + 1]):
            found_hi[i] = highs[j]
        if lows[j] <= min(lows[j - left:j]) and lows[j] < min(lows[j + 1:i + 1]):
            found_lo[i] = lows[j]
    out, hi, lo = [], None, None
    for i in range(len(bars)):
        if i:
            hi = found_hi[i - 1] if found_hi[i - 1] is not None else hi
            lo = found_lo[i - 1] if found_lo[i - 1] is not None else lo
        out.append((hi, lo))
    return out


def level_breaks(bars: list[Bar], left: int = 15, right: int = 15, volume: float = 20.0,
                 levels: list[tuple[float | None, float | None]] | None = None) -> list[LevelBreak]:
    levels = levels if levels is not None else pivot_levels(bars, left, right)
    vols = [b.v for b in bars]
    fast, slow = ema_series(vols, 5), ema_series(vols, 10)
    out = []
    for i in range(1, len(bars)):
        b, prev = bars[i], bars[i - 1]
        (hi, lo), (hi1, lo1) = levels[i], levels[i - 1]
        osc = 100 * (fast[i] - slow[i]) / slow[i] if fast[i] is not None and slow[i] else 0.0
        opening = first_of_session(bars, i)
        if hi is not None and hi1 is not None and b.c > hi and prev.c <= hi1:
            if b.o - b.l > b.c - b.o:
                out.append(LevelBreak(i, "long", "wick", hi, osc, opening and b.o > hi))
            elif osc > volume:
                out.append(LevelBreak(i, "long", "break", hi, osc, opening and b.o > hi))
        if lo is not None and lo1 is not None and b.c < lo and prev.c >= lo1:
            if b.h - b.o > b.o - b.c:
                out.append(LevelBreak(i, "short", "wick", lo, osc, opening and b.o < lo))
            elif osc > volume:
                out.append(LevelBreak(i, "short", "break", lo, osc, opening and b.o < lo))
    return out


def _regression_end(values: list[float]) -> float:
    """The value of the least-squares line through `values` at the last one."""
    n = len(values)
    mid, mean = (n - 1) / 2, sum(values) / n
    slope = sum((i - mid) * (v - mean) for i, v in enumerate(values)) / sum((i - mid) ** 2 for i in range(n))
    return mean + slope * (n - 1 - mid)


def squeeze(bars: list[Bar], length: int = 20, bb_mult: float = 1.5, kc_mult: float = 1.5) -> tuple[list[Release], int]:
    """Every release, and how many candles the squeeze has been on for at the last candle (0: it is off)."""
    closes = [b.c for b in bars]
    ranges = [b.h - b.l if i == 0 else max(b.h - b.l, abs(b.h - closes[i - 1]), abs(b.l - closes[i - 1])) for i, b in enumerate(bars)]
    gaps: list[float] = []  # close minus the middle of the last `length` candles, from candle length - 1 on
    out: list[Release] = []
    on = 0
    for i in range(length - 1, len(bars)):
        w = closes[i - length + 1:i + 1]
        mean = sum(w) / length
        window = bars[i - length + 1:i + 1]
        middle = (max(b.h for b in window) + min(b.l for b in window)) / 2
        gaps.append(closes[i] - (middle + mean) / 2)
        band = bb_mult * (sum((c - mean) ** 2 for c in w) / length) ** 0.5
        channel = kc_mult * sum(ranges[i - length + 1:i + 1]) / length
        if band < channel:
            on += 1
            continue
        if on and band > channel and len(gaps) >= length:
            momentum = _regression_end(gaps[-length:])
            if momentum:
                out.append(Release(i, "long" if momentum > 0 else "short", momentum, on, overnight_gap(bars, i, length)))
        on = 0
    return out, on
