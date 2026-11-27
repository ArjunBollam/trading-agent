"""Does a signal say what its candles say? Each one is worked out again here from scratch, straight from its textbook
definition, and not with the code that made it: the numbers in its text, the levels it names, the shape of its candles and
which side it closed on.

`faults` returns what doesn't hold, in words (an empty list: everything it says holds), or None for a kind with no check of
its own here. The signal audit (signal_audit.py) then relies on working the signal out again from fresh candles."""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from datetime import date, timedelta

from .config import ScanConfig
from .features import Bar, regular

CENT = 0.0101  # a text rounds its prices to the cent
WORDING = "wording: "  # starts a fault of the text only: the signal itself holds


def _num(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text)
    return float(m.group(1)) if m else None


def _ema(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * min(n - 1, len(values))
    if len(values) < n:
        return [None] * len(values)
    e = statistics.fmean(values[:n])
    out.append(e)
    for v in values[n:]:
        e += (v - e) * 2 / (n + 1)
        out.append(e)
    return out


def _rsi(closes: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= n:
        return out
    steps = [b - a for a, b in zip(closes, closes[1:], strict=False)]
    up, down = statistics.fmean(max(s, 0) for s in steps[:n]), statistics.fmean(max(-s, 0) for s in steps[:n])
    for i in range(n, len(closes)):
        if i > n:
            s = steps[i - 1]
            up, down = (up * (n - 1) + max(s, 0)) / n, (down * (n - 1) + max(-s, 0)) / n
        out[i] = 100 * up / (up + down) if up + down else 100.0
    return out


def _true_ranges(bars: list[Bar]) -> list[float]:
    return [max(b.h, p.c) - min(b.l, p.c) for p, b in zip(bars, bars[1:], strict=False)]


def average_range(bars: list[Bar], n: int = 14) -> float:
    """Wilder's average true range over the last 30 candles."""
    tr = _true_ranges(bars[-30:])
    if len(tr) < n:
        return 0.0
    a = statistics.fmean(tr[:n])
    for x in tr[n:]:
        a += (x - a) / n
    return a


def vwaps(today: list[Bar]) -> list[float | None]:
    out: list[float | None] = []
    money = shares = 0.0
    for b in today:
        if regular(b):
            money, shares = money + (b.h + b.l + b.c) / 3 * b.v, shares + b.v
        out.append(money / shares if shares else None)
    return out


def named_level(name: str, today: list[Bar], previous: list[Bar], bar_minutes: int) -> list[float]:
    """The prices a level name can stand for, before the last of `today`'s candles."""
    prev = [b for b in previous if regular(b)]
    if prev:
        h, lo, c = max(b.h for b in prev), min(b.l for b in prev), prev[-1].c
        p = (h + lo + c) / 3
        fixed = {"prior-day high": h, "prior-day low": lo, "prior-day close": c, "pivot S1": 2 * p - h, "pivot R1": 2 * p - lo,
                 "pivot S2": p - h + lo, "pivot R2": p + h - lo}
        if name in fixed:
            return [fixed[name]]
    rth = [b for b in today if regular(b)]
    opening = rth[:max(1, 15 // bar_minutes)]
    if name == "opening-range high" and opening:
        return [max(b.h for b in opening)]
    if name == "opening-range low" and opening:
        return [min(b.l for b in opening)]
    before = [b for b in today[:-3] if regular(b)]  # a swing the last candles made isn't a level to test yet
    if name == "high of day" and before:
        return [max(b.h for b in before)]
    if name == "low of day" and before:
        return [min(b.l for b in before)]
    return []


@dataclass
class _Candles:
    """The candles up to the signal's, newest last."""

    b: list[Bar]
    atr: float

    def rise(self, k: int) -> float:
        """How far price moved over the 5 candles before a pattern of `k` candles."""
        return self.b[-k - 1].c - self.b[-k - 6].c if len(self.b) >= k + 6 else 0.0


def _body(x: Bar) -> float:
    return abs(x.c - x.o)


def _top(x: Bar) -> float:
    return max(x.o, x.c)


def _low_body(x: Bar) -> float:
    return min(x.o, x.c)


def _upside_down(x: Bar) -> Bar:
    return Bar(x.t, -x.o, -x.l, -x.h, -x.c, x.v)


def _green(x: Bar) -> bool:
    return x.c > x.o


def _red(x: Bar) -> bool:
    return x.c < x.o


def _bullish_shapes(x: list[Bar], atr: float) -> dict[str, bool]:
    """What each bullish candle name means, on candles newest first. The bearish ones are these, upside down."""
    a, b, c = x[0], x[1], x[2]
    rng, body, upper, lower = a.h - a.l, _body(a), a.h - _top(a), _low_body(a) - a.l
    doji = rng > 0 and body <= 0.1 * rng
    engulf = _red(b) and _green(a) and a.o <= b.c and a.c >= b.o
    inside = _top(a) < b.o and _low_body(a) > b.c
    middle = (c.o + c.c) / 2
    return {
        "hammer": lower >= 2 * body and upper <= max(body, 0.1 * rng),
        "inverted_hammer": upper >= 2 * body and lower <= max(0.5 * body, 0.1 * rng),
        "dragonfly_doji": doji and lower >= 0.6 * rng and upper <= 0.1 * rng,
        "bullish_belt_hold": _green(a) and lower <= 0.05 * rng,
        "bullish_marubozu": _green(a) and upper + lower <= 0.05 * rng,
        "bullish_engulfing": engulf,
        "bullish_harami": _red(b) and inside and _green(a),
        "bullish_harami_cross": _red(b) and inside and doji,
        "homing_pigeon": _red(b) and inside and not _green(a),
        "piercing_line": _red(b) and _green(a) and a.o < b.c and (b.o + b.c) / 2 < a.c < b.o,
        "tweezer_bottom": _red(b) and _green(a) and abs(a.l - b.l) <= 0.05 * atr,
        "matching_low": _red(b) and _red(a) and abs(a.c - b.c) <= 0.05 * atr,
        "bullish_meeting_lines": _red(b) and _green(a) and abs(a.c - b.c) <= 0.1 * atr and a.o < b.c,
        "bullish_kicker": _red(b) and _green(a) and a.o > b.o,
        "bullish_separating_lines": _red(b) and _green(a) and abs(a.o - b.o) <= 0.1 * atr,
        "morning_star": _red(c) and _green(a) and a.c > middle and _top(b) <= c.c + 0.05 * atr,
        "morning_doji_star": _red(c) and _green(a) and a.c > middle and _body(b) <= 0.1 * (b.h - b.l),
        "bullish_abandoned_baby": _red(c) and _green(a) and a.c > middle and b.h < c.l and b.h < a.l,
        "three_white_soldiers": all(_green(v) for v in (a, b, c)) and c.c < b.c < a.c and c.o <= b.o <= c.c and b.o <= a.o <= b.c,
        "three_inside_up": _red(c) and _green(b) and _top(b) < c.o and _low_body(b) > c.c and _green(a) and a.c > c.o,
        "three_outside_up": _red(c) and _green(b) and b.o <= c.c and b.c >= c.o and _green(a) and a.c > b.c,
        "upside_tasuki_gap": _green(c) and _green(b) and b.o > c.c and _red(a) and c.c < a.c < b.o,
        "upside_gap_three_methods": _green(c) and _green(b) and b.o > c.c and _red(a) and c.o < a.c < c.c,
        "stick_sandwich": _red(c) and _green(b) and _red(a) and abs(a.c - c.c) <= 0.05 * atr,
        "unique_three_river": _red(c) and _red(b) and b.l < c.l and b.c > c.c and _green(a) and a.c < b.c,
    } | ({
        "bullish_three_line_strike": all(_red(v) for v in x[1:4]) and x[3].c > c.c > b.c and _green(a) and a.o <= b.c and a.c >= x[3].o,
    } if len(x) > 3 else {})


_UPSIDE_DOWN = {  # a bearish name: the bullish one it is, with the chart upside down
    "shooting_star": "hammer", "hanging_man": "inverted_hammer", "gravestone_doji": "dragonfly_doji",
    "bearish_belt_hold": "bullish_belt_hold", "bearish_marubozu": "bullish_marubozu", "bearish_engulfing": "bullish_engulfing",
    "bearish_harami": "bullish_harami", "bearish_harami_cross": "bullish_harami_cross", "dark_cloud_cover": "piercing_line",
    "tweezer_top": "tweezer_bottom", "bearish_meeting_lines": "bullish_meeting_lines", "bearish_kicker": "bullish_kicker",
    "bearish_separating_lines": "bullish_separating_lines", "evening_star": "morning_star", "evening_doji_star": "morning_doji_star",
    "bearish_abandoned_baby": "bullish_abandoned_baby", "three_black_crows": "three_white_soldiers",
    "three_inside_down": "three_inside_up", "three_outside_down": "three_outside_up", "downside_tasuki_gap": "upside_tasuki_gap",
    "downside_gap_three_methods": "upside_gap_three_methods", "bearish_three_line_strike": "bullish_three_line_strike",
}
# how many candles a shape has, for the move it has to come after
_LENGTH = {"hammer": 1, "inverted_hammer": 1, "dragonfly_doji": 1, "bullish_belt_hold": 1, "bullish_marubozu": 1,
           "bullish_three_line_strike": 4} | dict.fromkeys(
    ("morning_star", "morning_doji_star", "bullish_abandoned_baby", "three_white_soldiers", "three_inside_up", "three_outside_up",
     "upside_tasuki_gap", "upside_gap_three_methods", "stick_sandwich", "unique_three_river"), 3)


def _candle(kind: str, detail: str, c: _Candles) -> list[str] | None:
    flip = kind in _UPSIDE_DOWN
    name = _UPSIDE_DOWN.get(kind, kind)
    bars = [_upside_down(x) for x in c.b[-5:]] if flip else c.b[-5:]
    shapes = _bullish_shapes(bars[::-1], c.atr) if len(bars) >= 4 else {}
    if name not in shapes:
        return None
    out = [] if shapes[name] else ["the candles don't have that shape"]
    rise = c.rise(_LENGTH.get(name, 2))
    if "after a decline" in detail or "in a decline" in detail:
        out += [] if rise < 0 else [f"price hadn't fallen over the 5 candles before ({rise:+.2f})"]
    if "after a rise" in detail or "in a rise" in detail:
        out += [] if rise > 0 else [f"price hadn't risen over the 5 candles before ({rise:+.2f})"]
    return out


def _find_points(points: list[float], bars: list[Bar]) -> list[int] | None:
    """Candles, in order, whose highs and lows (taking turns) are these prices."""
    for start_high in (True, False):
        at, found = 0, []
        for n, p in enumerate(points):
            high = start_high == (n % 2 == 0)
            i = next((i for i in range(at, len(bars)) if abs((bars[i].h if high else bars[i].l) - p) < CENT), None)
            if i is None:
                break
            found.append(i)
            at = i
        if len(found) == len(points):
            return found
    return None


def _points(detail: str) -> list[float]:
    m = re.search(r"swing points ([\d., ]+?) since", detail)
    return [float(x) for x in m.group(1).split(",")] if m else []


def _chart_pattern(kind: str, side: str, detail: str, bars: list[Bar], atr: float, forming: bool) -> list[str] | None:
    pts = _points(detail)
    line = _num(r"(?:line|neckline) ([\d.]+)", detail)
    if not pts or line is None:
        return None
    out, last, prev = [], bars[-1], bars[-2]
    found = _find_points(pts, bars[-140:])
    if found is None:
        out.append("not every swing point is a high or low of its candles")
    top = side == "sell"
    name = kind.removesuffix("_forming")
    if name in ("double_top", "double_bottom", "triple_top", "triple_bottom"):
        inner = pts[1::2]
        want = min(inner) if top else max(inner)
        if abs(want - line) >= CENT:
            out.append(f"the neckline should be {want:.2f}")
        ends = pts[0::2]
        if max(ends) - min(ends) > 0.8 * atr:
            out.append(f"its {'tops' if top else 'bottoms'} are {max(ends) - min(ends):.2f} apart, more than 0.8 ATR")
    if name in ("head_and_shoulders", "inverse_head_and_shoulders") and len(pts) == 5:
        s = 1 if name == "head_and_shoulders" else -1
        if not s * pts[2] > max(s * pts[0], s * pts[4]):
            out.append("the head isn't beyond both shoulders")
    if forming:
        if (last.c < line - 0.25 * atr) if top else (last.c > line + 0.25 * atr):
            out.append(f"price ({last.c:.2f}) had already closed through the neckline {line:.2f}")
        gap = _num(r"([\d.]+) ATR away", detail)
        if gap is not None and atr and abs(abs(last.c - line) / atr - gap) > 0.15:
            out.append(f"it was {abs(last.c - line) / atr:.1f} ATR from the neckline, not {gap}")
    else:
        up = "closed above" in detail
        if not (last.c > line if up else last.c < line):
            out.append(f"it closed at {last.c:.2f}, not {'above' if up else 'below'} {line:.2f}")
        flat = name in ("double_top", "double_bottom", "triple_top", "triple_bottom")  # a sloping line was elsewhere a candle earlier
        if flat and ((prev.c > line + CENT) if up else (prev.c < line - CENT)):
            out.append(f"the candle before had already closed through it ({prev.c:.2f})")
    return out


def _wording(detail: str, level: float, extreme: float, up: bool) -> list[str]:
    """A text that says a level was tested or reached needs the candle to have got there, or to say how close it came."""
    word = "high" if up else "low"
    near = _num(r"within ([\d.]+) of", detail)
    short = level - extreme if up else extreme - level
    out = []
    if short > CENT and near is None:
        out.append(f"{WORDING}it came within {short:.2f} of {level:.2f} but didn't reach it ({word} {extreme:.2f})")
    if near is not None and abs(short - near) > CENT:
        out.append(f"the {word} came within {short:.2f}, not {near:.2f}")
    said = _num(rf"\({word} ([\d.]+)\)", detail)
    if said is not None and abs(said - extreme) > CENT:
        out.append(f"the {word} was {extreme:.2f}, not {said:.2f}")
    return out


def faults(kind: str, side: str, price: float, detail: str, level: str | None, bars: list[Bar], day: date, cfg: ScanConfig,
           daily: list[Bar] | None = None) -> list[str] | None:
    """What doesn't hold about a scanner signal on the last of `bars` (the sessions it was worked out from, oldest first)."""
    if len(bars) < 8:
        return None
    last, prev = bars[-1], bars[-2]
    today = [b for b in bars if b.t.date() == day]
    closes = [b.c for b in bars]
    atr = average_range(bars)
    out: list[str] = []
    off = max(CENT, 0.0002 * last.c)  # an indicator's value can move this much when a late trade corrects a candle

    def need(holds: bool, text: str) -> None:
        if not holds:
            out.append(text)

    def value(name: str, real: float | None, said: float | None, slack: float = off) -> None:
        if real is not None and said is not None and abs(real - said) > slack:
            out.append(f"{name} was {real:.2f}, not {said:.2f}")

    need(abs(last.c - price) <= max(CENT, 0.0005 * last.c), f"the candle closed at {last.c:.2f}, not {price:.2f}")

    if kind in ("vwap_coil_breakout", "vwap_coil_breakdown"):
        return out + _coil(kind, detail, today, cfg, off)

    if kind.startswith("vwap_"):
        vw = vwaps(today)
        if len(vw) < 2 or vw[-1] is None or vw[-2] is None:
            return out + ["there was no VWAP yet"]
        said = _num(r"VWAP ([\d.]+)", detail)
        value("VWAP", vw[-1], said)
        up = kind in ("vwap_reclaim", "vwap_bounce")
        need(last.c > vw[-1] if up else last.c < vw[-1], f"it closed at {last.c:.2f}, VWAP {vw[-1]:.2f}")
        came = prev.c < vw[-2] if kind in ("vwap_reclaim", "vwap_reject") else prev.c > vw[-2]
        need(came, f"the candle before closed at {prev.c:.2f}, on the same side of VWAP {vw[-2]:.2f}")
        if kind in ("vwap_bounce", "vwap_reject") and said is not None:
            out += _wording(detail, said, last.l if up else last.h, up=not up)
        return out

    if kind.startswith("rsi_"):
        r = _rsi(closes, cfg.rsi_period)
        if r[-1] is None or r[-2] is None:
            return None
        value("RSI", r[-1], _num(r"(?:fell|rose)(?: back)? to ([\d.]+)", detail), 0.35)
        lo, hi, slack = cfg.rsi_oversold, cfg.rsi_overbought, 0.35
        ok = {"rsi_oversold": r[-2] >= lo - slack and r[-1] < lo + slack, "rsi_oversold_exit": r[-2] < lo + slack and r[-1] >= lo - slack,
              "rsi_overbought": r[-2] <= hi + slack and r[-1] > hi - slack, "rsi_overbought_exit": r[-2] > hi - slack and r[-1] <= hi + slack}
        need(ok.get(kind, True), f"RSI went from {r[-2]:.1f} to {r[-1]:.1f}")
        return out

    if kind.startswith("ema_cross") or kind.startswith("macd_cross"):
        if kind.startswith("ema"):
            fast, slow = _ema(closes, 9), _ema(closes, 21)
            said = re.search(r"\(([\d.]+)\).*\(([\d.]+)\)", detail)
        else:
            line = [x - y if x is not None and y is not None else None for x, y in zip(_ema(closes, 12), _ema(closes, 26), strict=True)]
            known = [x for x in line if x is not None]
            fast, slow = line, [None] * (len(line) - len(known)) + _ema(known, 9)
            said = re.search(r"MACD (-?[\d.]+) .* (-?[\d.]+?)(?:;|$)", detail)
        if None in (fast[-1], fast[-2], slow[-1], slow[-2]):
            return None
        now, before, slack = fast[-1] - slow[-1], fast[-2] - slow[-2], 0.02 * atr
        up = kind.endswith("up")
        need(now > -slack and before <= slack if up else now < slack and before >= -slack, f"the gap went from {before:+.3f} to {now:+.3f}")
        if said:
            value("the fast line", fast[-1], float(said.group(1)), max(off, 0.05 * atr))
            value("the slow line", slow[-1], float(said.group(2)), max(off, 0.05 * atr))
        return out

    if kind.startswith("bb_reentry") or kind.startswith("squeeze_breakout"):
        def band(values: list[float]) -> tuple[float, float]:
            w = values[-20:]
            return statistics.fmean(w) - 2 * statistics.pstdev(w), statistics.fmean(w) + 2 * statistics.pstdev(w)
        (lo, hi), (lo1, hi1) = band(closes), band(closes[:-1])
        up = kind.endswith("up")
        if kind.startswith("bb"):
            need(prev.c < lo1 + off if up else prev.c > hi1 - off, f"the candle before closed at {prev.c:.2f}, band {lo1 if up else hi1:.2f}")
            need(last.c > lo - off if up else last.c < hi + off, f"it closed at {last.c:.2f}, band {lo if up else hi:.2f}")
            value("the band", lo if up else hi, _num(r"band ([\d.]+)", detail), max(off, 0.05 * atr))
        else:
            need(last.c > hi - off if up else last.c < lo + off, f"it closed at {last.c:.2f}, band {hi if up else lo:.2f}")
            value("the band", hi if up else lo, _num(r"closed (?:above|below) ([\d.]+)", detail), max(off, 0.05 * atr))
        return out

    if kind in ("support_test", "resistance_test", "breakout", "breakdown") or (
            kind in ("bullish_engulfing", "bearish_engulfing", "hammer", "shooting_star") and re.search(r" (?:at|near) the ", detail)):
        m = re.search(r"(?:the|of the) (prior-day high|prior-day low|prior-day close|pivot [SR][12]|opening-range high|opening-range low|"
                      r"high of day|low of day|VWAP) ([\d.]+)", detail)
        if not m:
            return out + ["its level isn't named"]
        name, at = m.group(1), float(m.group(2))
        before = sorted({b.t.date() for b in bars if b.t.date() < day})
        previous = [b for b in bars if before and b.t.date() == before[-1]]
        real = [v for v in vwaps(today)[-1:] if v] if name == "VWAP" else named_level(name, today, previous, cfg.bar_minutes)
        if real:
            value(f"the {name}", min(real, key=lambda v: abs(v - at)), at, max(CENT, 0.0001 * last.c))
        else:
            out.append(f"there was no {name}")
        if kind == "breakout":
            need(prev.c <= at + CENT and last.c > at - CENT, f"it went from {prev.c:.2f} to {last.c:.2f}")
        elif kind == "breakdown":
            need(prev.c >= at - CENT and last.c < at + CENT, f"it went from {prev.c:.2f} to {last.c:.2f}")
        elif kind == "support_test":
            need(last.c > at - CENT, f"it closed at {last.c:.2f}, below it")
            out += _wording(detail, at, last.l, up=False)
        elif kind == "resistance_test":
            need(last.c < at + CENT, f"it closed at {last.c:.2f}, above it")
            out += _wording(detail, at, last.h, up=True)
        else:
            out += _candle(kind, "", _Candles(bars, atr)) or []
            buy = side == "buy"
            need(last.c > at - CENT if buy else last.c < at + CENT,
                 f"it closed at {last.c:.2f}, {'below' if buy else 'above'} the {name}, which isn't {'support' if buy else 'resistance'} then")
            extreme = last.l if buy else last.h
            if " at the " in detail and (extreme > at + CENT if buy else extreme < at - CENT):
                out.append(f"{WORDING}it was near the {name}, not at it ({'low' if buy else 'high'} {extreme:.2f}, level {at:.2f})")
        return out + _volume(detail, bars)

    if kind.startswith("volume_spike"):
        need(_green(last) if kind.endswith("up") else _red(last), "the candle has the other colour")
        return out + _volume(detail.replace("volume ", "on ").replace("x the", "x volume the"), bars)

    if kind.startswith("williams_fractal"):
        high = kind.endswith("high")
        m, sides = bars[-3], bars[-5:-3] + bars[-2:]
        said = re.search(r"swing (?:high|low) ([\d.]+) at (\d\d:\d\d)", detail)
        need(m.h > max(x.h for x in sides) if high else m.l < min(x.l for x in sides), "the candles either side aren't all inside it")
        if said:
            value("the swing", m.h if high else m.l, float(said.group(1)), CENT)
            need(f"{m.t:%H:%M}" == said.group(2), f"the middle candle is {m.t:%H:%M}")
        return out

    if kind.startswith("zigzag_step"):
        n = [float(v) for v in re.findall(r"\d+\.\d+", detail)[:3]]
        up = kind.endswith("up")
        if len(n) == 3:
            second, first, line = n
            need(second > first if up else second < first, f"{second:.2f} isn't {'above' if up else 'below'} {first:.2f}")
            need(prev.c <= line + CENT < last.c + 2 * CENT if up else prev.c >= line - CENT > last.c - 2 * CENT,
                 f"it went from {prev.c:.2f} to {last.c:.2f}, the swing is {line:.2f}")
            need(_find_points([first, line, second], bars) is not None, "not every swing is a high or low of its candles")
        return out

    if kind.startswith("fib_"):
        m = re.search(r"([\d.]+)% retracement of the ([\d.]+)→([\d.]+) swing, ([\d.]+)", detail)
        if m:
            share, start, end, at = (float(v) for v in m.groups())
        elif m := re.search(r"([\d.]+)% retracement ([\d.]+) of the ([\d.]+)→([\d.]+) swing", detail):  # as it was written before
            share, at, start, end = (float(v) for v in m.groups())
        else:
            return out + ["its swing isn't in the text"]
        value("the retracement", end - share / 100 * (end - start), at, 2 * CENT)
        up = kind == "fib_support"
        need(last.c > at - CENT if up else last.c < at + CENT, f"it closed at {last.c:.2f}")
        out += _wording(detail, at, last.l if up else last.h, up=not up)
        need(_find_points([start, end], bars[-140:]) is not None, "its swing isn't a low and a high of its candles")
        return out

    if "price turned" in detail:
        m = re.search(r"at D \(([^;]*);(.*)\)", detail)
        pts = [float(v) for v in re.findall(r"\d+\.\d+", m.group(1))] if m else []
        if len(pts) < 3:
            return None
        if kind.endswith("three_drives"):
            need(pts[0] > pts[1] > pts[2] if side == "buy" else pts[0] < pts[1] < pts[2], "the drives don't step one way")
            return out
        need(_find_points(pts, bars[-140:]) is not None, "not every point is a high or low of its candles")
        a, b, c, d = pts[-4:]
        rough = 0.03 + 2 * CENT / max(abs(a - b), CENT)  # the prices in the text are rounded
        for pattern, real in ((r"CD ([\d.]+) of AB", (c - d) / (a - b)), (r"C at ([\d.]+) of AB", (c - b) / (a - b))):
            said = _num(pattern, m.group(2))
            if said is not None:
                need(abs(real - said) <= rough, f"{pattern.split('(')[0].strip()} is {real:.2f} of AB, not {said:.2f}")
        if len(pts) == 5:
            x = pts[0]
            for pattern, real in ((r"AB ([\d.]+) of XA", (a - b) / (a - x)), (r"D at ([\d.]+) of XA", (a - d) / (a - x))):
                said = _num(pattern, m.group(2))
                need(said is None or abs(real - said) <= 0.03 + 2 * CENT / max(abs(a - x), CENT), f"its ratio is {real:.2f}, not {said}")
        return out

    if kind in ("golden_cross", "death_cross") and daily:
        dc = [b.c for b in daily if b.t.date() < day]
        if len(dc) < 200:
            return None
        before = statistics.fmean(dc[-50:]) - statistics.fmean(dc[-200:])
        now = statistics.fmean((dc + [last.c])[-50:]) - statistics.fmean((dc + [last.c])[-200:])
        need(before <= 0.01 and now > -0.01 if kind == "golden_cross" else before >= -0.01 and now < 0.01,
             f"the averages' gap went from {before:+.2f} to {now:+.2f}")
        return out

    if kind in ("high_52w", "low_52w") and daily:
        year = [b for b in daily if day - timedelta(days=365) <= b.t.date() < day]
        if not year:
            return None
        edge = max(b.h for b in year) if kind == "high_52w" else min(b.l for b in year)
        value("the 52-week level", edge, _num(r"(?:high|low) ([\d.]+)", detail), CENT)
        need(last.c > edge if kind == "high_52w" else last.c < edge, f"it closed at {last.c:.2f}, the level is {edge:.2f}")
        return out

    if kind in ("doji", "long_legged_doji", "spinning_top"):
        rng = last.h - last.l
        need(rng > 0 and _body(last) <= (0.3 if kind == "spinning_top" else 0.1) * rng, "its body isn't small")
        rise = _Candles(bars, atr).rise(1)
        need(rise > 0 if "after a rise" in detail else rise < 0, f"price moved {rise:+.2f} over the 5 candles before")
        return out

    if "swing points" in detail:
        more = _chart_pattern(kind, side, detail, bars, atr, kind.endswith("_forming"))
        return None if more is None else out + more + _volume(detail, bars)

    shape = _candle(kind, detail, _Candles(bars, atr))
    return None if shape is None else out + shape


def _coil(kind: str, detail: str, today: list[Bar], cfg: ScanConfig, off: float) -> list[str]:
    """A VWAP coil break: the box is the low and high of the candles it names, VWAP runs through it, its volume is the
    share of the morning's it says, and the last candle closed out of it, past VWAP, from inside."""
    m = re.search(r"coiled ([\d.]+)-([\d.]+) around a flat VWAP ([\d.]+) for \d+ min \((\d\d:\d\d)-(\d\d:\d\d)\)", detail)
    if not m or len(today) < 3:
        return ["wording: the text doesn't name the box"]
    lo, hi, said_vw = float(m.group(1)), float(m.group(2)), float(m.group(3))
    t0, t1 = m.group(4), m.group(5)
    box = [b for b in today[:-1] if regular(b) and t0 <= f"{b.t:%H:%M}" <= t1]
    out: list[str] = []
    if len(box) < cfg.coil_min_bars:
        return [f"the box has {len(box)} candles, fewer than {cfg.coil_min_bars}"]
    if abs(min(b.l for b in box) - lo) > CENT:
        out.append(f"the box's low was {min(b.l for b in box):.2f}, not {lo:.2f}")
    if abs(max(b.h for b in box) - hi) > CENT:
        out.append(f"the box's high was {max(b.h for b in box):.2f}, not {hi:.2f}")
    vw = vwaps(today)
    last, prev = today[-1], today[-2]
    if vw[-1] is not None and abs(vw[-1] - said_vw) > off:
        out.append(f"VWAP was {vw[-1]:.2f}, not {said_vw:.2f}")
    end_vw = vw[today.index(box[-1])]
    if end_vw is not None and not min(b.l for b in box) < end_vw < max(b.h for b in box):
        out.append(f"VWAP {end_vw:.2f} wasn't inside the box when it ended")
    up = kind == "vwap_coil_breakout"
    edge = max(b.h for b in box) if up else min(b.l for b in box)
    if vw[-1] is not None and not ((last.c > edge and last.c > vw[-1]) if up else (last.c < edge and last.c < vw[-1])):
        out.append(f"it closed at {last.c:.2f}, not {'above' if up else 'below'} both the box {edge:.2f} and VWAP {vw[-1]:.2f}")
    if box[-1] is prev and not ((prev.c <= edge) if up else (prev.c >= edge)):
        out.append(f"the candle before closed at {prev.c:.2f}, already out of the box")
    before = [b.v for b in today if regular(b) and f"{b.t:%H:%M}" < t0]
    said_dry = _num(r"on ([\d.]+)x the volume before it", detail)
    if before and sum(before) and said_dry is not None:
        real = statistics.fmean(b.v for b in box) / statistics.fmean(before)
        if abs(real - said_dry) > 0.02:
            out.append(f"the box traded {real:.2f}x the volume before it, not {said_dry:.2f}x")
    return out


def _volume(detail: str, bars: list[Bar]) -> list[str]:
    said = _num(r"on (?:light volume \()?([\d.]+)x(?: volume|\))", detail)
    before = [b.v for b in bars[:-1] if regular(b) == regular(bars[-1])][-20:]
    if said is None or not before or not sum(before):
        return []
    real = bars[-1].v / statistics.fmean(before)
    return [] if abs(real - said) <= max(0.15, 0.1 * said) else [f"volume was {real:.1f}x, not {said:.1f}x"]


def live_cross_faults(side: str, price: float, level: float, around: list[Bar]) -> list[str] | None:
    """A live cross against the 1-minute candles around it: did the price trade there, and at the level?"""
    if not around:
        return None
    hi, lo = max(b.h for b in around), min(b.l for b in around)
    slack = max(CENT, 0.0002 * price)
    out = [] if lo - slack <= price <= hi + slack else [f"{price:.2f} didn't trade then ({lo:.2f} to {hi:.2f})"]
    if not (price > level - CENT if side == "buy" else price < level + CENT):
        out.append(f"{price:.2f} isn't {'above' if side == 'buy' else 'below'} {level:.2f}")
    if not lo - slack <= level <= hi + slack:
        out.append(f"price didn't cross {level:.2f} then ({lo:.2f} to {hi:.2f})")
    return out


# --- the labels the charts draw (qqe.py, overlays.py, smc.py), worked out again the long way ---


def qqe_labels(closes: list[float], cfg: ScanConfig) -> dict[int, str]:
    """{candle: long | short} for QQE, written line by line as the published script runs."""
    raw = _rsi(closes, cfg.qqe_rsi_period)
    first = next((i for i, v in enumerate(raw) if v is not None), len(raw))
    rsi = [None] * first + _ema([v for v in raw[first:] if v is not None], cfg.qqe_smoothing)
    first = next((i for i, v in enumerate(rsi) if v is not None), len(rsi))
    moves = [abs(b - a) for a, b in zip(rsi[first:], rsi[first + 1:], strict=False)]
    n = cfg.qqe_rsi_period * 2 - 1
    once = _ema(moves, n)
    start = next((i for i, v in enumerate(once) if v is not None), len(once))
    twice = [None] * start + _ema([v for v in once[start:] if v is not None], n)
    width: list[float | None] = [None] * (first + 1) + [v * cfg.qqe_factor if v is not None else None for v in twice]
    out: dict[int, str] = {}
    low, high, trend, was = [None, None], [None, None], 1, None  # [two candles ago, last candle]
    for i in range(1, len(closes)):
        if width[i] is None:
            continue
        r, r1, w = rsi[i], rsi[i - 1], width[i]
        new_low, new_high = r - w, r + w
        if low[1] is not None and r1 > low[1] and r > low[1]:
            new_low = max(low[1], new_low)
        if high[1] is not None and r1 < high[1] and r < high[1]:
            new_high = min(high[1], new_high)
        if high[0] is not None and ((r > high[1]) != (r1 > high[0]) and r != high[1]):
            trend = 1
        elif low[0] is not None and ((r < low[1]) != (r1 < low[0]) and r != low[1]):
            trend = -1
        side = r - (new_low if trend == 1 else new_high)
        now = "long" if side > 0 else "short" if side < 0 else None
        if was is not None and now and now != was[0]:
            out[i] = now
        was = (now,)
        low, high = [low[1], new_low], [high[1], new_high]
    return out


def level_break_labels(bars: list[Bar], cfg: ScanConfig) -> dict[int, tuple[str, str, float]]:
    """{candle: (side, break | wick, level)} for support and resistance breaks."""
    n = cfg.sr_pivot_bars
    highs, lows = [b.h for b in bars], [b.l for b in bars]
    tops = [j for j in range(n, len(bars) - n) if highs[j] == max(highs[j - n:j + n + 1]) and highs[j + 1:j + n + 1].count(highs[j]) == 0]
    bottoms = [j for j in range(n, len(bars) - n) if lows[j] == min(lows[j - n:j + n + 1]) and lows[j + 1:j + n + 1].count(lows[j]) == 0]
    vols = [b.v for b in bars]
    fast, slow = _ema(vols, 5), _ema(vols, 10)
    out: dict[int, tuple[str, str, float]] = {}

    def level(pivots: list[int], prices: list[float], i: int) -> float | None:
        return next((prices[j] for j in reversed(pivots) if j + n < i), None)

    for i in range(1, len(bars)):
        b, p = bars[i], bars[i - 1]
        loud = slow[i] is not None and bool(slow[i]) and 100 * (fast[i] / slow[i] - 1) > cfg.sr_volume
        hi, hi1 = level(tops, highs, i), level(tops, highs, i - 1)
        lo, lo1 = level(bottoms, lows, i), level(bottoms, lows, i - 1)
        if None not in (hi, hi1) and p.c <= hi1 and b.c > hi and (b.o - b.l > b.c - b.o or loud):
            out[i] = ("long", "wick" if b.o - b.l > b.c - b.o else "break", hi)
        if None not in (lo, lo1) and p.c >= lo1 and b.c < lo and (b.h - b.o > b.o - b.c or loud):
            out[i] = ("short", "wick" if b.h - b.o > b.o - b.c else "break", lo)
    return out


def squeeze_labels(bars: list[Bar], cfg: ScanConfig) -> dict[int, str]:
    """{candle: long | short} for squeeze releases."""
    n, closes = cfg.squeeze_length, [b.c for b in bars]
    tr = [bars[0].h - bars[0].l] + _true_ranges(bars) if bars else []

    def inside(i: int) -> float:
        w = closes[i - n + 1:i + 1]
        return cfg.squeeze_kc_mult * statistics.fmean(tr[i - n + 1:i + 1]) - cfg.squeeze_bb_mult * statistics.pstdev(w)

    def off_middle(i: int) -> float:
        w = bars[i - n + 1:i + 1]
        return closes[i] - ((max(b.h for b in w) + min(b.l for b in w)) / 2 + statistics.fmean(b.c for b in w)) / 2

    out: dict[int, str] = {}
    for i in range(2 * n - 2, len(bars)):
        if inside(i) < 0 < inside(i - 1):
            fit = statistics.linear_regression(range(n), [off_middle(k) for k in range(i - n + 1, i + 1)])
            end = fit.intercept + fit.slope * (n - 1)
            if end:
                out[i] = "long" if end > 0 else "short"
    return out


def structure_faults(bars: list[Bar], i: int, pivot: int, level: float, side: str, size: int) -> list[str]:
    """A BOS or CHoCH: is the pivot one, and is this the first close through it?"""
    out, up = [], side == "long"
    p = bars[pivot]
    if abs((p.h if up else p.l) - level) >= CENT:
        out.append(f"the {'high' if up else 'low'} at {p.t:%H:%M} was {p.h if up else p.l:.2f}, not {level:.2f}")
    after = bars[pivot + 1:pivot + 1 + size]
    if len(after) < size or pivot + size > i or not (p.h > max(b.h for b in after) if up else p.l < min(b.l for b in after)):
        out.append(f"{p.t:%H:%M} isn't a pivot: the {size} candles after it aren't all {'lower' if up else 'higher'}")
    if not (bars[i].c > level if up else bars[i].c < level):
        out.append(f"it closed at {bars[i].c:.2f}, not {'above' if up else 'below'} {level:.2f}")
    known = pivot + size  # only closes after the pivot was known count as breaking it
    if any((b.c > level if up else b.c < level) and (a.c <= level if up else a.c >= level)
           for a, b in zip(bars[known:i - 1], bars[known + 1:i], strict=False)):
        out.append("an earlier candle had already closed through it")
    return out


def zone_faults(bars: list[Bar], at: int, top: float, bottom: float, side: str) -> list[str]:
    """An order block still drawn: are its edges that candle's, and has price not traded through it since the candle after?"""
    b, out = bars[at], []
    lo, hi = sorted((top, bottom))  # a very wide candle's edges are swapped
    if abs(b.h - hi) >= CENT or abs(b.l - lo) >= CENT:
        out.append(f"the {b.t:%H:%M} candle runs {b.l:.2f} to {b.h:.2f}, not {lo:.2f} to {hi:.2f}")
    elif top > bottom and (bars[-1].l < lo if side == "long" else bars[-1].h > hi):
        out.append(f"price has traded through it ({bars[-1].l:.2f} to {bars[-1].h:.2f})")
    return out
