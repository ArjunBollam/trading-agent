"""Chart, candlestick and Fibonacci patterns (formations.py), on drawn charts."""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

from paperbot.config import ScanConfig
from paperbot.features import Bar, atr
from paperbot.formations import (
    CANDLES,
    LABELS,
    candle_patterns,
    chart_patterns,
    fib_patterns,
    find,
    zigzag,
)
from paperbot.patterns import LABELS as SIGNAL_LABELS

SC = ScanConfig()
T0 = datetime(2026, 10, 26, 9, 30, tzinfo=ZoneInfo("America/New_York"))


def path(*points: float, steps: int = 6, wiggle: float = 0.05) -> list[Bar]:
    """5-minute bars along straight lines through the points, `steps` bars per leg."""
    bars, price = [], points[0]
    for a, b in pairwise(points):
        for k in range(1, steps + 1):
            c = a + (b - a) * k / steps
            bars.append(Bar(T0 + timedelta(minutes=5 * len(bars)), price, max(price, c) + wiggle,
                            min(price, c) - wiggle, c, 1000))
            price = c
    return bars


def hits(bars: list[Bar], fn, start: int = 20) -> list[tuple[str, str, int]]:
    """(kind, side, bar index) for every pattern found as the chart is drawn bar by bar."""
    out = []
    for end in range(start, len(bars) + 1):
        b = bars[:end]
        a = atr(b[-30:]) or 0.0
        out += [(k, side, end - 1) for k, side, *_ in fn(b, a, SC)]
    return out


def kinds(found) -> set[tuple[str, str]]:
    return {(k, s) for k, s, _ in found}


def test_every_pattern_has_a_label_and_signal_label():
    assert set(CANDLES) <= set(LABELS) and set(LABELS) <= set(SIGNAL_LABELS)


def test_zigzag_finds_alternating_swings():
    piv = zigzag(path(100, 104, 101, 105, 102), 1.0)
    assert [(p.high, round(p.price)) for p in piv] == [(False, 100), (True, 104), (False, 101), (True, 105)]


def test_double_top_breaks_its_neckline():
    found = hits(path(100, 104, 101, 104.05, 99.5), chart_patterns)
    assert ("double_top", "sell") in kinds(found)
    assert ("double_bottom", "buy") in kinds(hits(path(104, 100, 103, 99.95, 104.5), chart_patterns))


def test_triple_top_and_bottom():
    assert ("triple_top", "sell") in kinds(hits(path(100, 104, 101, 104.1, 101.2, 103.95, 99.5), chart_patterns))
    assert ("triple_bottom", "buy") in kinds(hits(path(104, 100, 103, 99.9, 102.8, 100.05, 104.5), chart_patterns))


def test_head_and_shoulders_and_inverse():
    found = hits(path(99, 103, 101, 106, 101.1, 103.1, 98.5), chart_patterns)
    assert ("head_and_shoulders", "sell") in kinds(found)
    found = hits(path(105, 101, 103, 98, 102.9, 100.9, 105.5), chart_patterns)
    assert ("inverse_head_and_shoulders", "buy") in kinds(found)


def test_triangles_wedges_channels_and_rectangle():
    cases = {
        ("ascending_triangle", "buy"): path(100, 120, 108, 120.05, 116, 124),
        ("descending_triangle", "sell"): path(120, 100, 112, 99.95, 104, 96),
        ("triangle", "buy"): path(100, 120, 104, 116, 108, 118),
        ("falling_wedge", "buy"): path(120, 100, 114, 98, 108, 96, 108),
        ("rising_wedge", "sell"): path(100, 120, 106, 122, 112, 124, 112),
        ("channel_up", "sell"): path(100, 110, 104, 114, 108, 118, 106),
        ("channel_down", "buy"): path(120, 110, 116, 106, 112, 102, 114),
        ("rectangle", "sell"): path(100, 110, 100.1, 109.9, 100.05, 109.95, 97),
    }
    for want, bars in cases.items():
        assert want in kinds(hits(bars, chart_patterns)), want


def test_bull_flag_and_bear_pennant():
    flag = path(100, 100.4, 100, 100.4, 108, 107, 107.6, 106.6, 112, steps=4)
    assert ("bull_flag", "buy") in kinds(hits(flag, chart_patterns))
    pennant = path(110, 109.6, 110, 109.6, 102, 104, 102.6, 103.6, 102.9, 103.3, 98, steps=4)
    assert ("bear_pennant", "sell") in kinds(hits(pennant, chart_patterns))


def test_no_chart_pattern_on_a_straight_line():
    assert hits(path(100, 120, steps=60), chart_patterns) == []


def test_harmonics_turn_at_d():
    gartley = path(104, 100, 110, 103.82, 107.64, 102.14, 105)
    assert ("bullish_gartley", "buy") in kinds(hits(gartley, fib_patterns))
    bat = path(96, 100, 90, 94.4, 91.2, 98.9, 95)  # bearish: X 100 low? no, mirrored: X 90 ... D 98.9
    found = kinds(hits(bat, fib_patterns))
    assert any(k.startswith("bearish_") and s == "sell" for k, s in found), found
    abcd = path(106, 110, 104, 108, 102, 105)
    assert ("bullish_abcd", "buy") in kinds(hits(abcd, fib_patterns))


def test_fibonacci_retracement_held():
    bars = path(100, 110, 106.3, steps=10)  # 110 → 106.3 tests the 38.2% level (106.18)
    bars.append(Bar(bars[-1].t + timedelta(minutes=5), 106.3, 106.6, 106.1, 106.55, 1000))
    found = fib_patterns(bars, atr(bars[-30:]), SC)
    assert [(k, s, lv) for k, s, _, _, lv in found] == [("fib_support", "buy", "fib 38.2%")]


def candles(*rows: tuple[float, float, float, float], before: list[Bar] | None = None) -> list[Bar]:
    bars = list(before or [])
    for o, h, lo, c in rows:
        bars.append(Bar(T0 + timedelta(minutes=5 * len(bars)), o, h, lo, c, 1000))
    return bars


def found_candles(bars):
    return {(k, s) for k, s, *_ in candle_patterns(bars, atr(bars[-30:]), SC)}


DOWN = path(112, 104, steps=16)  # a decline into the pattern
UP = path(100, 108, steps=16)


def test_reversal_candles_after_a_move():
    star = candles((104, 104.1, 101, 101.1), (100.8, 100.9, 100.4, 100.7), (100.9, 103.2, 100.8, 103.0), before=DOWN)
    assert ("morning_star", "buy") in found_candles(star)
    evening = candles((108, 111, 107.9, 110.9), (111.2, 111.6, 111.1, 111.3), (111.0, 111.1, 108.6, 108.8), before=UP)
    assert ("evening_star", "sell") in found_candles(evening)
    engulf = candles((108, 108.6, 107.9, 108.5), (108.7, 108.8, 106.9, 107.2), before=UP)
    assert ("bearish_engulfing", "sell") in found_candles(engulf)
    hammer = candles((103.6, 104.1, 102.4, 104.0), before=DOWN)
    assert ("dragonfly_doji", "buy") in found_candles(candles((104, 104.1, 102.4, 103.95), before=DOWN))
    assert ("hammer", "buy") in found_candles(hammer)


def test_continuation_and_three_bar_candles():
    soldiers = candles((100, 101.6, 99.9, 101.5), (101.2, 102.9, 101.1, 102.8), (102.5, 104.1, 102.4, 104.0),
                       before=path(101, 100, steps=16))
    assert ("three_white_soldiers", "buy") in found_candles(soldiers)
    rising = candles((108, 111, 107.9, 110.9), (110.7, 110.8, 110.3, 110.4), (110.4, 110.5, 110.0, 110.1),
                     (110.1, 110.3, 109.8, 110.2), (110.2, 112.1, 110.1, 112.0), before=UP)
    assert ("rising_three_methods", "buy") in found_candles(rising)
    strike = candles((104, 104.1, 103, 103.1), (103.1, 103.2, 102, 102.1), (102.1, 102.2, 101, 101.1),
                     (101.0, 104.4, 100.9, 104.3), before=path(106, 104, steps=16))
    assert ("bullish_three_line_strike", "buy") in found_candles(strike)


def test_indecision_candles_are_log_only_by_default():
    doji = candles((108, 109.0, 107.0, 108.02), before=UP)
    out = find(doji, atr(doji[-30:]), SC)
    assert [(k, s, w) for k, s, _, w, _ in out if k in ("doji", "long_legged_doji")] == [("long_legged_doji", "sell", True)]


def test_thinkorswim_candles_added():
    swallow = candles((106, 106, 104, 104), (104, 104, 102, 102), (101.5, 102.5, 100.9, 101.0),
                      (102.6, 102.7, 100.5, 100.6), before=DOWN)
    assert ("concealing_baby_swallow", "buy") in found_candles(swallow)
    gap_up = candles((108, 110.1, 107.9, 110), (110.5, 112.6, 110.4, 112.5), (112.0, 112.1, 109.4, 109.5), before=UP)
    assert ("upside_gap_three_methods", "buy") in found_candles(gap_up)
    gap_down = candles((104, 104.1, 101.9, 102), (101.5, 101.6, 99.4, 99.5), (100, 102.6, 99.9, 102.5), before=DOWN)
    assert ("downside_gap_three_methods", "sell") in found_candles(gap_down)
    gapping = candles((108, 110.1, 107.9, 110), (109.9, 110.1, 109.7, 109.8), (109.8, 110.0, 109.6, 109.9),
                      (109.9, 110.1, 109.7, 109.95), (109.95, 110.1, 109.8, 109.9), (110.4, 110.9, 110.35, 110.8),
                      before=UP)
    assert ("high_price_gapping_play", "buy") in found_candles(gapping)
    white = candles((108, 108.6, 107.9, 108.5), (109, 109.7, 108.95, 109.6), (109.02, 109.7, 108.97, 109.62), before=UP)
    assert ("bullish_side_by_side_white_lines", "buy") in found_candles(white)
    south = candles((105, 105.1, 101.8, 103), (104, 104.1, 102.5, 103.0), (103.3, 103.3, 103.0, 103.0), before=DOWN)
    assert ("three_stars_in_the_south", "buy") in found_candles(south)
    peak = candles((104.0, 104.5, 103.9, 104.4), (104.4, 105.0, 104.3, 104.9), (104.9, 106.0, 104.8, 105.5),
                   (105.5, 105.6, 104.6, 104.7), (104.7, 104.8, 104.0, 104.1), before=DOWN)
    assert ("williams_fractal_high", "sell") in found_candles(peak)
    assert ("zigzag_step_up", "buy") in kinds(hits(path(100, 104, 102, 106), candle_patterns, start=12))
    assert ("zigzag_step_down", "sell") in kinds(hits(path(106, 102, 104, 100), candle_patterns, start=12))


def test_pattern_groups_and_lists_come_from_the_config():
    bars = path(100, 104, 101, 104.05, 99.5)

    def double_tops(cfg):
        out = []
        for end in range(20, len(bars) + 1):
            out += [w for k, _, _, w, _ in find(bars[:end], atr(bars[:end][-30:]), cfg) if k == "double_top"]
        return out

    assert double_tops(SC) == [False]
    assert double_tops(SC.model_copy(update={"pattern_groups": ["candlestick", "fibonacci"]})) == []
    assert double_tops(SC.model_copy(update={"disabled_patterns": ["double_top"]})) == []
    assert double_tops(SC.model_copy(update={"log_only_patterns": ["double_top"]})) == [True]


def test_random_walk_stays_quiet():
    rng = random.Random(7)
    price, pts = 100.0, [100.0]
    for _ in range(80):
        price += rng.uniform(-1, 1)
        pts.append(price)
    bars = path(*pts, steps=3, wiggle=0.15)
    chart = hits(bars, chart_patterns)
    assert len(chart) <= len(bars) / 15  # a pattern every 15 bars at most, on noise
