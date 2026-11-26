"""Smart money concepts, for looking at on the charts only, worked out here from the published rules and defaults of
LuxAlgo's "Smart Money Concepts (SMC)":

- Market structure: pivots on two scales, swing (50 candles) and internal (5). A close through the last pivot is a break of
  structure (BOS) when it goes with that scale's trend, a change of character (CHoCH) when it turns it.
- Order blocks: the extreme candle between a broken pivot and its break. A block is dropped once price trades through it.
- Equal highs and lows: two pivots in a row (3 candles) within a tenth of the average range.
- Strong and weak highs and lows, and the premium, equilibrium and discount zones between the swing extremes.
- Fair value gaps: three candles that leave a gap behind a strong middle one, until price trades through it.

A pivot counts once its candles have closed, and every signal uses closed candles only, so none changes later. Nothing
here is alerted, and no order depends on it."""

from __future__ import annotations

from dataclasses import dataclass, field

from .features import Bar
from .overlays import first_of_session

SWING, INTERNAL, EQUAL, ATR_LENGTH = 50, 5, 3, 200


@dataclass(frozen=True)
class Structure:
    index: int  # the candle that closed through the pivot
    pivot: int  # the pivot's candle
    level: float
    side: str  # long: broke a high | short: broke a low
    kind: str  # BOS | CHoCH
    internal: bool
    gap: bool = False  # the session opened past the level


@dataclass(frozen=True)
class Block:
    index: int  # the candle it comes from
    top: float
    bottom: float
    side: str  # long: demand, under a break up | short: supply, over a break down
    internal: bool


@dataclass(frozen=True)
class Equal:
    first: int
    second: int
    levels: tuple[float, float]
    side: str  # high | low
    known: int  # the candle it was confirmed on


@dataclass(frozen=True)
class FairGap:
    index: int  # the middle candle
    top: float
    bottom: float
    side: str


@dataclass
class _Pivot:
    level: float | None = None
    index: int = 0
    broken: bool = False


@dataclass
class _Scale:
    """One scale's alternating legs, and its last pivot high and low."""

    size: int
    leg: int = 0
    high: _Pivot = field(default_factory=_Pivot)
    low: _Pivot = field(default_factory=_Pivot)
    bias: int = 0  # +1 after breaking a high, -1 after breaking a low

    def step(self, bars: list[Bar], i: int) -> str | None:
        """Which pivot candle `i` confirmed: the high or the low `size` candles back, or neither."""
        j = i - self.size
        if j < 0:
            return None
        after = bars[j + 1:i + 1]
        leg = self.leg
        if bars[j].h > max(b.h for b in after):
            leg = 0
        elif bars[j].l < min(b.l for b in after):
            leg = 1
        changed, self.leg = leg != self.leg, leg
        return None if not changed else "low" if leg == 1 else "high"


@dataclass
class SmartMoney:
    structures: list[Structure] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)  # still standing, newest first
    equals: list[Equal] = field(default_factory=list)
    gaps: list[FairGap] = field(default_factory=list)  # still open, newest first
    swing_bias: int = 0
    internal_bias: int = 0
    top: float | None = None  # the swing range, followed as price makes new extremes
    top_index: int = 0
    bottom: float | None = None
    bottom_index: int = 0
    range_index: int = 0  # the last swing pivot's candle

    def zones(self) -> dict[str, tuple[float, float]]:
        """(top, bottom) of the premium, equilibrium and discount zones."""
        if self.top is None or self.bottom is None:
            return {}
        t, b = self.top, self.bottom
        return {"premium": (t, 0.95 * t + 0.05 * b), "equilibrium": (0.525 * t + 0.475 * b, 0.475 * t + 0.525 * b),
                "discount": (0.05 * t + 0.95 * b, b)}


def _atr(bars: list[Bar], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    total, value = 0.0, 0.0
    for i, b in enumerate(bars):
        tr = b.h - b.l if i == 0 else max(b.h - b.l, abs(b.h - bars[i - 1].c), abs(b.l - bars[i - 1].c))
        if i < n:
            total += tr
            if i == n - 1:
                value = out[i] = total / n
        else:
            value = out[i] = (value * (n - 1) + tr) / n
    return out


def smart_money(bars: list[Bar], swing: int = SWING, internal: int = INTERNAL, equal: int = EQUAL, threshold: float = 0.1) -> SmartMoney:
    out = SmartMoney()
    big, small, eq = _Scale(swing), _Scale(internal), _Scale(equal)
    atr = _atr(bars, ATR_LENGTH)
    # a very wide candle's high and low are swapped, so it isn't picked as an order block
    wide = [a is not None and b.h - b.l >= 2 * a for a, b in zip(atr, bars, strict=True)]
    highs = [b.l if w else b.h for b, w in zip(bars, wide, strict=True)]
    lows = [b.h if w else b.l for b, w in zip(bars, wide, strict=True)]
    moves = 0.0
    before: dict[int, tuple[float | None, float | None]] = {}  # each scale's levels on the previous candle

    for i, b in enumerate(bars):
        if out.top is not None and b.h >= out.top:
            out.top, out.top_index = b.h, i
        if out.bottom is not None and b.l <= out.bottom:
            out.bottom, out.bottom_index = b.l, i
        out.gaps = [g for g in out.gaps if not (b.l < g.bottom if g.side == "long" else b.h > g.top)]

        for scale in (big, small, eq):
            found = scale.step(bars, i)
            if found is None:
                continue
            j = i - scale.size
            level, p = (bars[j].h, scale.high) if found == "high" else (bars[j].l, scale.low)
            if scale is eq and p.level is not None and atr[i] is not None and abs(p.level - level) < threshold * atr[i]:
                out.equals.append(Equal(p.index, j, (p.level, level), found, i))
            p.level, p.index, p.broken = level, j, False
            if scale is big:
                out.range_index = j
                if found == "high":
                    out.top, out.top_index = level, j
                else:
                    out.bottom, out.bottom_index = level, j

        opening = first_of_session(bars, i)
        for scale in (small, big):
            inner = scale is small
            hi1, lo1 = before.get(id(scale), (None, None))
            prev = bars[i - 1].c if i else b.c
            p = scale.high
            if (p.level is not None and hi1 is not None and not p.broken and b.c > p.level and prev <= hi1
                    and not (inner and p.level == big.high.level)):
                out.structures.append(Structure(i, p.index, p.level, "long", "CHoCH" if scale.bias < 0 else "BOS", inner,
                                                opening and b.o > p.level))
                p.broken, scale.bias = True, 1
                k = min(range(p.index, i), key=lambda n: lows[n])
                out.blocks.insert(0, Block(k, highs[k], lows[k], "long", inner))
            p = scale.low
            if (p.level is not None and lo1 is not None and not p.broken and b.c < p.level and prev >= lo1
                    and not (inner and p.level == big.low.level)):
                out.structures.append(Structure(i, p.index, p.level, "short", "CHoCH" if scale.bias > 0 else "BOS", inner,
                                                opening and b.o < p.level))
                p.broken, scale.bias = True, -1
                k = max(range(p.index, i), key=lambda n: highs[n])
                out.blocks.insert(0, Block(k, highs[k], lows[k], "short", inner))
            before[id(scale)] = (scale.high.level, scale.low.level)
        out.blocks = [x for x in out.blocks if not (b.l < x.bottom if x.side == "long" else b.h > x.top)]

        if i >= 2:
            mid, first = bars[i - 1], bars[i - 2]
            move = (mid.c - mid.o) / mid.o
            moves += abs(move)
            usual = 2 * moves / i
            if b.l > first.h and mid.c > first.h and move > usual:
                out.gaps.insert(0, FairGap(i - 1, b.l, first.h, "long"))
            elif b.h < first.l and mid.c < first.l and -move > usual:
                out.gaps.insert(0, FairGap(i - 1, first.l, b.h, "short"))

    out.swing_bias, out.internal_bias = big.bias, small.bias
    return out
