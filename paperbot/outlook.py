"""The morning report: one page a day on a symbol's key support, what could happen above it and below it, and why.

Worked out from 180 days of 4-hour candles around the clock (pre-market, regular, after hours and overnight), the way a
chartist marks up a chart by hand: swing highs and lows on several timeframes, the last session's high, low and close, the
overnight range, floor-trader pivots, the 20 and 50 EMA and the 200 SMA, round numbers and trend lines. Prices that several
of these share become one level or zone, scored by how many and how strong they are.

Everything in a report is something its candles say, with the reason beside it. It describes scenarios, it doesn't predict
one, and it isn't advice: on 244 past SPY mornings the key level bounced 54% of the times it was touched, and a level at a
random distance 50%."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .features import Bar

OPEN, CLOSE = time(9, 30), time(16)
DAYS = 180
ROUND_STEPS = (0.5, 1, 2.5, 5, 10, 25, 50, 100)


@dataclass(frozen=True)
class Point:
    price: float
    weight: float
    why: str


@dataclass
class Zone:
    points: list[Point]

    @property
    def low(self) -> float:
        return min(p.price for p in self.points)

    @property
    def high(self) -> float:
        return max(p.price for p in self.points)

    @property
    def score(self) -> float:
        return sum(p.weight for p in self.points)

    @property
    def best(self) -> Point:
        return max(self.points, key=lambda p: p.weight)


@dataclass
class Line:
    """A trend line through two swings, on 4-hour candles."""

    start: datetime
    start_price: float
    end: datetime
    end_price: float
    now: float  # its price at the report
    falling: bool


@dataclass
class Report:
    symbol: str
    at: datetime
    price: float
    prior_close: float | None
    prior_day: date | None
    adr: float  # the average daily range
    key: Zone
    key_price: float
    up: list[tuple[str, Zone]]  # (what it is, where), nearest first
    down: list[tuple[str, Zone]]
    bias: str  # buyers | sellers | mixed
    facts: list[str]
    support: list[str]
    averages: dict[str, float] = field(default_factory=dict)
    lines: list[Line] = field(default_factory=list)


# --- candles ------------------------------------------------------------------------------------------


def merge(rows: list[Bar], start: datetime | None = None) -> Bar:
    return Bar(start or rows[0].t, rows[0].o, max(b.h for b in rows), min(b.l for b in rows), rows[-1].c, sum(b.v for b in rows))


def grouped(bars: list[Bar], hours: int) -> list[Bar]:
    """Candles of `hours`, on the clock (a 4-hour day is 12 AM, 4 AM, 8 AM, 12 PM, 4 PM and 8 PM in New York)."""
    out: dict[datetime, list[Bar]] = defaultdict(list)
    for b in bars:
        out[b.t.replace(hour=b.t.hour // hours * hours, minute=0, second=0, microsecond=0)].append(b)
    return [merge(rows, t) for t, rows in sorted(out.items())]


def sessions(bars: list[Bar]) -> list[tuple[date, Bar]]:
    """Each day's regular-hours candle."""
    out: dict[date, list[Bar]] = defaultdict(list)
    for b in bars:
        if OPEN <= b.t.time() < CLOSE:
            out[b.t.date()].append(b)
    return [(d, merge(rows)) for d, rows in sorted(out.items())]


def ema(values: list[float], n: int) -> list[float | None]:
    if len(values) < n:
        return [None] * len(values)
    e = sum(values[:n]) / n
    out: list[float | None] = [None] * (n - 1) + [e]
    for v in values[n:]:
        e += (v - e) * 2 / (n + 1)
        out.append(e)
    return out


def sma(values: list[float], n: int) -> list[float | None]:
    return [sum(values[i - n + 1:i + 1]) / n if i >= n - 1 else None for i in range(len(values))]


def swings(bars: list[Bar], k: int) -> list[tuple[int, bool, float]]:
    """(candle, is a high, price) for every candle higher or lower than the `k` on each side of it."""
    out = []
    for i in range(k, len(bars) - k):
        sides = bars[i - k:i] + bars[i + 1:i + k + 1]
        if bars[i].h > max(b.h for b in sides):
            out.append((i, True, bars[i].h))
        if bars[i].l < min(b.l for b in sides):
            out.append((i, False, bars[i].l))
    return out


# --- levels -------------------------------------------------------------------------------------------


def _day(d: date) -> str:
    return f"{d:%b} {d.day}"


def _hour(t: datetime) -> str:
    return f"{_day(t.date())} {t:%I %p}".replace(" 0", " ")


def points(bars: list[Bar], at: datetime, adr: float) -> tuple[list[Point], dict[str, float]]:
    """Everything a level can be made of, at `at`, from the 30-minute candles before it."""
    price = bars[-1].c
    out: list[Point] = []
    days = sessions(bars)
    today = [x for x in days if x[0] == at.date()]
    past = [x for x in days if x[0] < at.date()]
    h1, h4 = grouped(bars, 1), grouped(bars, 4)

    recent = past[-10:]
    if past:
        d, b = past[-1]
        out += [Point(b.h, 3, f"last session's high ({_day(d)})"), Point(b.l, 3, f"last session's low ({_day(d)})"),
                Point(b.c, 2, f"last session's close ({_day(d)})")]
        mid = (b.h + b.l + b.c) / 3
        for name, v in (("S1", 2 * mid - b.h), ("S2", mid - b.h + b.l), ("R1", 2 * mid - b.l), ("R2", mid + b.h - b.l)):
            out.append(Point(v, 1, f"pivot {name}"))
        open_ = datetime.combine(at.date(), OPEN, tzinfo=at.tzinfo)
        night = [x for x in bars if datetime.combine(d, CLOSE, tzinfo=at.tzinfo) <= x.t < min(open_, at)]
        if night:
            out += [Point(max(x.h for x in night), 2, "overnight high"), Point(min(x.l for x in night), 2, "overnight low")]
    if len(recent) >= 3:
        d, b = min(recent, key=lambda x: x[1].l)
        out.append(Point(b.l, 3, f"lowest of the last {len(recent)} sessions ({_day(d)})"))
        d, b = max(recent, key=lambda x: x[1].h)
        out.append(Point(b.h, 3, f"highest of the last {len(recent)} sessions ({_day(d)})"))
    if today:
        out += [Point(today[0][1].h, 2, "today's high so far"), Point(today[0][1].l, 2, "today's low so far")]
    daily = [b for _, b in past]
    for i, high, v in swings(daily, 1):
        age = len(daily) - i
        out.append(Point(v, 2 if age <= 10 else 1.25 if age <= 40 else 0.75, f"daily swing {'high' if high else 'low'} ({_day(past[i][0])})"))
    for i, high, v in swings(h4, 2):
        age = len(h4) - i
        out.append(Point(v, 1.5 if age <= 36 else 1 if age <= 180 else 0.5, f"4-hour swing {'high' if high else 'low'} ({_hour(h4[i].t)})"))
    for i, high, v in swings(h1, 3):
        if len(h1) - i <= 24 * 7:
            out.append(Point(v, 0.75, f"1-hour swing {'high' if high else 'low'} ({_hour(h1[i].t)})"))

    closes = [b.c for b in h4]
    averages = {name: v for name, v in (("20 EMA", ema(closes, 20)[-1]), ("50 EMA", ema(closes, 50)[-1]), ("200 SMA", sma(closes, 200)[-1]))
                if v is not None}
    out += [Point(v, 1.5, f"4-hour {name}") for name, v in averages.items()]
    step = next((s for s in ROUND_STEPS if s >= adr * 0.45), ROUND_STEPS[-1])
    base = round(price / step) * step
    out += [Point(base + n * step, 0.75, f"round number {base + n * step:g}") for n in range(-6, 7)]
    return _same(out, price), averages


def _same(found: list[Point], price: float) -> list[Point]:
    """One price seen on several timeframes counts once: at its best weight, and a little more for each other sighting."""
    out: list[Point] = []
    for p in sorted(found, key=lambda p: -p.weight):
        i = next((i for i, q in enumerate(out) if abs(q.price - p.price) <= 0.00012 * price), None)
        if i is None:
            out.append(p)
        elif "round number" not in p.why:
            q = out[i]
            out[i] = Point(q.price, q.weight + 0.25, q.why)
    return out


def zones(found: list[Point], adr: float) -> list[Zone]:
    """Points close together become one zone: gaps up to a tenth of the average daily range, up to a third of it wide."""
    out: list[Zone] = []
    for p in sorted(found, key=lambda p: p.price):
        if out and p.price - out[-1].high <= 0.1 * adr and p.price - out[-1].low <= 0.33 * adr:
            out[-1].points.append(p)
        else:
            out.append(Zone([p]))
    return out


def trend_lines(h4: list[Bar], at: datetime) -> list[Line]:
    """The latest falling line over two swing highs and rising line under two swing lows that price hasn't closed through."""
    out = []
    piv = swings(h4, 3)
    n = len(h4) - 1
    for high in (True, False):
        pts = [(i, v) for i, h, v in piv if h == high and n - i <= 120]
        best = None
        for b in range(len(pts) - 1, 0, -1):
            for a in range(b - 1, -1, -1):
                (i, u), (j, v) = pts[a], pts[b]
                if j - i < 12 or (v >= u if high else v <= u):
                    continue
                slope = (v - u) / (j - i)
                if any((x.c > u + slope * (k - i)) if high else (x.c < u + slope * (k - i)) for k, x in enumerate(h4[i:n + 1], i)):
                    continue
                best = Line(h4[i].t, u, h4[j].t, v, u + slope * (n - i), high)
                break
            if best:
                break
        if best:
            out.append(best)
    return out


# --- the report ---------------------------------------------------------------------------------------


def build(symbol: str, bars: list[Bar], at: datetime) -> Report | None:
    """The report at `at` from 30-minute candles, oldest first; nothing at or after `at` is used."""
    bars = [b for b in bars if b.t < at]
    days = sessions(bars)
    past = [x for x in days if x[0] < at.date()]
    if len(past) < 25 or not bars:
        return None
    price = bars[-1].c
    adr = sum(b.h - b.l for _, b in past[-14:]) / len(past[-14:])
    found, averages = points(bars, at, adr)
    h4 = grouped(bars, 4)
    lines = trend_lines(h4, at)
    for ln in lines:
        found.append(Point(ln.now, 1, f"{'falling' if ln.falling else 'rising'} trend line"))
    below = zones([p for p in found if p.price <= price], adr)[::-1]  # nearest first
    above = zones([p for p in found if p.price > price + 0.02 * adr], adr)
    key = _key(below, price, adr)
    if key is None:  # at new lows: nothing is under the price
        key = Zone([Point(price, 1, "the current price: there is no earlier low under it")])
    key_price = key.best.price
    up = _upside(above, price, adr)
    down = _downside([z for z in below if z.high < key.low], key, adr)
    prior_day, prior = past[-1]
    bias, facts = _facts(price, prior, prior_day, averages, h4, lines, at, days)
    return Report(symbol, at, price, prior.c, prior_day, adr, key, key_price, up, down, bias, facts,
                  _support(bars, key, key_price, adr, at, price), averages, lines)


STRONG, SOME = 2.0, 0.75  # a zone's score to count as a level, and as a faint one where there's nothing better


def _key(below: list[Zone], price: float, adr: float) -> Zone | None:
    """The support to hold: the strongest near the price, the nearer the better; a faint one if nothing strong is near."""
    def worth(z: Zone) -> float:
        return z.score * (1 - 0.6 * (price - z.high) / (0.75 * adr))

    near = [z for z in below if price - z.high <= 0.75 * adr]
    strong = [z for z in near if z.score >= STRONG]
    close_by = [z for z in near if z.score >= 1.5 and price - z.high <= 0.4 * adr]
    found = strong or close_by or [z for z in near if z.score >= SOME]
    return max(found, key=worth) if found else next((z for z in below if z.score >= STRONG), None)


def _first(zones_ahead: list[Zone], edge: float, adr: float, up: bool) -> Zone | None:
    """The first level past `edge`: the nearest strong one, or a faint one if that is more than half a day's range away."""
    def away(z: Zone) -> float:
        return z.low - edge if up else edge - z.high

    ahead = [z for z in zones_ahead if away(z) >= 0.03 * adr]
    strong = next((z for z in ahead if z.score >= STRONG), None)
    if strong is not None and away(strong) <= 0.5 * adr:
        return strong
    faint = [z for z in ahead if z.score >= SOME and (strong is None or away(z) < away(strong)) and away(z) <= 0.75 * adr]
    return max(faint, key=lambda z: z.score) if faint else strong


def _upside(above: list[Zone], price: float, adr: float) -> list[tuple[str, Zone]]:
    first = _first(above, price, adr, up=True)
    if first is None:
        return []
    out = [("First upside target", first)]
    rest = [z for z in above if z.low - first.high >= 0.03 * adr and z.score >= STRONG]
    reach = [z for z in rest if z.low <= price + 1.25 * adr]
    if reach:
        major = max(reach, key=lambda z: z.score)
        out.append(("Major resistance", major))
        rest = [z for z in rest if z.low > major.high]
    if rest:
        out.append(("Next upside target", rest[0]))
        far = [z for z in rest[1:] if z.low <= price + 2.75 * adr]
        if far:
            out.append(("Overhead supply", max(far, key=lambda z: z.score)))
    return out


def _downside(under: list[Zone], key: Zone, adr: float) -> list[tuple[str, Zone]]:
    first = _first(under, key.low, adr, up=False)
    if first is None:
        return []
    out = [("First support below", first)]
    rest = [z for z in under if first.low - z.high >= 0.03 * adr and z.score >= STRONG and z.high >= key.low - 2.5 * adr]
    if rest:
        major = max(rest, key=lambda z: z.score)
        out.append(("Major support", major))
        deeper = [z for z in under if z.high < major.low and z.score >= STRONG and z.high >= key.low - 4 * adr]
        if deeper:
            out.append(("Support under that", max(deeper, key=lambda z: z.score)))
    return out


def _facts(price: float, prior: Bar, prior_day: date, averages: dict[str, float], h4: list[Bar], lines: list[Line], at: datetime,
           days: list[tuple[date, Bar]]) -> tuple[str, list[str]]:
    facts = []
    move = (price - prior.c) / prior.c * 100
    today = next((b for d, b in days if d == at.date()), None)
    if today:
        gap = (today.o - prior.c) / prior.c * 100
        if abs(gap) >= 0.1:
            facts.append(f"Opened at {today.o:.2f}, a gap {'up' if gap > 0 else 'down'} of {abs(gap):.1f}% from {_day(prior_day)}'s close of {prior.c:.2f}.")
        facts.append(f"So far today: high {today.h:.2f}, low {today.l:.2f}. Now {price:.2f}, {move:+.1f}% from the last close.")
    else:
        facts.append(f"Now {price:.2f}, {move:+.1f}% from {_day(prior_day)}'s close of {prior.c:.2f}.")
    over = [name for name, v in averages.items() if price > v]
    under = [name for name, v in averages.items() if price <= v]

    def listed(names: list[str]) -> str:
        return " and ".join([", ".join(names[:-1]), names[-1]] if len(names) > 1 else names)

    if over:
        facts.append(f"Price is above the 4-hour {listed(over)}" + (f", and below the {listed(under)}." if under else "."))
    elif under:
        facts.append(f"Price is below the 4-hour {listed(under)}.")
    piv = swings(h4, 3)
    highs, lows = [v for _, h, v in piv if h][-2:], [v for _, h, v in piv if not h][-2:]
    steps = 0
    if len(highs) == 2 and len(lows) == 2:
        hh, hl = highs[1] > highs[0], lows[1] > lows[0]
        steps = (1 if hh else -1) + (1 if hl else -1)
        facts.append(f"The last two 4-hour swing highs are {'rising' if hh else 'falling'} ({highs[0]:.2f}, {highs[1]:.2f}) and the swing lows "
                     f"{'rising' if hl else 'falling'} ({lows[0]:.2f}, {lows[1]:.2f}).")
    for ln in lines:
        facts.append(f"A {'falling' if ln.falling else 'rising'} trend line from {_day(ln.start.date())} is at {ln.now:.2f}.")
    e20, e50 = averages.get("20 EMA"), averages.get("50 EMA")
    score = steps + (0 if e20 is None else 1 if price > e20 else -1) + (0 if e50 is None else 1 if price > e50 else -1)
    return ("buyers" if score >= 2 else "sellers" if score <= -2 else "mixed"), facts


def _times(n: int) -> str:
    return "once" if n == 1 else "twice" if n == 2 else f"{n} times"


def visits(bars: list[Bar], level: float, adr: float) -> tuple[list[Bar], list[Bar], bool]:
    """Each time price came down to a level: the candles of the visits that bounced (a close 0.15 of a day's range above),
    the candles that closed through it, and whether a visit is still going on."""
    held, lost, at = [], [], None
    above = False  # whether price had been clear of the level since the last visit
    for b in bars:
        if at is None and above and b.l <= level + 0.06 * adr:
            at = b
        if at is not None:
            at = b if b.l < at.l else at
            if b.c < level - 0.02 * adr:
                lost.append(b)
                at, above = None, False
            elif b.c > level + 0.15 * adr:
                held.append(at)
                at = None
        above = above or b.c > level + 0.15 * adr
    return held, lost, at is not None


def _support(bars: list[Bar], key: Zone, level: float, adr: float, at: datetime, price: float) -> list[str]:
    """What price has done at the key level lately."""
    out = [f"{level:.2f} is made of: " + "; ".join(reasons(key, 4)) + "."]
    held, lost, now = visits([b for b in bars if b.t >= at - timedelta(days=14)], level, adr)
    if held:
        out.append(f"In the last two weeks price came down to it and bounced {_times(len(held))}, the last on {_hour(held[-1].t)} (low {held[-1].l:.2f}).")
    if lost:
        out.append(f"It gave way {_times(len(lost))}, the last on {_hour(lost[-1].t)} (closed at {lost[-1].c:.2f}).")
    if now:
        out.append("Price is testing it now.")
    elif not held and not lost:
        out.append("Price hasn't come back to it from above in the last two weeks, so it's untested.")
    out.append(f"Price is {price - level:.2f} above it ({(price - level) / level * 100:.1f}%)." if price >= level else
               f"Price is {level - price:.2f} under it.")
    return out


# --- words --------------------------------------------------------------------------------------------


def tidy(v: float) -> str:
    return f"{v:.2f}"


def span(z: Zone, adr: float, up: bool) -> tuple[float, float]:
    """The zone's prices, nearest first; one price twice when it's narrow."""
    if z.high - z.low < 0.05 * adr:
        return z.best.price, z.best.price
    return (z.low, z.high) if up else (z.high, z.low)


def reasons(z: Zone, n: int = 3) -> list[str]:
    return [f"{p.why} {p.price:.2f}" if "round number" not in p.why else p.why for p in sorted(z.points, key=lambda p: -p.weight)[:n]]


def to_json(r: Report, bars: list[Bar], shown: int = DAYS) -> dict:
    """The report as the page draws it: words, levels and the chart."""
    bars = [b for b in bars if b.t < r.at]
    h4 = grouped(bars, 4)
    closes = [b.c for b in h4]
    key = tidy(r.key_price)

    def rows(listed: list[tuple[str, Zone]], up: bool) -> list[dict]:
        out = []
        for what, z in listed:
            a, b = span(z, r.adr, up)
            out.append({"what": what, "near": round(a, 2), "far": round(b, 2), "low": round(min(a, b), 2), "high": round(max(a, b), 2),
                        "why": reasons(z), "score": round(z.score, 1)})
        return out

    up, down = rows(r.up, True), rows(r.down, False)

    def text(x: dict) -> str:
        return tidy(x["near"]) if x["near"] == x["far"] else f"{tidy(x['low'])} to {tidy(x['high'])}"

    strong = next((x for x in up if x["what"] == "Major resistance"), up[0] if up else None)
    bull = f"While {r.symbol} holds above {key}, buyers have something to lean on and a push up stays possible."
    bear = f"If {r.symbol} closes a half-hour candle below {key}, that support has gone and sellers have the upper hand."
    control = {"buyers": "Buyers have the better of it on the 4-hour chart", "sellers": "Sellers have the better of it on the 4-hour chart",
               "mixed": "Neither side is in charge on the 4-hour chart"}[r.bias]
    outlook = [f"{control}. {key} is the line between the two scenarios."]
    if strong:
        outlook.append(f"For buyers to take over, price needs to get back above {text(strong)} and stay there. Until then a rise is a bounce.")
    if down:
        outlook.append(f"Below {key}, the next place buyers showed up is {text(down[0])}.")
    outlook.append("Let a candle close on one side before acting on it: a touch isn't a break, and a first move often reverses.")
    epoch = [int(b.t.timestamp()) for b in h4]

    def series(values: list[float | None]) -> list[list]:
        return [[t, round(v, 2)] for t, v in zip(epoch, values, strict=True) if v is not None]

    return {
        "id": f"{r.at:%Y-%m-%d-%H%M}-{r.symbol}", "symbol": r.symbol, "at": r.at.isoformat(), "day": r.at.date().isoformat(),
        "price": round(r.price, 2), "prior_close": round(r.prior_close, 2), "adr": round(r.adr, 2), "bias": r.bias,
        "title": f"{r.symbol}: {key} is the level to hold",
        "key": {"price": round(r.key_price, 2), "low": round(r.key.low, 2), "high": round(r.key.high, 2), "why": reasons(r.key, 4)},
        "facts": r.facts, "support": r.support, "bull": {"text": bull, "levels": up}, "bear": {"text": bear, "levels": down}, "outlook": outlook,
        "chart": {"bars": [[int(b.t.timestamp()), round(b.o, 2), round(b.h, 2), round(b.l, 2), round(b.c, 2), round(b.v)] for b in h4],
                  "ema20": series(ema(closes, 20)), "ema50": series(ema(closes, 50)), "sma200": series(sma(closes, 200)),
                  "lines": [{"from": int(ln.start.timestamp()), "a": round(ln.start_price, 2), "to": epoch[-1], "b": round(ln.now, 2),
                             "falling": ln.falling} for ln in r.lines]},
    }


# --- what came of it ----------------------------------------------------------------------------------


def follow(report: dict, bars: list[Bar]) -> dict:
    """What price did after a report, on candles closed since: whether the key level held, and the targets reached.

    The key level is lost on a half-hour (or, with finer candles, the last one's) close below it."""
    at = datetime.fromisoformat(report["at"])
    end = datetime.combine(at.date(), CLOSE, tzinfo=at.tzinfo)
    after = [b for b in bars if at <= b.t < end]
    key = report["key"]["price"]
    out = {"status": "waiting", "since": None, "low": None, "high": None, "last": None, "lost_at": None, "reclaimed_at": None, "hit": []}
    if not after:
        return out
    half: dict[datetime, list[Bar]] = defaultdict(list)
    for b in after:
        half[b.t.replace(minute=b.t.minute // 30 * 30)].append(b)
    out |= {"low": min(b.l for b in after), "high": max(b.h for b in after), "last": after[-1].c}
    below = False
    for t, rows in sorted(half.items()):
        done = rows[-1].t + (rows[1].t - rows[0].t if len(rows) > 1 else timedelta(minutes=30)) >= t + timedelta(minutes=30)
        if not done:
            continue
        c = rows[-1].c
        closed = (t + timedelta(minutes=30)).isoformat()
        if c < key and not below:
            below, out["lost_at"], out["reclaimed_at"] = True, out["lost_at"] or closed, None
        elif c >= key and below:
            below, out["reclaimed_at"] = False, closed
    out["status"] = "lost" if below else "reclaimed" if out["lost_at"] else "holding"
    for x in report["bull"]["levels"]:
        hit = next((b for b in after if b.h >= x["near"]), None)
        if hit:
            out["hit"].append({"what": x["what"], "at": hit.t.isoformat()})
    for x in report["bear"]["levels"]:
        hit = next((b for b in after if b.l <= x["near"]), None)
        if hit:
            out["hit"].append({"what": x["what"], "at": hit.t.isoformat()})
    return out


# --- getting the candles, and keeping the reports --------------------------------------------------------


def gather(at: datetime, full: Callable[[int, datetime, datetime], list[Bar]], night: Callable[[datetime, datetime], list[Bar]],
           latest: Callable[[datetime], list[Bar]] | None = None) -> list[Bar]:
    """180 days of 30-minute candles up to `at`, around the clock: the full market's from 4 AM to 8 PM (`full(minutes, start,
    end)`, which is 16 minutes late), Robinhood's overnight ones (`night`), and for the last minutes the 5-minute candles of
    `latest` (live) or `full`, put together."""
    tz, start, half = at.tzinfo, at - timedelta(days=DAYS + 5), timedelta(minutes=30)

    def local(rows: list[Bar]) -> list[Bar]:
        return [Bar(b.t.astimezone(tz), b.o, b.h, b.l, b.c, b.v) for b in rows]

    def daytime(b: Bar) -> bool:
        return time(4) <= b.t.time() < time(20)

    day = [b for b in local(full(30, start, at)) if daytime(b) and b.t + half <= at]
    midnight = datetime.combine(at.date(), time(0), tzinfo=tz)
    five = local(latest(midnight) if latest else full(5, midnight, at))
    have = {b.t for b in day if not latest or b.t + half <= at - timedelta(minutes=17)}  # the late ones may be incomplete
    parts: dict[datetime, list[Bar]] = defaultdict(list)
    for b in five:
        t = b.t.replace(minute=b.t.minute // 30 * 30)
        if b.t + timedelta(minutes=5) <= at and daytime(b) and t not in have:
            parts[t].append(b)
    out = {b.t: b for b in local(night(start, at)) if not daytime(b) and b.t + half <= at}
    out |= {b.t: b for b in day if b.t in have or b.t not in parts} | {t: merge(rows, t) for t, rows in parts.items()}
    return [b for _, b in sorted(out.items())]


def traded_today(bars: list[Bar], at: datetime) -> bool:
    return any(b.t.date() == at.date() and OPEN <= b.t.time() < CLOSE for b in bars)


def save(folder: Path, report: dict, kind: str) -> Path:
    """kind: morning (the day's one) or now (made on request)."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{report['id']}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(report | {"kind": kind}))
    os.replace(tmp, path)
    return path


def saved(folder: Path) -> list[Path]:
    """Newest first."""
    return sorted(folder.glob("????-??-??-????-*.json"), reverse=True) if folder.is_dir() else []


def finish(folder: Path, symbol: str, bars: list[Bar], today: date) -> None:
    """Write what came of earlier days' reports into them, once, from the candles just downloaded."""
    for path in saved(folder)[:20]:
        report = json.loads(path.read_text())
        if report["symbol"] != symbol or report.get("outcome") or date.fromisoformat(report["day"]) >= today:
            continue
        outcome = follow(report, bars)
        if outcome["status"] != "waiting":
            save(folder, report | {"outcome": outcome}, report["kind"])


def as_text(report: dict) -> str:
    def rows(levels: list[dict]) -> list[str]:
        return [f"  {x['low']:.2f}{'' if x['low'] == x['high'] else ' - ' + format(x['high'], '.2f')}  {x['what']} ({'; '.join(x['why'])})" for x in levels]

    return "\n".join([report["title"], f"At {datetime.fromisoformat(report['at']):%Y-%m-%d %H:%M} ET, price {report['price']:.2f}.", "", *report["facts"],
                      "", "Bullish scenario: " + report["bull"]["text"], *rows(report["bull"]["levels"]), "", "Bearish scenario: " + report["bear"]["text"],
                      *rows(report["bear"]["levels"]), "", "Support:", *("  " + x for x in report["support"]), "", "Outlook:", *("  " + x for x in report["outlook"]),
                      "", "Scenarios from the chart, not a prediction and not advice."])
