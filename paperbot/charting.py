"""The Charts tab's candles: any interval (seconds, minutes, hours, days, weeks, months, ticks, price ranges) and every
chart type TradingView's top plan has (Heikin Ashi, Renko, line break, Kagi, point & figure), plus the volume profile,
TPO (market profile) and footprint drawn over them. Pure functions: what goes in is candles or trades, what comes out
is candles, columns or rows; nothing here fetches anything.

Each rule follows the chart type's textbook definition. Charts that aren't drawn against time (ticks, ranges, Renko,
line break, Kagi, point & figure) keep each piece's real time, but can put several pieces on the same second."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .features import Bar

REGULAR_OPEN, REGULAR_CLOSE = time(9, 30), time(16, 0)
EXTENDED_OPEN = time(4, 0)


@dataclass(frozen=True)
class Interval:
    """s seconds, m minutes, D days, W weeks, M months (all drawn against time), T trades, R price range."""

    unit: str
    size: float

    @property
    def label(self) -> str:
        n = int(self.size) if self.unit != "R" else self.size
        if self.unit == "m" and self.size % 60 == 0:
            return f"{int(self.size // 60)}h"
        return {"s": f"{n}s", "m": f"{n}m", "D": f"{n}D", "W": f"{n}W", "M": f"{n}M", "T": f"{n}T", "R": f"R{n:g}"}[self.unit]

    @property
    def timed(self) -> bool:
        return self.unit in "smDWM"

    @property
    def needs_trades(self) -> bool:
        return self.unit in "sTR"


_UNITS = {"s": "s", "sec": "s", "S": "s", "m": "m", "min": "m", "h": "h", "H": "h", "hr": "h", "d": "D", "D": "D",
          "w": "W", "W": "W", "M": "M", "mo": "M", "mon": "M", "t": "T", "T": "T", "tick": "T", "ticks": "T", "r": "R", "R": "R"}


def parse_interval(text: str) -> Interval:
    """"30s", "1", "5m", "90", "2h", "D", "1W", "3M", "100T", "R0.5" or "0.5R". A bare number is minutes, like
    TradingView; capital M is months, small m minutes."""
    s = str(text or "").strip()
    m = re.fullmatch(r"([rR])\s*(\d+(?:\.\d+)?)", s)
    if m:
        size, unit = float(m.group(2)), "R"
    else:
        m = re.fullmatch(r"(\d+(?:\.\d+)?)?\s*([A-Za-z]+)?", s)
        if not m or not s:
            raise ValueError(f"not an interval: {text!r}")
        size = float(m.group(1)) if m.group(1) else 1.0
        word = m.group(2) or "m"
        unit = _UNITS.get(word) or _UNITS.get(word.lower())
        if unit is None:
            raise ValueError(f"not an interval: {text!r}")
    if unit == "h":
        unit, size = "m", size * 60
    if size <= 0:
        raise ValueError("an interval must be more than zero")
    if unit != "R" and size != int(size):
        raise ValueError(f"{text!r}: use a whole number")
    limits = {"s": 3600, "m": 1440, "D": 365, "W": 52, "M": 12, "T": 100_000, "R": 1e6}
    if size > limits[unit]:
        raise ValueError(f"{text!r} is too long")
    if unit == "m" and size == 1440:
        unit, size = "D", 1.0
    return Interval(unit, size)


# ---------- time-based candles ----------

def regular(t: datetime) -> bool:
    return t.weekday() < 5 and REGULAR_OPEN <= t.time() < REGULAR_CLOSE


def merge(rows: list[Bar], t: datetime | None = None) -> Bar:
    return Bar(t or rows[0].t, rows[0].o, max(b.h for b in rows), min(b.l for b in rows), rows[-1].c, sum(b.v for b in rows))


def resample_minutes(bars: list[Bar], minutes: int, extended: bool = True, around_the_clock: bool = False) -> list[Bar]:
    """1-minute (or 5-minute) candles into `minutes`-minute ones, each session counted from its open (4:00 with extended
    hours, 9:30 without, midnight for the 24-hour market), the way TradingView lines them up. Without extended hours,
    candles outside 9:30-16:00 are dropped."""
    groups: dict[datetime, list[Bar]] = {}
    for b in bars:
        if not extended and not regular(b.t):
            continue
        anchor = time(0) if around_the_clock else EXTENDED_OPEN if extended else REGULAR_OPEN
        start = datetime.combine(b.t.date(), anchor, tzinfo=b.t.tzinfo)
        k = int((b.t - start).total_seconds() // 60) // minutes * minutes
        groups.setdefault(start + timedelta(minutes=k), []).append(b)
    return [merge(v, t) for t, v in sorted(groups.items())]


def resample_days(daily: list[Bar], unit: str, size: int = 1) -> list[Bar]:
    """Daily candles into n-day, weekly (from Monday) or monthly ones."""
    if unit == "D" and size == 1:
        return list(daily)
    groups: dict[object, list[Bar]] = defaultdict(list)
    for i, b in enumerate(daily):
        d = b.t.date()
        if unit == "W":
            monday = d - timedelta(days=d.weekday())
            key = (monday.toordinal() - date(1970, 1, 5).toordinal()) // 7 // size
        elif unit == "M":
            key = (d.year * 12 + d.month - 1) // size
        else:
            key = i // size
        groups[key].append(b)
    return [merge(v) for _, v in sorted(groups.items(), key=lambda kv: kv[1][0].t)]


@dataclass(frozen=True, slots=True)
class Print:
    t: datetime
    price: float
    size: float


def second_bars(prints: list[Print], seconds: int) -> list[Bar]:
    """Trades into n-second candles; a stretch with no trades has no candle, as on TradingView."""
    groups: dict[int, list[Print]] = {}
    for p in prints:
        groups.setdefault(int(p.t.timestamp()) // seconds * seconds, []).append(p)
    out = []
    for k, ps in sorted(groups.items()):
        t = datetime.fromtimestamp(k, ps[0].t.tzinfo)
        out.append(Bar(t, ps[0].price, max(p.price for p in ps), min(p.price for p in ps), ps[-1].price, sum(p.size for p in ps)))
    return out


def tick_bars(prints: list[Print], n: int) -> list[Bar]:
    """A candle every n trades (the last one may be short: it's still forming)."""
    out = []
    for i in range(0, len(prints), n):
        ps = prints[i:i + n]
        out.append(Bar(ps[0].t, ps[0].price, max(p.price for p in ps), min(p.price for p in ps), ps[-1].price, sum(p.size for p in ps)))
    return out


def range_bars(prints: list[Print], size: float) -> list[Bar]:
    """A new candle each time price has covered `size` from its low to its high. A candle closes at the edge of its
    range, the next one opens there, and a jump wider than one range leaves candles with no trades in them."""
    out: list[Bar] = []
    cur: list | None = None  # t, o, h, l, c, v
    eps = size * 1e-9
    for p in prints:
        if cur is None:
            cur = [p.t, p.price, p.price, p.price, p.price, p.size]
            continue
        while True:
            h, l = max(cur[2], p.price), min(cur[3], p.price)  # noqa: E741
            if h - l <= size + eps:
                cur[2], cur[3], cur[4] = h, l, p.price
                cur[5] += p.size
                break
            edge = cur[3] + size if p.price > cur[2] else cur[2] - size
            if p.price > cur[2]:
                cur[2] = edge
            else:
                cur[3] = edge
            cur[4] = edge
            out.append(Bar(*cur))
            cur = [p.t, edge, edge, edge, edge, 0.0]
    if cur is not None:
        out.append(Bar(*cur))
    return out


# ---------- chart types ----------

def heikin_ashi(bars: list[Bar]) -> list[Bar]:
    out: list[Bar] = []
    for b in bars:
        c = (b.o + b.h + b.l + b.c) / 4
        o = (b.o + b.c) / 2 if not out else (out[-1].o + out[-1].c) / 2
        out.append(Bar(b.t, o, max(b.h, o, c), min(b.l, o, c), c, b.v))
    return out


def atr_value(bars: list[Bar], n: int = 14) -> float | None:
    """The last ATR (Wilder's), for sizing boxes the way TradingView does by default."""
    if len(bars) < 2:
        return None
    trs = [bars[0].h - bars[0].l] + [max(b.h - b.l, abs(b.h - a.c), abs(b.l - a.c)) for a, b in zip(bars, bars[1:])]
    if len(trs) < n:
        return sum(trs) / len(trs)
    a = sum(trs[:n]) / n
    for x in trs[n:]:
        a = (a * (n - 1) + x) / n
    return a


def nice_step(raw: float) -> float:
    """The next tidy size up from `raw`: 0.01, 0.02, 0.05, 0.1, 0.25, 0.5, 1, 2, 2.5, 5, 10..."""
    if raw <= 0 or not math.isfinite(raw):
        return 0.01
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if m * mag >= raw - 1e-12:
            return round(max(m * mag, 0.01), 10)
    return round(10 * mag, 10)


def box_size(bars: list[Bar], box: float | None) -> float:
    if box and box > 0:
        return float(box)
    a = atr_value(bars) or (bars[-1].c * 0.001 if bars else 1.0)
    return nice_step(a)


def renko(bars: list[Bar], box: float) -> list[Bar]:
    """Traditional Renko on closes: a brick each time the close moves one box past the last brick, a turn only after
    it moves two boxes back (past the far side of the last brick). Each brick carries the volume since the one before."""
    if not bars:
        return []
    out: list[Bar] = []
    anchor = math.floor(bars[0].c / box) * box
    vol = 0.0
    for b in bars:
        vol += b.v
        while True:
            if not out:
                top, bottom, up = anchor, anchor, None
            else:
                last = out[-1]
                up = last.c > last.o
                top, bottom = max(last.o, last.c), min(last.o, last.c)
            if (up is None or up) and b.c >= top + box - 1e-9:
                o = top
                out.append(Bar(b.t, o, o + box, o, o + box, vol))
            elif (up is None or not up) and b.c <= bottom - box + 1e-9:
                o = bottom
                out.append(Bar(b.t, o, o, o - box, o - box, vol))
            elif up is True and b.c <= bottom - box + 1e-9:
                o = bottom
                out.append(Bar(b.t, o, o, o - box, o - box, vol))
            elif up is False and b.c >= top + box - 1e-9:
                o = top
                out.append(Bar(b.t, o, o + box, o, o + box, vol))
            else:
                break
            vol = 0.0
    return out


def line_break(bars: list[Bar], lines: int = 3) -> list[Bar]:
    """Three-line break (or n): a new line in the same direction when the close passes the last line; a turn only when
    it passes the far end of the last n lines."""
    if not bars:
        return []
    first = bars[0]
    out = [Bar(first.t, first.o, max(first.o, first.c), min(first.o, first.c), first.c, first.v)]
    vol = 0.0
    for b in bars[1:]:
        vol += b.v
        last = out[-1]
        up = last.c >= last.o
        recent = out[-lines:]
        hi = max(max(x.o, x.c) for x in recent)
        lo = min(min(x.o, x.c) for x in recent)
        if (up and b.c > last.c) or (not up and b.c < last.c):
            o = last.c
        elif (up and b.c < lo) or (not up and b.c > hi):
            o = last.o
        else:
            continue
        out.append(Bar(b.t, o, max(o, b.c), min(o, b.c), b.c, vol))
        vol = 0.0
    return out


@dataclass(frozen=True)
class KagiColumn:
    t: datetime
    start: float
    end: float
    thick: bool  # yang (thick) at the column's start
    switch: float | None  # the price where it turns thick or thin, if it does in this column
    v: float


def kagi(bars: list[Bar], reversal: float) -> list[KagiColumn]:
    """Kagi on closes: the line keeps going while price moves its way, and turns when price comes back by
    `reversal`. It's thick (yang) once it rises above the last shoulder (a high turn) and thin (yin) once it falls
    below the last waist (a low turn)."""
    if not bars:
        return []
    cols: list[list] = []  # t, start, end, v
    start = end = bars[0].c
    t0, vol, up = bars[0].t, bars[0].v, None
    for b in bars[1:]:
        vol += b.v
        if up is None:
            if abs(b.c - start) >= reversal:
                up, end = b.c > start, b.c
            continue
        if (up and b.c > end) or (not up and b.c < end):
            end = b.c
        elif (up and end - b.c >= reversal) or (not up and b.c - end >= reversal):
            cols.append([t0, start, end, vol - b.v])
            t0, start, end, up, vol = b.t, end, b.c, not up, b.v
    cols.append([t0, start, end, vol])
    out: list[KagiColumn] = []
    thick, shoulder, waist = cols[0][2] >= cols[0][1], None, None
    for t, s, e, v in cols:
        at_start, switch = thick, None
        if e > s:
            if not thick and shoulder is not None and e > shoulder:
                thick, switch = True, shoulder
            shoulder = e
        elif e < s:
            if thick and waist is not None and e < waist:
                thick, switch = False, waist
            waist = e
        out.append(KagiColumn(t, s, e, at_start, switch, v))
    return out


@dataclass(frozen=True)
class PnfColumn:
    t: datetime
    up: bool  # a column of X (rising) or O (falling)
    top: float
    bottom: float
    v: float


def point_figure(bars: list[Bar], box: float, reversal: int = 3, high_low: bool = False) -> list[PnfColumn]:
    """Point & figure: X boxes while price rises, O boxes while it falls, a new column only after a move of
    `reversal` boxes the other way. On closes, or on highs and lows."""
    if not bars:
        return []
    cols: list[list] = []  # t, up, top, bottom, v
    anchor = math.floor(bars[0].c / box) * box
    vol = 0.0
    for b in bars:
        vol += b.v
        hi, lo = (b.h, b.l) if high_low else (b.c, b.c)
        if not cols:
            if hi >= anchor + box - 1e-9:
                cols.append([b.t, True, anchor + math.floor((hi - anchor) / box + 1e-9) * box, anchor, vol])
                vol = 0.0
            elif lo <= anchor - box + 1e-9:
                cols.append([b.t, False, anchor, anchor - math.floor((anchor - lo) / box + 1e-9) * box, vol])
                vol = 0.0
            continue
        c = cols[-1]
        if c[1]:
            if hi >= c[2] + box - 1e-9:
                c[2] += math.floor((hi - c[2]) / box + 1e-9) * box
                c[4] += vol
                vol = 0.0
            elif lo <= c[2] - reversal * box + 1e-9:
                n = math.floor((c[2] - lo) / box + 1e-9)
                cols.append([b.t, False, c[2] - box, c[2] - n * box, vol])
                vol = 0.0
        else:
            if lo <= c[3] - box + 1e-9:
                c[3] -= math.floor((c[3] - lo) / box + 1e-9) * box
                c[4] += vol
                vol = 0.0
            elif hi >= c[3] + reversal * box - 1e-9:
                n = math.floor((hi - c[3]) / box + 1e-9)
                cols.append([b.t, True, c[3] + n * box, c[3] + box, vol])
                vol = 0.0
    return [PnfColumn(t, up, round(top, 8), round(bottom, 8), v) for t, up, top, bottom, v in cols]


# ---------- profiles ----------

@dataclass(frozen=True)
class VolumeProfile:
    start: datetime
    end: datetime
    step: float
    rows: list[tuple[float, float, float]]  # (row low, volume on rising candles, on falling ones), low to high
    poc: float  # the middle of the busiest row
    vah: float
    val: float


def _value_area(counts: dict[int, float], share: float) -> tuple[int, int, int]:
    """(point of control, value area low row, high row): from the busiest row, add the busier neighbour each time
    until `share` of the total is inside."""
    total = sum(counts.values())
    lo_row, hi_row = min(counts), max(counts)
    mid = (lo_row + hi_row) / 2
    poc = max(counts, key=lambda r: (counts[r], -abs(r - mid)))
    lo = hi = poc
    inside = counts[poc]
    while inside < share * total and (lo > lo_row or hi < hi_row):
        below = counts.get(lo - 1, 0.0) if lo > lo_row else -1.0
        above = counts.get(hi + 1, 0.0) if hi < hi_row else -1.0
        if above >= below:
            hi += 1
            inside += max(above, 0.0)
        else:
            lo -= 1
            inside += max(below, 0.0)
    return poc, lo, hi


def volume_profile(bars: list[Bar], rows: int = 24, share: float = 0.7) -> VolumeProfile | None:
    """Volume at each price, from candles: each candle's volume spread evenly across its high-low range and split by
    whether it closed up or down (the way TradingView's profile reads lower-timeframe candles)."""
    bars = [b for b in bars if b.v > 0]
    if not bars:
        return None
    hi, lo = max(b.h for b in bars), min(b.l for b in bars)
    step = nice_step((hi - lo) / max(rows, 1)) if hi > lo else 0.01
    up: dict[int, float] = defaultdict(float)
    down: dict[int, float] = defaultdict(float)
    for b in bars:
        a, z = math.floor(b.l / step + 1e-9), math.floor(b.h / step - 1e-9) if b.h > b.l else math.floor(b.l / step + 1e-9)
        z = max(a, z)
        each = b.v / (z - a + 1)
        side = up if b.c >= b.o else down
        for r in range(a, z + 1):
            side[r] += each
    counts = {r: up.get(r, 0.0) + down.get(r, 0.0) for r in set(up) | set(down)}
    poc, val, vah = _value_area(counts, share)
    out = [(round(r * step, 8), up.get(r, 0.0), down.get(r, 0.0)) for r in range(min(counts), max(counts) + 1)]
    return VolumeProfile(bars[0].t, bars[-1].t, step, out, round((poc + 0.5) * step, 8), round((vah + 1) * step, 8),
                         round(val * step, 8))


def by_session(bars: list[Bar]) -> dict[date, list[Bar]]:
    out: dict[date, list[Bar]] = defaultdict(list)
    for b in bars:
        out[b.t.date()].append(b)
    return dict(out)


LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


@dataclass(frozen=True)
class Tpo:
    day: date
    start: datetime
    step: float
    rows: list[tuple[float, str]]  # (row low, the letters of each half hour that traded there), low to high
    poc: float
    vah: float
    val: float
    ib_high: float  # the first hour (periods A and B): the initial balance
    ib_low: float
    open: float
    close: float
    singles: list[float]  # rows only one period traded in, inside the day's range


def tpo(bars: list[Bar], period: int = 30, step: float | None = None, share: float = 0.7) -> Tpo | None:
    """Market profile for one regular session from its 1-minute candles: a letter for each `period` minutes (A from
    9:30) at every price row it traded through."""
    bars = [b for b in bars if regular(b.t)]
    if not bars:
        return None
    day = bars[0].t.date()
    hi, lo = max(b.h for b in bars), min(b.l for b in bars)
    step = step or (nice_step((hi - lo) / 40) if hi > lo else 0.01)
    opened = datetime.combine(day, REGULAR_OPEN, tzinfo=bars[0].t.tzinfo)
    letters: dict[int, str] = defaultdict(str)
    ib = [b for b in bars if b.t < opened + timedelta(minutes=2 * period)]
    for k in sorted({int((b.t - opened).total_seconds() // 60) // period for b in bars}):
        part = [b for b in bars if int((b.t - opened).total_seconds() // 60) // period == k]
        a = math.floor(min(b.l for b in part) / step + 1e-9)
        z = math.floor(max(b.h for b in part) / step + 1e-9)
        for r in range(a, z + 1):
            letters[r] += LETTERS[k % len(LETTERS)]
    counts = {r: float(len(s)) for r, s in letters.items()}
    poc, val, vah = _value_area(counts, share)
    rows = [(round(r * step, 8), letters.get(r, "")) for r in range(min(letters), max(letters) + 1)]
    inside = [r for r in letters if min(letters) < r < max(letters)]
    return Tpo(day, opened, step, rows, round((poc + 0.5) * step, 8), round((vah + 1) * step, 8), round(val * step, 8),
               max(b.h for b in ib), min(b.l for b in ib), bars[0].o, bars[-1].c,
               sorted(round(r * step, 8) for r in inside if len(letters[r]) == 1))


def to_csv(rows: list[Bar], extra: dict[str, list] | None = None) -> str:
    """Candles (and any study columns) as CSV, times in exchange time."""
    extra = extra or {}
    head = ["time", "open", "high", "low", "close", "volume", *extra]
    lines = [",".join(head)]
    for i, b in enumerate(rows):
        cols = [b.t.isoformat(), f"{b.o:g}", f"{b.h:g}", f"{b.l:g}", f"{b.c:g}", f"{b.v:g}"]
        cols += ["" if v[i] is None else f"{v[i]:.6g}" for v in extra.values()]
        lines.append(",".join(cols))
    return "\n".join(lines) + "\n"
