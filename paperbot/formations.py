"""Chart, candlestick and Fibonacci patterns for `paperbot scan`, each an event on the last completed bar.

Classic patterns are built from swing highs and lows (a zigzag: a swing counts once price moves
scan.pattern_swing_atr ATRs away from it) and reported when the last bar closes through the
pattern's line, by more than scan.min_cross_atr ATRs:
  double top / bottom, triple top / bottom, head and shoulders and inverse: through the neckline
  ascending, descending and symmetrical triangles, rising and falling wedges, channels up and down,
  rectangles: through either trendline, whichever way it breaks
  flags and pennants: a sharp move (the pole), a small pause against it, and a break in its direction
Candlestick patterns (about 55, one to five bars): reversals count only after a move the other way
  (the 5 bars before the pattern rose or fell 0.75 ATR or more). Doji, long-legged doji and spinning
  tops are indecision: weak (printed, not alerted).
Fibonacci: AB=CD, Gartley, Bat, Butterfly, Crab and Three Drives, reported when price turns at D
  (the zigzag confirms D on the last bar); and a first test that held at the 38.2/50/61.8/78.6%
  retracement of the latest big swing (scan.fib_swing_atr ATRs or more).

Every pattern is written once, for one direction; the other direction runs the same code on the
chart turned upside down (mirror), so a double top on the mirrored chart is a double bottom.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import ScanConfig
from .features import Bar

Hit = tuple[str, str, str, bool, str | None]  # kind, side, detail, weak, level

LABELS = {
    # classic
    "double_top": "Double top", "double_bottom": "Double bottom",
    "triple_top": "Triple top", "triple_bottom": "Triple bottom",
    "head_and_shoulders": "Head and shoulders", "inverse_head_and_shoulders": "Inverse head and shoulders",
    "ascending_triangle": "Ascending triangle", "descending_triangle": "Descending triangle",
    "triangle": "Symmetrical triangle", "rising_wedge": "Rising wedge", "falling_wedge": "Falling wedge",
    "channel_up": "Channel up", "channel_down": "Channel down", "rectangle": "Rectangle",
    "bull_flag": "Bull flag", "bear_flag": "Bear flag", "bull_pennant": "Bull pennant", "bear_pennant": "Bear pennant",
    # Fibonacci
    "bullish_gartley": "Bullish Gartley", "bearish_gartley": "Bearish Gartley",
    "bullish_bat": "Bullish Bat", "bearish_bat": "Bearish Bat",
    "bullish_butterfly": "Bullish Butterfly", "bearish_butterfly": "Bearish Butterfly",
    "bullish_crab": "Bullish Crab", "bearish_crab": "Bearish Crab",
    "bullish_abcd": "Bullish AB=CD", "bearish_abcd": "Bearish AB=CD",
    "bullish_three_drives": "Bullish Three Drives", "bearish_three_drives": "Bearish Three Drives",
    "fib_support": "Fibonacci retracement held", "fib_resistance": "Fibonacci retracement rejected",
}

CANDLES = {  # kind: (label, what it is)
    "hammer": ("Hammer", "small body, long lower wick, after a decline"),
    "shooting_star": ("Shooting star", "small body, long upper wick, after a rise"),
    "inverted_hammer": ("Inverted hammer", "small body, long upper wick, after a decline"),
    "hanging_man": ("Hanging man", "small body, long lower wick, after a rise"),
    "dragonfly_doji": ("Dragonfly doji", "opened and closed at the high with a long lower wick, after a decline"),
    "gravestone_doji": ("Gravestone doji", "opened and closed at the low with a long upper wick, after a rise"),
    "bullish_belt_hold": ("Bullish belt hold", "long green bar that opened at its low, after a decline"),
    "bearish_belt_hold": ("Bearish belt hold", "long red bar that opened at its high, after a rise"),
    "bullish_marubozu": ("Bullish marubozu", "long green bar with no wicks"),
    "bearish_marubozu": ("Bearish marubozu", "long red bar with no wicks"),
    "bullish_engulfing": ("Bullish engulfing", "green body engulfed the prior red body, after a decline"),
    "bearish_engulfing": ("Bearish engulfing", "red body engulfed the prior green body, after a rise"),
    "bullish_harami": ("Bullish harami", "small green body inside the prior long red body, after a decline"),
    "bearish_harami": ("Bearish harami", "small red body inside the prior long green body, after a rise"),
    "bullish_harami_cross": ("Bullish harami cross", "doji inside the prior long red body, after a decline"),
    "bearish_harami_cross": ("Bearish harami cross", "doji inside the prior long green body, after a rise"),
    "homing_pigeon": ("Homing pigeon", "small red body inside the prior long red body, after a decline"),
    "piercing_line": ("Piercing line", "green bar opened below the prior red close and closed above its midpoint"),
    "dark_cloud_cover": ("Dark cloud cover", "red bar opened above the prior green close and closed below its midpoint"),
    "tweezer_bottom": ("Tweezer bottom", "red bar then green bar with the same low, after a decline"),
    "tweezer_top": ("Tweezer top", "green bar then red bar with the same high, after a rise"),
    "matching_low": ("Matching low", "two red bars closing at the same price, after a decline"),
    "bullish_meeting_lines": ("Bullish meeting lines", "green bar opened far lower and closed at the prior red close"),
    "bearish_meeting_lines": ("Bearish meeting lines", "red bar opened far higher and closed at the prior green close"),
    "bullish_kicker": ("Bullish kicker", "long green bar opened above the prior long red bar's open"),
    "bearish_kicker": ("Bearish kicker", "long red bar opened below the prior long green bar's open"),
    "bullish_separating_lines": ("Bullish separating lines", "in a rise, a green bar opened where the prior red bar opened"),
    "bearish_separating_lines": ("Bearish separating lines", "in a decline, a red bar opened where the prior green bar opened"),
    "on_neck": ("On neck", "in a decline, a green bar closed only back at the prior red bar's low"),
    "in_neck": ("In neck", "in a decline, a green bar closed just inside the prior red body"),
    "thrusting": ("Thrusting", "in a decline, a green bar closed into the prior red body but below its midpoint"),
    "morning_star": ("Morning star", "long red bar, small bar below it, green bar closing above the red bar's midpoint"),
    "evening_star": ("Evening star", "long green bar, small bar above it, red bar closing below the green bar's midpoint"),
    "morning_doji_star": ("Morning doji star", "long red bar, doji below it, green bar closing above the red bar's midpoint"),
    "evening_doji_star": ("Evening doji star", "long green bar, doji above it, red bar closing below the green bar's midpoint"),
    "bullish_abandoned_baby": ("Bullish abandoned baby", "doji gapped below a long red bar and the green bar after it"),
    "bearish_abandoned_baby": ("Bearish abandoned baby", "doji gapped above a long green bar and the red bar after it"),
    "three_white_soldiers": ("Three white soldiers", "three long green bars, each opening in the last body and closing higher"),
    "three_black_crows": ("Three black crows", "three long red bars, each opening in the last body and closing lower"),
    "identical_three_crows": ("Identical three crows", "three long red bars, each opening at the last close"),
    "three_inside_up": ("Three inside up", "bullish harami, then a green close above the first bar's open"),
    "three_inside_down": ("Three inside down", "bearish harami, then a red close below the first bar's open"),
    "three_outside_up": ("Three outside up", "bullish engulfing, then a higher green close"),
    "three_outside_down": ("Three outside down", "bearish engulfing, then a lower red close"),
    "upside_tasuki_gap": ("Upside tasuki gap", "two green bars with a gap up, then a red bar that didn't fill the gap"),
    "downside_tasuki_gap": ("Downside tasuki gap", "two red bars with a gap down, then a green bar that didn't fill the gap"),
    "bullish_tri_star": ("Bullish tri-star", "three doji, the middle one gapped below, after a decline"),
    "bearish_tri_star": ("Bearish tri-star", "three doji, the middle one gapped above, after a rise"),
    "stick_sandwich": ("Stick sandwich", "red, green, then red closing at the first red close, after a decline"),
    "unique_three_river": ("Unique three river bottom", "long red, red inside it with a new low, then a small green bar"),
    "concealing_baby_swallow": ("Concealing baby swallow", ("two red marubozu, a red bar gapping down whose wick reached back "
                                 "into the last body, then a red bar engulfing it, after a decline")),
    "upside_gap_three_methods": ("Upside gap three methods", ("two long green bars with a gap up, then a red bar that "
                                  "filled the gap: the rise continues")),
    "downside_gap_three_methods": ("Downside gap three methods", ("two long red bars with a gap down, then a green bar "
                                    "that filled the gap: the decline continues")),
    "high_price_gapping_play": ("High price gapping play", ("small bars holding near the top of a long green bar, then a gap "
                                 "up above them")),
    "low_price_gapping_play": ("Low price gapping play", ("small bars holding near the bottom of a long red bar, then a gap "
                                "down below them")),
    "bullish_side_by_side_white_lines": ("Side-by-side white lines (bullish)", ("in a rise, two similar green bars side by "
                                          "side, both gapped above the bar before")),
    "bearish_side_by_side_white_lines": ("Side-by-side white lines (bearish)", ("in a decline, two similar green bars side "
                                          "by side, both gapped below the bar before")),
    "three_stars_in_the_south": ("Three stars in the south", ("three shrinking red bars with higher lows, after a decline: "
                                  "the selling is drying up")),
    "williams_fractal_high": ("Williams fractal (high)", "a high with two lower highs on each side"),
    "williams_fractal_low": ("Williams fractal (low)", "a low with two higher lows on each side"),
    "zigzag_step_up": ("ZigZag step up", "a higher swing low, then a close above the last swing high: the staircase climbs"),
    "zigzag_step_down": ("ZigZag step down", "a lower swing high, then a close below the last swing low: the staircase falls"),
    "two_crows": ("Two crows", "long green bar, red bar gapped above it, red bar closing into the green body"),
    "upside_gap_two_crows": ("Upside gap two crows", "long green bar, two red bars gapped above it, the second engulfing the first"),
    "advance_block": ("Advance block", "three green bars with shrinking bodies and growing upper wicks: the rise is tiring"),
    "deliberation": ("Deliberation", "two long green bars, then a small one: the rise is stalling"),
    "bullish_three_line_strike": ("Bullish three-line strike", "three lower red closes, then a green bar erasing all three"),
    "bearish_three_line_strike": ("Bearish three-line strike", "three higher green closes, then a red bar erasing all three"),
    "rising_three_methods": ("Rising three methods", "long green bar, three small bars inside it, then a higher green close"),
    "falling_three_methods": ("Falling three methods", "long red bar, three small bars inside it, then a lower red close"),
    "bullish_mat_hold": ("Bullish mat hold", "long green bar, a gap up and three small bars holding it, then a higher close"),
    "bearish_mat_hold": ("Bearish mat hold", "long red bar, a gap down and three small bars holding it, then a lower close"),
    "bullish_breakaway": ("Bullish breakaway", "a gap down and three more lower bars, then a green bar closing into the gap"),
    "bearish_breakaway": ("Bearish breakaway", "a gap up and three more higher bars, then a red bar closing into the gap"),
    "ladder_bottom": ("Ladder bottom", "three long red bars, a red bar with an upper wick, then a green bar opening higher"),
    "doji": ("Doji", "opened and closed at the same price: indecision"),
    "long_legged_doji": ("Long-legged doji", "doji with long wicks both ways: indecision"),
    "spinning_top": ("Spinning top", "small body with wicks both ways: indecision"),
}
LABELS |= {k: v[0] for k, v in CANDLES.items()}

CHART = {"double_top", "double_bottom", "triple_top", "triple_bottom", "head_and_shoulders",
         "inverse_head_and_shoulders", "ascending_triangle", "descending_triangle", "triangle", "rising_wedge",
         "falling_wedge", "channel_up", "channel_down", "rectangle", "bull_flag", "bear_flag", "bull_pennant",
         "bear_pennant"}  # breakouts: weak on light volume

# Tops and bottoms before the break: the last shoulder is in place, the neckline hasn't gone yet.
FORMING = {k + "_forming" for k in ("double_top", "double_bottom", "triple_top", "triple_bottom",
                                    "head_and_shoulders", "inverse_head_and_shoulders")}
LABELS |= {k: LABELS[k.removesuffix("_forming")] + " forming" for k in sorted(FORMING)}

_CHART_MIRROR = {
    "double_top": "double_bottom", "triple_top": "triple_bottom", "head_and_shoulders": "inverse_head_and_shoulders",
    "bear_flag": "bull_flag", "bear_pennant": "bull_pennant", "rectangle": "rectangle", "triangle": "triangle",
    "ascending_triangle": "descending_triangle", "descending_triangle": "ascending_triangle",
    "rising_wedge": "falling_wedge", "falling_wedge": "rising_wedge", "channel_up": "channel_down",
    "channel_down": "channel_up",
}
_CANDLE_MIRROR = {  # a bullish pattern on the upside-down chart is this bearish one (None: no common name)
    "hammer": "shooting_star", "inverted_hammer": "hanging_man", "dragonfly_doji": "gravestone_doji",
    "bullish_belt_hold": "bearish_belt_hold", "bullish_marubozu": "bearish_marubozu",
    "bullish_engulfing": "bearish_engulfing", "bullish_harami": "bearish_harami",
    "bullish_harami_cross": "bearish_harami_cross", "homing_pigeon": None, "piercing_line": "dark_cloud_cover",
    "tweezer_bottom": "tweezer_top", "matching_low": None, "bullish_meeting_lines": "bearish_meeting_lines",
    "bullish_kicker": "bearish_kicker", "bullish_separating_lines": "bearish_separating_lines",
    "bullish_abandoned_baby": "bearish_abandoned_baby", "morning_doji_star": "evening_doji_star",
    "morning_star": "evening_star", "three_white_soldiers": "three_black_crows",
    "three_inside_up": "three_inside_down", "three_outside_up": "three_outside_down",
    "upside_tasuki_gap": "downside_tasuki_gap", "bullish_tri_star": "bearish_tri_star", "stick_sandwich": None,
    "unique_three_river": None, "bullish_three_line_strike": "bearish_three_line_strike",
    "rising_three_methods": "falling_three_methods", "bullish_mat_hold": "bearish_mat_hold",
    "bullish_breakaway": "bearish_breakaway", "ladder_bottom": None, "concealing_baby_swallow": None,
    "upside_gap_three_methods": "downside_gap_three_methods", "high_price_gapping_play": "low_price_gapping_play",
    "three_stars_in_the_south": None,
}


GROUPS = {"classic": "chart_patterns", "candlestick": "candle_patterns", "fibonacci": "fib_patterns"}


def catalog() -> list[tuple[str, str, str]]:
    """(group, kind, label) for every pattern, grouped like thinkorswim's pattern tabs."""
    def group(kind: str) -> str:
        return "classic" if kind in CHART | FORMING else "candlestick" if kind in CANDLES else "fibonacci"
    order = list(GROUPS)
    return sorted(((group(k), k, v) for k, v in LABELS.items()), key=lambda r: (order.index(r[0]), r[2]))


def find(bars: list[Bar], atr: float, cfg: ScanConfig) -> list[Hit]:
    """Every pattern completed by the last bar, from the groups in scan.pattern_groups, without
    scan.disabled_patterns; scan.log_only_patterns come back weak (printed and logged, not alerted)."""
    if len(bars) < 12 or atr <= 0:
        return []
    out: list[Hit] = []
    for group in cfg.pattern_groups:
        out += globals()[GROUPS[group]](bars, atr, cfg)
    off, quiet = set(cfg.disabled_patterns), set(cfg.log_only_patterns)
    return [(k, side, d, weak or k in quiet, lv) for k, side, d, weak, lv in out if k not in off]


# --- swings and lines -------------------------------------------------------------------------

@dataclass(frozen=True)
class Pivot:
    i: int
    price: float
    high: bool
    confirmed: int  # the bar whose move away from it made it a swing


def zigzag(bars: list[Bar], threshold: float) -> list[Pivot]:
    """Alternating swing highs and lows: a high counts once price falls `threshold` below it, a low
    once price rises `threshold` above it. The extreme still forming is left out."""
    out: list[Pivot] = []
    if threshold <= 0 or not bars:
        return out
    hi = lo = 0
    up: bool | None = None
    for i, b in enumerate(bars):
        if up is None:
            hi = i if b.h > bars[hi].h else hi
            lo = i if b.l < bars[lo].l else lo
            if hi < i and bars[hi].h - b.l >= threshold:
                out.append(Pivot(hi, bars[hi].h, True, i))
                up, lo = False, i
            elif lo < i and b.h - bars[lo].l >= threshold:
                out.append(Pivot(lo, bars[lo].l, False, i))
                up, hi = True, i
        elif up:
            if b.h >= bars[hi].h:
                hi = i
            elif bars[hi].h - b.l >= threshold:
                out.append(Pivot(hi, bars[hi].h, True, i))
                up, lo = False, i
        elif b.l <= bars[lo].l:
            lo = i
        elif b.h - bars[lo].l >= threshold:
            out.append(Pivot(lo, bars[lo].l, False, i))
            up, hi = True, i
    return out


def mirror(bars: list[Bar]) -> list[Bar]:
    """The chart upside down: highs become lows, so a top becomes a bottom."""
    return [Bar(b.t, -b.o, -b.l, -b.h, -b.c, b.v) for b in bars]


def reached(name: str, level: float, extreme: float, up: bool) -> str:
    """"the pivot S1 29.20 (low 29.17)", or "within 0.03 of the pivot S1 29.20 (low 29.23)" when the candle only came close."""
    near = "" if (extreme >= level if up else extreme <= level) else f"within {abs(extreme - level):.2f} of "
    return f"{near}{name} {level:.2f} ({'high' if up else 'low'} {extreme:.2f})"


def tested(name: str, level: float, extreme: float, up: bool) -> str:
    text = reached(name, level, extreme, up)
    return ("came " if text.startswith("within ") else "tested ") + text


def _fit(points: list[Pivot]) -> tuple[float, float]:
    """Least-squares line through the points: (slope per bar, intercept)."""
    n = len(points)
    mx, my = sum(p.i for p in points) / n, sum(p.price for p in points) / n
    sxx = sum((p.i - mx) ** 2 for p in points)
    slope = sum((p.i - mx) * (p.price - my) for p in points) / sxx if sxx else 0.0
    return slope, my - slope * mx


def _y(line: tuple[float, float], i: int) -> float:
    return line[0] * i + line[1]


# --- classic ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Break:
    """A classic pattern the last bar closed through."""
    kind: str
    side: str  # buy: broke up, sell: broke down
    line: float  # the line it broke, on the last bar
    points: tuple[float, ...]  # the swing prices, oldest first
    start: int  # bar index of the first swing point


def chart_breaks(bars: list[Bar], atr: float, cfg: ScanConfig) -> list[Break]:
    if len(bars) < 20 or atr <= 0:
        return []
    th = cfg.pattern_swing_atr * atr
    start = max(0, len(bars) - cfg.pattern_lookback)
    out: list[Break] = []
    for flip in (False, True):
        b = mirror(bars) if flip else bars
        piv = [p for p in zigzag(b, th) if p.i >= start]
        fine = [p for p in zigzag(b, th / 2) if p.i >= start]
        hit = _breakdown(b, piv, fine, atr, cfg)
        if hit is None:
            continue
        kind, line, pts = hit
        real = (lambda v: -v) if flip else (lambda v: v)
        out.append(Break(_CHART_MIRROR[kind] if flip else kind, "buy" if flip else "sell", real(line),
                         tuple(real(p.price) for p in pts), pts[0].i))
    return out


def chart_patterns(bars: list[Bar], atr: float, cfg: ScanConfig) -> list[Hit]:
    out: list[Hit] = []
    for k in chart_breaks(bars, atr, cfg):
        up = k.side == "buy"
        what = "neckline" if k.kind in _NECKLINE else "resistance line" if up else "support line"
        first = bars[k.start].t
        since = f"{first:%H:%M}" if first.date() == bars[-1].t.date() else f"{first:%m/%d %H:%M}"
        swings = ", ".join(f"{p:.2f}" for p in k.points)
        detail = (f"closed {'above' if up else 'below'} its {what} {k.line:.2f} (swing points {swings} "
                  f"since {since})")
        out.append((k.kind, k.side, detail, False, None))
    return out + forming_patterns(bars, atr, cfg)


_NECKLINE = {"double_top", "double_bottom", "triple_top", "triple_bottom", "head_and_shoulders",
             "inverse_head_and_shoulders"}


def _breakdown(b: list[Bar], piv: list[Pivot], fine: list[Pivot], atr: float, cfg: ScanConfig,
               ) -> tuple[str, float, list[Pivot]] | None:
    """The pattern whose support the last bar just closed through: (kind, the line on the last bar,
    the swing points that make it). Checked most specific first."""
    n, prev, last = len(b), b[-2], b[-1]
    tol, buf = cfg.pattern_tolerance_atr * atr, cfg.min_cross_atr * atr

    def broke(line_prev: float, line_now: float) -> bool:
        return prev.c >= line_prev and last.c < line_now - buf

    shapes = [(pts, _formation(b, pts, tol)) for pts in (piv[-5:], piv[-4:]) if len(pts) >= 4]
    for pts, shape in shapes:  # flat support and resistance, 2+ touches each: a rectangle before a double top
        if shape and shape[0] == "rectangle" and broke(_y(shape[2], n - 2), _y(shape[2], n - 1)):
            return "rectangle", _y(shape[2], n - 1), pts

    for kind, neck, pts in _reversals(piv, tol):
        if broke(_y(neck, n - 2), _y(neck, n - 1)):
            return kind, _y(neck, n - 1), pts

    hit = _bear_flag(b, fine, atr, cfg, broke)
    if hit:
        return hit

    for pts, shape in shapes:
        if shape and broke(_y(shape[2], n - 2), _y(shape[2], n - 1)):
            return shape[0], _y(shape[2], n - 1), pts
    return None


def _reversals(piv: list[Pivot], tol: float) -> list[tuple[str, tuple[float, float], list[Pivot]]]:
    """Head and shoulders, triple and double tops ending at the latest swing high, most specific first:
    (kind, the neckline, the swing points). Whether price has broken the neckline isn't checked here."""
    highs = [p for p in piv if p.high]
    if not highs:
        return []
    out = []
    seq = piv[:piv.index(highs[-1]) + 1]  # up to the latest swing high
    if len(seq) >= 5:
        ls, l1, hd, l2, rs = seq[-5:]
        height = hd.price - (l1.price + l2.price) / 2
        if (hd.price > max(ls.price, rs.price) + tol and min(ls.price, rs.price) > max(l1.price, l2.price)
                and abs(ls.price - rs.price) <= max(2 * tol, 0.35 * height)):
            out.append(("head_and_shoulders", _fit([l1, l2]), seq[-5:]))
        tops = [seq[-5].price, seq[-3].price, seq[-1].price]
        if max(tops) - min(tops) <= 1.5 * tol:
            out.append(("triple_top", (0.0, min(seq[-4].price, seq[-2].price)), seq[-5:]))
    if len(seq) >= 3 and abs(seq[-3].price - seq[-1].price) <= tol:
        out.append(("double_top", (0.0, seq[-2].price), seq[-3:]))
    return out


@dataclass(frozen=True)
class Forming:
    """A top or bottom whose last shoulder is in place but whose neckline hasn't broken yet."""
    kind: str  # e.g. head_and_shoulders (a top) or inverse_head_and_shoulders (a bottom)
    side: str  # what a break of the neckline would be: sell for a top, buy for a bottom
    neckline: float  # on the last bar
    points: tuple[float, ...]  # the swing prices, oldest first
    start: int  # bar index of the first swing point
    confirmed: int  # bar index on which the last shoulder was confirmed


def forming(bars: list[Bar], atr: float, cfg: ScanConfig) -> list[Forming]:
    """Every head and shoulders, triple and double top or bottom still forming on the last bar: the last
    shoulder is a confirmed swing, price hasn't closed through the neckline since, and it hasn't gone
    back past that shoulder (which would undo the pattern). The most specific one per direction."""
    if len(bars) < 20 or atr <= 0:
        return []
    th, tol, buf = cfg.pattern_swing_atr * atr, cfg.pattern_tolerance_atr * atr, cfg.min_cross_atr * atr
    start, n = max(0, len(bars) - cfg.pattern_lookback), len(bars)
    out = []
    for flip in (False, True):
        b = mirror(bars) if flip else bars
        piv = [p for p in zigzag(b, th) if p.i >= start]
        for kind, neck, pts in _reversals(piv, tol):
            rs = pts[-1]
            after = b[rs.i + 1:]
            if (not after or max(x.h for x in after) > rs.price
                    or any(x.c < _y(neck, j) - buf for j, x in enumerate(after, rs.i + 1))):
                continue
            real = (lambda v: -v) if flip else (lambda v: v)
            out.append(Forming(_CHART_MIRROR[kind] if flip else kind, "buy" if flip else "sell",
                               real(_y(neck, n - 1)), tuple(real(p.price) for p in pts), pts[0].i, rs.confirmed))
            break  # the most specific shape only
    return out


def forming_patterns(bars: list[Bar], atr: float, cfg: ScanConfig) -> list[Hit]:
    """A warning before the break: a top or bottom still forming, and the last bar is the first to close
    within scan.forming_near_atr ATRs of its neckline (the bar before was farther away)."""
    out: list[Hit] = []
    near = cfg.forming_near_atr * atr
    for f in forming(bars, atr, cfg):
        if not abs(bars[-1].c - f.neckline) <= near < abs(bars[-2].c - f.neckline):
            continue
        first = bars[f.start].t
        since = f"{first:%H:%M}" if first.date() == bars[-1].t.date() else f"{first:%m/%d %H:%M}"
        gap = abs(bars[-1].c - f.neckline) / atr
        detail = (f"last {'shoulder' if 'shoulders' in f.kind else 'peak' if f.side == 'sell' else 'trough'} is in "
                  f"place (swing points {', '.join(f'{p:.2f}' for p in f.points)} since {since}); a close "
                  f"{'below' if f.side == 'sell' else 'above'} the neckline {f.neckline:.2f}, {gap:.1f} ATR away, "
                  f"would confirm it")
        out.append((f.kind + "_forming", f.side, detail, False, f"{first:%m/%d %H:%M}"))
    return out


def _formation(b: list[Bar], pts: list[Pivot], tol: float):
    """Triangles, wedges, channels and rectangles from trendlines through the swing highs and lows:
    (kind, upper line, lower line), or None."""
    his, los = [p for p in pts if p.high], [p for p in pts if not p.high]
    if len(his) < 2 or len(los) < 2:
        return None
    up, dn = _fit(his), _fit(los)
    i0, i1 = pts[0].i, len(b) - 1
    span = i1 - i0
    if span < 10:
        return None
    w0, w1 = _y(up, i0) - _y(dn, i0), _y(up, i1) - _y(dn, i1)
    if w0 <= 0 or w1 <= 0:
        return None
    if any(x.c > _y(up, j) + tol or x.c < _y(dn, j) - tol for j, x in enumerate(b[i0:i1], i0)):
        return None  # closes outside the lines before now: not one formation

    def slope(line):
        d = line[0] * span
        return 1 if d > tol else -1 if d < -tol else 0

    su, sd = slope(up), slope(dn)
    converging, parallel = w1 < 0.75 * w0, 0.75 * w0 <= w1 <= 1.33 * w0
    kind = None
    if su == 0 and sd == 0:
        kind = "rectangle"
    elif converging:
        kind = {(0, 1): "ascending_triangle", (-1, 0): "descending_triangle", (-1, 1): "triangle",
                (1, 1): "rising_wedge", (-1, -1): "falling_wedge"}.get((su, sd))
    elif parallel:
        kind = {(1, 1): "channel_up", (-1, -1): "channel_down"}.get((su, sd))
    return (kind, up, dn) if kind else None


def _bear_flag(b: list[Bar], piv: list[Pivot], atr: float, cfg: ScanConfig, broke):
    """A sharp drop (the pole), a pause that drifts up or tightens, then a close below the pause."""
    n = len(b)
    tol = cfg.pattern_tolerance_atr * atr
    ends = [k for k, p in enumerate(piv) if not p.high and k > 0]
    for k in reversed(ends[-3:]):
        p0, p1, rest = piv[k - 1], piv[k], piv[k:]
        pole = p0.price - p1.price
        if pole < cfg.flag_pole_atr * atr or p1.i - p0.i > 12 or not 3 <= n - 1 - p1.i <= 30 or len(rest) > 6:
            continue
        his, los = [p for p in rest if p.high], [p for p in rest if not p.high]
        if len(los) < 2 or not his or max(p.price for p in his) - p1.price > 0.5 * pole:
            continue
        dn = _fit(los)
        up = _fit(his) if len(his) >= 2 else (dn[0], his[0].price - dn[0] * his[0].i)
        w0, w1 = _y(up, p1.i) - _y(dn, p1.i), _y(up, n - 1) - _y(dn, n - 1)
        if w0 <= 0 or w1 <= 0 or dn[0] * (n - 1 - p1.i) < -tol:  # a bear flag doesn't keep falling
            continue
        if any(x.c > _y(up, j) + tol or x.c < _y(dn, j) - tol for j, x in enumerate(b[p1.i:n - 1], p1.i)):
            continue
        if broke(_y(dn, n - 2), _y(dn, n - 1)):
            return ("bear_pennant" if w1 < 0.6 * w0 else "bear_flag"), _y(dn, n - 1), [p0, *rest]
    return None


# --- candlesticks ---------------------------------------------------------------------------------

def _body(x: Bar) -> float:
    return abs(x.c - x.o)


def _rng(x: Bar) -> float:
    return x.h - x.l


def _top(x: Bar) -> float:
    return max(x.o, x.c)


def _bot(x: Bar) -> float:
    return min(x.o, x.c)


def _uw(x: Bar) -> float:
    return x.h - _top(x)


def _lw(x: Bar) -> float:
    return _bot(x) - x.l


def _trend(b: list[Bar], k: int, move: float) -> int:
    """The move into a k-bar pattern: +1 if the 5 bars before it rose `move` or more, -1 if they fell
    (move is scan.candle_trend_atr ATRs)."""
    if len(b) < k + 6:
        return 0
    d = b[-k - 1].c - b[-k - 6].c
    return 1 if d >= move else -1 if d <= -move else 0


class _Shape:
    """Size tests relative to the recent bars."""

    def __init__(self, b: list[Bar], atr: float):
        bodies = [_body(x) for x in b[-15:-1]]
        self.avg = sum(bodies) / len(bodies) if bodies else 0.0
        self.atr = atr

    def long(self, x: Bar) -> bool:
        return _body(x) >= 1.2 * self.avg and _body(x) >= 0.5 * _rng(x) and _body(x) > 0

    def small(self, x: Bar) -> bool:
        return _body(x) <= 0.6 * self.avg

    @staticmethod
    def doji(x: Bar) -> bool:
        return _rng(x) > 0 and _body(x) <= 0.1 * _rng(x)

    def sized(self, x: Bar) -> bool:
        return _rng(x) >= 0.6 * self.atr

    def near(self, p: float, q: float, k: float = 0.1) -> bool:
        return abs(p - q) <= k * self.atr


def _bull(x: Bar) -> bool:
    return x.c > x.o


def _bear(x: Bar) -> bool:
    return x.c < x.o


def _bullish_candles(b: list[Bar], atr: float, move: float) -> list[str]:
    """Bullish patterns on the last bars (the bearish twins come from the mirrored chart)."""
    s, hits = _Shape(b, atr), []
    x1, x2, x3, x4, x5 = b[-1], b[-2], b[-3], b[-4], b[-5]
    t1, t2, t3, t4, t5 = (_trend(b, k, move) for k in (1, 2, 3, 4, 5))

    # one bar
    if t1 < 0 and s.sized(x1):
        if s.doji(x1) and _lw(x1) >= 0.6 * _rng(x1) and _uw(x1) <= 0.1 * _rng(x1):
            hits.append("dragonfly_doji")
        elif not s.doji(x1) and _lw(x1) >= 2 * _body(x1) and _uw(x1) <= max(0.5 * _body(x1), 0.1 * _rng(x1)):
            hits.append("hammer")
        elif not s.doji(x1) and _uw(x1) >= 2 * _body(x1) and _lw(x1) <= max(0.5 * _body(x1), 0.1 * _rng(x1)):
            hits.append("inverted_hammer")
        if _bull(x1) and s.long(x1) and _lw(x1) <= 0.05 * _rng(x1) < _uw(x1):
            hits.append("bullish_belt_hold")
    if _bull(x1) and _body(x1) >= 1.5 * s.avg and _uw(x1) + _lw(x1) <= 0.05 * _rng(x1):
        hits.append("bullish_marubozu")

    # two bars
    if t2 < 0 and _bear(x2):
        if _bull(x1) and x1.o <= x2.c and x1.c >= x2.o and _body(x1) > _body(x2):
            hits.append("bullish_engulfing")
        elif s.long(x2) and _top(x1) < x2.o and _bot(x1) > x2.c and _body(x1) <= 0.6 * _body(x2):
            hits.append("bullish_harami_cross" if s.doji(x1) else "bullish_harami" if _bull(x1) else "homing_pigeon")
        elif s.long(x2) and _bull(x1) and x1.o < x2.c and x2.c + _body(x2) / 2 < x1.c < x2.o:
            hits.append("piercing_line")
        if _bull(x1) and s.sized(x1) and s.sized(x2) and s.near(x1.l, x2.l, 0.05):
            hits.append("tweezer_bottom")
        if s.long(x2) and _bear(x1) and s.near(x1.c, x2.c, 0.05):
            hits.append("matching_low")
        if s.long(x2) and _bull(x1) and s.long(x1) and x1.o < x2.c - _body(x2) / 2 and s.near(x1.c, x2.c):
            hits.append("bullish_meeting_lines")
    if _bear(x2) and s.long(x2) and _bull(x1) and s.long(x1) and x1.o > x2.o:
        hits.append("bullish_kicker")
    if t2 > 0 and _bear(x2) and _bull(x1) and s.long(x1) and s.near(x1.o, x2.o):
        hits.append("bullish_separating_lines")

    # three bars
    if t3 < 0 and _bear(x3) and s.long(x3) and _bull(x1) and x1.c > (x3.o + x3.c) / 2:
        if s.doji(x2) and x2.h < x3.l and x2.h < x1.l:
            hits.append("bullish_abandoned_baby")
        elif (s.small(x2) or s.doji(x2)) and _top(x2) <= x3.c + 0.05 * atr:
            hits.append("morning_doji_star" if s.doji(x2) else "morning_star")
    if (t3 <= 0 and all(_bull(x) and _body(x) >= s.avg and _uw(x) <= 0.3 * _body(x) for x in (x3, x2, x1))
            and x3.c < x2.c < x1.c and x3.o <= x2.o <= x3.c and x2.o <= x1.o <= x2.c):
        hits.append("three_white_soldiers")
    if t3 < 0 and _bear(x3) and s.long(x3) and _bull(x2) and _top(x2) < x3.o and _bot(x2) > x3.c and _bull(x1) and x1.c > x3.o:
        hits.append("three_inside_up")
    if (t3 < 0 and _bear(x3) and _bull(x2) and x2.o <= x3.c and x2.c >= x3.o and _body(x2) > _body(x3)
            and _bull(x1) and x1.c > x2.c):
        hits.append("three_outside_up")
    if (t3 > 0 and _bull(x3) and _bull(x2) and _bot(x2) > _top(x3) and _bear(x1) and _bot(x2) < x1.o < _top(x2)
            and _top(x3) < x1.c < _bot(x2)):
        hits.append("upside_tasuki_gap")
    if t3 < 0 and s.doji(x3) and s.doji(x2) and s.doji(x1) and _top(x2) < _bot(x3) and _top(x2) < _bot(x1):
        hits.append("bullish_tri_star")
    if (t3 < 0 and _bear(x3) and _bull(x2) and x2.o > x3.c and _bear(x1) and x1.o > x2.c
            and s.near(x1.c, x3.c, 0.05)):
        hits.append("stick_sandwich")
    if (t3 < 0 and _bear(x3) and s.long(x3) and _bear(x2) and x2.o < x3.o and x2.c > x3.c and x2.l < x3.l
            and _bull(x1) and s.small(x1) and x1.c < x2.c):
        hits.append("unique_three_river")
    if (t3 > 0 and _bull(x3) and s.long(x3) and _bull(x2) and s.long(x2) and _bot(x2) > _top(x3) and _bear(x1)
            and _bot(x2) < x1.o < _top(x2) and _bot(x3) < x1.c < _top(x3)):
        hits.append("upside_gap_three_methods")
    if (t3 < 0 and _bear(x3) and s.long(x3) and _lw(x3) >= 0.5 * _body(x3) and _bear(x2) and _body(x2) < _body(x3)
            and x2.l > x3.l and x3.l < x2.o < x3.h and _bear(x1) and s.small(x1) and _body(x1) > 0
            and _uw(x1) + _lw(x1) <= 0.1 * _rng(x1) and x1.h <= x2.h and x1.l >= x2.l):
        hits.append("three_stars_in_the_south")

    # four and five bars
    if _bear(x4) and _bear(x3) and _bear(x2) and x4.c > x3.c > x2.c and _bull(x1) and x1.o <= x2.c and x1.c >= x4.o:
        hits.append("bullish_three_line_strike")
    pause = (x4, x3, x2)
    if t5 > 0 and _bull(x5) and s.long(x5) and _bull(x1) and s.long(x1):
        if (all(s.small(x) and x.h <= x5.h and x.l >= x5.l for x in pause) and sum(_bear(x) for x in pause) >= 2
                and x1.c > x5.c):
            hits.append("rising_three_methods")
        elif (_bot(x4) > x5.c and all(s.small(x) for x in pause) and min(x.l for x in pause) > x5.l
              and x1.c > max(x.h for x in pause)):
            hits.append("bullish_mat_hold")
    if (t5 < 0 and _bear(x5) and s.long(x5) and _bear(x4) and _top(x4) < x5.c and x3.c < x4.c and x2.c < x3.c
            and _bull(x1) and s.long(x1) and _top(x4) < x1.c < x5.c):
        hits.append("bullish_breakaway")
    if (t5 < 0 and all(_bear(x) and s.long(x) for x in (x5, x4, x3)) and x5.o > x4.o > x3.o and x5.c > x4.c > x3.c
            and _bear(x2) and _uw(x2) >= _body(x2) and _bull(x1) and x1.o > x2.o):
        hits.append("ladder_bottom")
    if (t4 < 0 and all(_bear(x) and _body(x) >= s.avg and _uw(x) + _lw(x) <= 0.1 * _rng(x) for x in (x4, x3))
            and _bear(x2) and x2.o < x3.c < x2.h and _bear(x1) and x1.o >= x2.h and x1.c <= x2.l):
        hits.append("concealing_baby_swallow")
    for k in range(8, 2, -1):  # a long green bar, a pause of k small bars near its top, then a gap above the pause
        if len(b) < k + 8:
            continue
        pause, before = b[-k - 1:-1], b[-k - 2]
        top, bottom = max(x.h for x in pause), min(x.l for x in pause)
        if (_bull(before) and s.long(before) and all(s.small(x) for x in pause) and top - bottom <= atr
                and bottom >= (before.o + before.c) / 2 and x1.o > top and x1.c > top):
            hits.append("high_price_gapping_play")
            break
    return hits


def _bearish_only_candles(b: list[Bar], atr: float, move: float) -> list[str]:
    """Bearish patterns whose bullish twin has no common name."""
    s, hits = _Shape(b, atr), []
    x1, x2, x3 = b[-1], b[-2], b[-3]
    t2, t3 = _trend(b, 2, move), _trend(b, 3, move)
    if t3 > 0 and _bull(x3) and s.long(x3) and _bear(x2) and _bot(x2) > x3.c and _bear(x1):
        if x2.c < x1.o < x2.o and x3.o < x1.c < x3.c:
            hits.append("two_crows")
        elif s.small(x2) and x1.o > x2.o and x1.c < x2.c and x1.c > x3.c:
            hits.append("upside_gap_two_crows")
    if (t3 >= 0 and _bull(x3) and _bull(x2) and _bull(x1) and x3.c < x2.c < x1.c
            and _body(x3) > _body(x2) > _body(x1) and _uw(x1) > _uw(x2) > _uw(x3)):
        hits.append("advance_block")
    elif (t3 > 0 and _bull(x3) and s.long(x3) and _bull(x2) and s.long(x2) and _bull(x1) and s.small(x1)
          and x1.o >= x2.c - 0.1 * atr):
        hits.append("deliberation")
    if t2 < 0 and _bear(x2) and s.long(x2) and _bull(x1) and x1.o < x2.l:
        if s.near(x1.c, x2.l, 0.05):
            hits.append("on_neck")
        elif x2.c <= x1.c <= x2.c + 0.1 * _body(x2):
            hits.append("in_neck")
        elif x2.c + 0.1 * _body(x2) < x1.c < (x2.o + x2.c) / 2:
            hits.append("thrusting")
    if (all(_bear(x) and s.long(x) for x in (x3, x2, x1)) and x3.c > x2.c > x1.c
            and s.near(x2.o, x3.c, 0.05) and s.near(x1.o, x2.c, 0.05)):
        hits.append("identical_three_crows")
    return hits


def _neutral_candles(b: list[Bar], atr: float) -> list[str]:
    s, x1 = _Shape(b, atr), b[-1]
    if not s.sized(x1):
        return []
    if s.doji(x1):
        both = _uw(x1) >= 0.3 * _rng(x1) and _lw(x1) >= 0.3 * _rng(x1)
        return ["long_legged_doji" if both and _rng(x1) >= atr else "doji"]
    if 0.1 * _rng(x1) < _body(x1) <= 0.3 * _rng(x1) and _uw(x1) >= _body(x1) and _lw(x1) >= _body(x1):
        return ["spinning_top"]
    return []


def _side_by_side(b: list[Bar], atr: float, move: float) -> list[tuple[str, str]]:
    """Side-by-side white lines: two similar green bars with the same open, gapped away from the bar before."""
    s, (x3, x2, x1) = _Shape(b, atr), b[-3:]
    t3 = _trend(b, 3, move)
    twins = (_bull(x2) and _bull(x1) and s.near(x1.o, x2.o) and _body(x2) > 0
             and abs(_body(x1) - _body(x2)) <= 0.3 * _body(x2))
    if twins and t3 > 0 and _bot(x2) > _top(x3) and _bot(x1) > _top(x3):
        return [("bullish_side_by_side_white_lines", "buy")]
    if twins and t3 < 0 and _bear(x3) and _top(x2) < _bot(x3) and _top(x1) < _bot(x3):
        return [("bearish_side_by_side_white_lines", "sell")]
    return []


def _fractals(b: list[Bar]) -> list[Hit]:
    """Bill Williams fractals, confirmed two bars after the middle one."""
    if len(b) < 5:
        return []
    m, sides = b[-3], b[-5:-3] + b[-2:]
    out: list[Hit] = []
    if m.h > max(x.h for x in sides):
        detail = f"swing high {m.h:.2f} at {m.t:%H:%M}, with two lower highs on each side"
        out.append(("williams_fractal_high", "sell", detail, False, None))
    if m.l < min(x.l for x in sides):
        detail = f"swing low {m.l:.2f} at {m.t:%H:%M}, with two higher lows on each side"
        out.append(("williams_fractal_low", "buy", detail, False, None))
    return out


def _zigzag_steps(bars: list[Bar], atr: float, cfg: ScanConfig) -> list[Hit]:
    """The zigzag's staircase: a higher swing low, then a close above the swing high between (and down)."""
    th, tol, buf = cfg.pattern_swing_atr * atr, cfg.pattern_tolerance_atr * atr, cfg.min_cross_atr * atr
    out: list[Hit] = []
    for flip in (False, True):
        b = mirror(bars) if flip else bars
        piv = zigzag(b, th)
        if len(piv) < 3 or piv[-1].high:
            continue
        l1, h1, l2 = piv[-3:]
        if l2.price > l1.price + tol and b[-2].c <= h1.price < b[-1].c - buf:
            real = (lambda v: -v) if flip else (lambda v: v)
            detail = (f"swing {'high' if flip else 'low'} {real(l2.price):.2f} {'below' if flip else 'above'} "
                      f"{real(l1.price):.2f}, then a close {'below' if flip else 'above'} the swing "
                      f"{'low' if flip else 'high'} {real(h1.price):.2f}")
            out.append(("zigzag_step_down" if flip else "zigzag_step_up", "sell" if flip else "buy", detail, False, None))
    return out


def candle_patterns(bars: list[Bar], atr: float, cfg: ScanConfig) -> list[Hit]:
    if len(bars) < 12 or atr <= 0:
        return []
    move = cfg.candle_trend_atr * atr
    found: list[tuple[str, str]] = [(k, "buy") for k in _bullish_candles(bars, atr, move)]
    found += [(m, "sell") for k in _bullish_candles(mirror(bars), atr, move) if (m := _CANDLE_MIRROR[k])]
    found += [(k, "sell") for k in _bearish_only_candles(bars, atr, move)]
    found += _side_by_side(bars, atr, move)
    if "identical_three_crows" in {k for k, _ in found}:
        found = [f for f in found if f[0] != "three_black_crows"]
    out: list[Hit] = [(k, side, CANDLES[k][1], False, None) for k, side in found]
    trend = _trend(bars, 1, move)
    if trend and not {k for k, _ in found} & {"dragonfly_doji", "gravestone_doji"}:
        for k in _neutral_candles(bars, atr):  # indecision against the move so far
            out.append((k, "sell" if trend > 0 else "buy", f"{CANDLES[k][1]}, after a {'rise' if trend > 0 else 'decline'}",
                        False, None))
    return out + _fractals(bars) + _zigzag_steps(bars, atr, cfg)


# --- Fibonacci ----------------------------------------------------------------------------------

_HARMONICS = (  # name, AB/XA, BC/AB, CD/BC, AD/XA
    ("gartley", (0.58, 0.66), (0.382, 0.886), (1.13, 1.618), (0.75, 0.82)),
    ("bat", (0.382, 0.5), (0.382, 0.886), (1.618, 2.618), (0.85, 0.92)),
    ("butterfly", (0.75, 0.82), (0.382, 0.886), (1.618, 2.618), (1.24, 1.66)),
    ("crab", (0.382, 0.618), (0.382, 0.886), (2.24, 3.618), (1.55, 1.68)),
)
_RETRACEMENTS = (0.786, 0.618, 0.5, 0.382)  # deepest first


def fib_patterns(bars: list[Bar], atr: float, cfg: ScanConfig) -> list[Hit]:
    if len(bars) < 20 or atr <= 0:
        return []
    th, tol, buf = cfg.pattern_swing_atr * atr, cfg.pattern_tolerance_atr * atr, cfg.min_cross_atr * atr
    start = max(0, len(bars) - cfg.pattern_lookback)
    out: list[Hit] = []
    for flip in (False, True):
        b = mirror(bars) if flip else bars
        real = (lambda v: -v) if flip else (lambda v: v)
        side, word = ("sell", "bearish") if flip else ("buy", "bullish")
        piv = [p for p in zigzag(b, th) if p.i >= start]
        n, prev, last = len(b), b[-2], b[-1]
        if piv and not piv[-1].high and piv[-1].confirmed == n - 1:  # D: a swing low confirmed just now
            hit = _harmonic(piv)
            if hit:
                name, pts, ratios = hit
                where = ", ".join(f"{lab} {real(p.price):.2f}" for lab, p in zip("XABCD"[5 - len(pts):], pts, strict=False))
                if name == "three_drives":
                    where = ", ".join(f"{real(p.price):.2f}" for p in pts)
                out.append((f"{word}_{name}", side, f"price turned {'down' if flip else 'up'} at D ({where}; {ratios})",
                            False, None))
        if len(piv) >= 2 and piv[-1].high and not piv[-2].high:  # a pullback from the latest swing high
            lo, hi = piv[-2], piv[-1]
            swing = hi.price - lo.price
            since = b[hi.i + 1:n - 1]
            if swing >= cfg.fib_swing_atr * atr and hi.i < n - 1:
                for r in _RETRACEMENTS:
                    lv = hi.price - r * swing
                    if (lv - 2 * tol <= last.l <= lv + tol and last.c > lv + buf and prev.c > lv
                            and all(x.l > lv for x in since)):  # the first bar to reach it
                        what = f"the {r * 100:.1f}% retracement of the {real(lo.price):.2f}→{real(hi.price):.2f} swing,"
                        detail = (f"{tested(what, real(lv), real(last.l), up=flip)} and closed {'below' if flip else 'above'} it")
                        out.append(("fib_resistance" if flip else "fib_support", side, detail, False,
                                    f"fib {r * 100:.1f}%"))
                        break
    return out


def _harmonic(piv: list[Pivot]) -> tuple[str, list[Pivot], str] | None:
    """Bullish harmonic patterns ending at the latest swing low D."""
    def within(v, rng):
        return rng[0] <= v <= rng[1]

    if len(piv) >= 5:
        x, a, b, c, d = piv[-5:]
        xa, ab, bc, cd, ad = a.price - x.price, a.price - b.price, c.price - b.price, c.price - d.price, a.price - d.price
        if min(xa, ab, bc, cd) > 0:
            r = (ab / xa, bc / ab, cd / bc, ad / xa)
            for name, *rngs in _HARMONICS:
                if all(within(v, g) for v, g in zip(r, rngs, strict=True)):
                    return name, piv[-5:], f"AB {r[0]:.3f} of XA, D at {r[3]:.3f} of XA"
    if len(piv) >= 6:
        p0, d1, c1, d2, c2, d3 = piv[-6:]
        if d1.price > d2.price > d3.price and p0.price > c1.price:
            r1 = (c1.price - d1.price) / (p0.price - d1.price)
            r2 = (c2.price - d2.price) / (c1.price - d2.price) if c1.price > d2.price else 0
            e1 = (c1.price - d2.price) / (c1.price - d1.price)
            e2 = (c2.price - d3.price) / (c2.price - d2.price) if c2.price > d2.price else 0
            if within(r1, (0.5, 0.886)) and within(r2, (0.5, 0.886)) and within(e1, (1.13, 1.786)) and within(e2, (1.13, 1.786)):
                return "three_drives", [d1, d2, d3], f"drives extend {e1:.2f} and {e2:.2f}"
    if len(piv) >= 4:
        a, b, c, d = piv[-4:]
        ab, bc, cd = a.price - b.price, c.price - b.price, c.price - d.price
        if ab > 0 and bc > 0 and d.price < b.price and within(bc / ab, (0.382, 0.886)) and within(cd / ab, (0.9, 1.1)):
            return "abcd", piv[-4:], f"CD {cd / ab:.2f} of AB, C at {bc / ab:.3f} of AB"
    return None
