"""Trend lines for the Charts tab: lines you draw (saved per symbol), lines found from swing points, and a tracker that
follows each line through a break, a retest and its outcome on closed candles.

A line is two points in (time, price). Between and beyond them it moves at a steady rate per hour of trading, counted
on a market clock that only runs during the session the line was drawn on (4:00-20:00 with extended hours, 9:30-16:00
without, or around the clock from Sunday 20:00 to Friday 20:00 for the overnight market) and skips the rest. So a line is straight on every intraday chart with the same session and on
daily and weekly charts, and its price at any moment is the same whatever interval you look at it on. Market holidays
(NYSE full-day closures, listed below through 2027) add nothing either; for the 24-hour market, the holiday's whole
window (8 PM the evening before to 8 PM) is skipped.

The break-and-retest stages, on closed candles only:
- broken: a candle closes on the other side of the line from where price was;
- then a later candle's wick comes back to the line from the broken side, reaching it or coming within `tol` of it:
  confirmed if that candle closes on the broken side, failed if it closes back through.
`tol` is a tenth of the candle's ATR(14). Every stage's text says what that candle did, with the prices."""

from __future__ import annotations

import math
import re
import secrets
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .features import Bar

SESSIONS = {"ext": (time(4, 0), time(20, 0)), "rth": (time(9, 30), time(16, 0))}
KINDS = ("trend", "hline", "hray")  # trend line, horizontal line (all the way across), horizontal ray (from its point on)
EPOCH = date(1970, 1, 5)  # a Monday
CLOCKS = ("ext", "rth", "24h")
WEEK_OPEN, WEEK_HOURS = 20 * 3600, 120 * 3600  # the 24-hour market: Sunday 20:00 to Friday 20:00 (ET)
# NYSE full-day closures (early closes are ordinary days here). Add each new year's when it's published.
HOLIDAYS = tuple(date.fromisoformat(x) for x in (
    "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27", "2024-06-19", "2024-07-04", "2024-09-02", "2024-11-28", "2024-12-25",
    "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27",
    "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"))
_HOLIDAY_STARTS: dict[str, list[float]] = {}


def session_seconds(clock: str) -> int:
    a, b = SESSIONS[clock]
    return (b.hour * 60 + b.minute - a.hour * 60 - a.minute) * 60


def market_clock(t: datetime, clock: str = "ext") -> float:
    """Seconds of session time from a fixed Monday to `t` (market time): every weekday adds one session; nights,
    weekends and market holidays add nothing."""
    raw = _raw_clock(t, clock)
    if clock not in _HOLIDAY_STARTS:
        if clock == "24h":  # a holiday's 24-hour window: from 20:00 the evening before
            starts = [_raw_clock(datetime.combine(h - timedelta(days=1), time(20)), clock) for h in HOLIDAYS]
        else:
            starts = [_raw_clock(datetime.combine(h, time(0)), clock) for h in HOLIDAYS]
        _HOLIDAY_STARTS[clock] = sorted(starts)
    starts = _HOLIDAY_STARTS[clock]
    length = 86400 if clock == "24h" else session_seconds(clock)
    k = bisect_right(starts, raw)
    if k:
        raw -= (k - 1) * length + min(raw - starts[k - 1], length)
    return raw


def _raw_clock(t: datetime, clock: str) -> float:
    """market_clock without the holidays: from the wall-clock time alone."""
    if clock == "24h":
        days = (t.date() - date(1970, 1, 4)).days  # from a Sunday
        into = days % 7 * 86400 + t.hour * 3600 + t.minute * 60 + t.second + t.microsecond / 1e6
        return days // 7 * WEEK_HOURS + min(max(into - WEEK_OPEN, 0), WEEK_HOURS)
    a, b = SESSIONS[clock]
    d = t.date()
    days = (d - EPOCH).days
    weekdays = days // 7 * 5 + min(days % 7, 5)
    if d.weekday() >= 5:
        return weekdays * session_seconds(clock)
    s = t.hour * 3600 + t.minute * 60 + t.second + t.microsecond / 1e6
    lo, hi = a.hour * 3600 + a.minute * 60, b.hour * 3600 + b.minute * 60
    return weekdays * session_seconds(clock) + min(max(s - lo, 0), hi - lo)


@dataclass(frozen=True)
class Line:
    id: str
    kind: str
    t1: datetime
    p1: float
    t2: datetime
    p2: float
    clock: str = "ext"
    extend: bool = True  # a trend line goes on past its second point
    name: str = ""

    def price_at(self, t: datetime) -> float | None:
        """The line's price at `t`, or None where it isn't drawn (before it starts, or past the end of an unextended one)."""
        if self.kind == "hline":
            return self.p1
        if t < self.t1:
            return None
        if self.kind == "hray":
            return self.p1
        c1, c2 = market_clock(self.t1, self.clock), market_clock(self.t2, self.clock)
        c = market_clock(t, self.clock)
        if not self.extend and t > self.t2:
            return None
        if c2 == c1:
            return self.p1
        return self.p1 + (self.p2 - self.p1) * (c - c1) / (c2 - c1)

    @property
    def label(self) -> str:
        if self.name:
            return self.name
        if self.kind == "trend":
            return f"trend line {self.p1:.2f} to {self.p2:.2f}"
        return f"{'horizontal line' if self.kind == 'hline' else 'horizontal ray'} {self.p1:.2f}"

    @property
    def called(self) -> str:
        """How a sentence names it: "Tonight's high 729.54" if it has a name, otherwise "the trend line 705.05 to 701.08"."""
        return self.name if self.name else f"the {self.label}"


def line_from_json(d: dict, tz) -> Line:
    """A drawing as the page saves it: {id, kind, t1, p1, t2, p2 (times in epoch seconds), clock, extend, name}."""
    kind = d.get("kind")
    if kind not in KINDS:
        raise ValueError(f"unknown drawing {kind!r}")
    try:
        t1 = datetime.fromtimestamp(float(d["t1"]), tz)
        p1 = float(d["p1"])
        t2 = datetime.fromtimestamp(float(d.get("t2", d["t1"])), tz)
        p2 = float(d.get("p2", p1))
    except (KeyError, TypeError, ValueError):
        raise ValueError("a drawing needs its times and prices") from None
    if not all(math.isfinite(x) and x > 0 for x in (p1, p2)):
        raise ValueError("a drawing's prices must be above zero")
    if kind == "trend" and t2 < t1:
        t1, p1, t2, p2 = t2, p2, t1, p1
    clock = d.get("clock") if d.get("clock") in CLOCKS else "ext"
    return Line(str(d.get("id") or secrets.token_hex(4))[:24], kind, t1, p1, t2, p2, clock, bool(d.get("extend", True)),
                str(d.get("name") or "")[:60])


def _atr(bars: list[Bar], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    trs = [b.h - b.l if i == 0 else max(b.h - b.l, abs(b.h - bars[i - 1].c), abs(b.l - bars[i - 1].c)) for i, b in enumerate(bars)]
    if len(trs) < n:
        return [sum(trs[:i + 1]) / (i + 1) for i in range(len(trs))]
    a = sum(trs[:n]) / n
    for i in range(len(trs)):
        if i < n - 1:
            out[i] = sum(trs[:i + 1]) / (i + 1)
        elif i == n - 1:
            out[i] = a
        else:
            a = (a * (n - 1) + trs[i]) / n
            out[i] = a
    return out


@dataclass(frozen=True)
class Stage:
    stage: str  # broken | confirmed | failed
    index: int  # the candle's index in the list given
    side: str  # up: broke above the line | down: broke below it
    line: float  # the line's price at that candle
    text: str


def _named(t: datetime, daily: bool = False) -> str:
    """A candle's name in a sentence: its time ("00:05"), or for daily and longer candles its date ("Nov 27")."""
    return t.strftime("%b %d") if daily else t.strftime("%H:%M")


def track(line: Line, bars: list[Bar], tol_atr: float = 0.1) -> list[Stage]:
    """The line's breaks and retests on these (closed) candles, oldest first. After a failed retest price is back where
    it was, so a new break can start; after a confirmed one the tracking stops."""
    out: list[Stage] = []
    atr = _atr(bars)
    daily = bool(bars) and all(b.t.hour == 0 and b.t.minute == 0 for b in bars)

    def _hm(t: datetime) -> str:
        return _named(t, daily)

    side = 0  # where closes have been: +1 above the line, -1 below
    broken: Stage | None = None
    for i, b in enumerate(bars):
        lp = line.price_at(b.t)
        if lp is None:
            continue
        where = 1 if b.c > lp else -1 if b.c < lp else 0
        if broken is None:
            if side == 0:
                side = where
                continue
            if where == -side:
                up = where > 0
                broken = Stage("broken", i, "up" if up else "down", lp,
                               f"The {_hm(b.t)} candle closed at {b.c:.2f}, {'above' if up else 'below'} {line.called} "
                               f"({lp:.2f} there).")
                out.append(broken)
            continue
        if i == broken.index:
            continue
        up = broken.side == "up"
        tol = tol_atr * (atr[i] or 0.0)
        wick = b.l if up else b.h
        reached = wick <= lp if up else wick >= lp
        near = wick <= lp + tol if up else wick >= lp - tol
        if not near:
            continue
        how = (f"its {'low' if up else 'high'} {wick:.2f} reached the line ({lp:.2f} there)" if reached else
               f"its {'low' if up else 'high'} {wick:.2f} came within {abs(wick - lp):.2f} of the line ({lp:.2f} there; "
               f"the tolerance is a tenth of ATR, {tol:.2f})")
        held = b.c > lp if up else b.c < lp
        if held:
            out.append(Stage("confirmed", i, broken.side, lp,
                             f"Break {'up' if up else 'down'} confirmed: after the {_hm(bars[broken.index].t)} close "
                             f"{'above' if up else 'below'} {line.called}, the {_hm(b.t)} candle came back: {how}, and it "
                             f"closed at {b.c:.2f}, still {'above' if up else 'below'} it."))
            return out
        out.append(Stage("failed", i, broken.side, lp,
                         f"Break {'up' if up else 'down'} failed: the {_hm(b.t)} candle came back ({how}) and closed at "
                         f"{b.c:.2f}, {'below' if up else 'above'} the line again."))
        broken, side = None, -1 if up else 1
    return out


# ---------- lines found from swing points ----------

@dataclass(frozen=True)
class AutoLine:
    side: str  # resistance: through swing highs | support: through swing lows
    i1: int
    p1: float
    i2: int
    p2: float  # the two swing points it's drawn through (the first and the last it touches)
    touches: int
    end: int  # the last candle it reaches: where a close first went through it, or the last candle
    broken: bool


def swings(bars: list[Bar], n: int = 3) -> tuple[list[int], list[int]]:
    """Swing highs and lows: a candle with a higher high (lower low) than the n candles on each side."""
    hi, lo = [], []
    for i in range(n, len(bars) - n):
        w = bars[i - n:i + n + 1]
        if bars[i].h == max(b.h for b in w) and all(b.h < bars[i].h for b in w[:n]):
            hi.append(i)
        if bars[i].l == min(b.l for b in w) and all(b.l > bars[i].l for b in w[:n]):
            lo.append(i)
    return hi, lo


def auto_lines(bars: list[Bar], clock: str = "ext", n: int = 3, min_touches: int = 3, tol_atr: float = 0.15,
               most: int = 3) -> list[AutoLine]:
    """Lines through swing highs (and lows) that touch at least `min_touches` of them, within a tolerance of
    `tol_atr` ATRs, with no candle's high (low) going past them, wicks included, from the first touch to the last.
    The ones with the most touches, newest first, at most `most` each side."""
    if len(bars) < 2 * n + 3:
        return []
    atr = _atr(bars)
    clk = [market_clock(b.t, clock) for b in bars]
    highs, lows = swings(bars, n)
    out: list[AutoLine] = []
    for side, piv, val in (("resistance", highs[-40:], lambda b: b.h), ("support", lows[-40:], lambda b: b.l)):
        sign = 1 if side == "resistance" else -1
        found: list[AutoLine] = []
        for a in range(len(piv)):
            for z in range(a + 1, len(piv)):
                i, j = piv[a], piv[z]
                if clk[j] == clk[i]:
                    continue
                slope = (val(bars[j]) - val(bars[i])) / (clk[j] - clk[i])

                def at(k, i=i, slope=slope):
                    return val(bars[i]) + slope * (clk[k] - clk[i])

                tol = tol_atr * (atr[j] or 0.0)
                if any(sign * (val(bars[k]) - at(k)) > tol for k in range(i, j + 1)):
                    continue  # a candle pokes through it
                touches = [k for k in piv if i <= k <= j and abs(val(bars[k]) - at(k)) <= tol]
                if len(touches) < min_touches:
                    continue
                end, broken = len(bars) - 1, False
                for k in range(j + 1, len(bars)):
                    if sign * (bars[k].c - at(k)) > 0:
                        end, broken = k, True
                        break
                found.append(AutoLine(side, i, val(bars[i]), j, at(j), len(touches), end, broken))
        found.sort(key=lambda x: (-x.touches, -x.i2))
        kept: list[AutoLine] = []
        for x in found:
            if any(abs(x.i1 - y.i1) <= n and abs(x.i2 - y.i2) <= n * 2 or (x.i1 >= y.i1 and x.i2 <= y.i2) for y in kept):
                continue
            kept.append(x)
            if len(kept) == most:
                break
        out += kept
    return out


# ---------- saved drawings ----------

TICKER = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")


class Drawings:
    """logs/drawings.json: each symbol's lines, in time and price, the same on every interval."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict[str, list[dict]]:
        import json

        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, symbol: str) -> list[dict]:
        return self.load().get(symbol.upper(), [])

    def find(self, symbol: str, did: str) -> dict | None:
        return next((d for d in self.get(symbol) if d.get("id") == did), None)

    def save(self, symbol: str, drawings: list, tz) -> list[dict]:
        import json
        import os

        symbol = str(symbol).strip().upper()
        if not TICKER.match(symbol):
            raise ValueError("drawings are kept per ticker")
        if not isinstance(drawings, list) or len(drawings) > 200:
            raise ValueError("at most 200 drawings on a symbol")
        clean = []
        for d in drawings:
            line = line_from_json(d if isinstance(d, dict) else {}, tz)
            color = str(d.get("color") or "#2962ff")
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                raise ValueError("a colour is #rrggbb")
            tf = str(d.get("tf") or "")
            if tf:
                from .charting import parse_interval

                tf = parse_interval(tf).label  # the interval it was drawn on: a trend line isn't shown on much longer ones
            clean.append({"id": line.id, "kind": line.kind, "t1": int(line.t1.timestamp()), "p1": round(line.p1, 6),
                          "t2": int(line.t2.timestamp()), "p2": round(line.p2, 6), "clock": line.clock, "extend": line.extend,
                          "name": line.name, "color": color, "tf": tf})
        data = self.load()
        if clean:
            data[symbol] = clean
        else:
            data.pop(symbol, None)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, self.path)
        return clean

