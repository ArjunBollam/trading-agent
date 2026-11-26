"""claims.py: the separate check of what each signal says, and of the charts' labels."""

from __future__ import annotations

from dataclasses import replace

import pytest
from test_overlays import random_bars
from test_scan import PREV, SC, TODAY, at, breakout_bars, kinds, prev_session

from paperbot import claims, formations
from paperbot.features import Bar
from paperbot.overlays import level_breaks, squeeze
from paperbot.patterns import analyze
from paperbot.qqe import qqe
from paperbot.smc import INTERNAL, smart_money


def two_days(seed: int) -> list[Bar]:
    return [replace(b, t=at(PREV, i) if i < 78 else at(TODAY, i - 78)) for i, b in enumerate(random_bars(seed, 156))]


def check(sig, bars):
    return claims.faults(sig.kind, sig.side, sig.price, sig.detail, sig.level, bars, TODAY, SC)


@pytest.mark.parametrize("seed", range(6))
def test_every_signal_the_scanner_makes_holds_up(seed):
    bars, seen = two_days(seed), set()
    for end in range(80, 157):
        for sig in analyze("T", bars[:end], TODAY, SC)[0]:
            got = check(sig, bars[:end])
            assert not got, (sig.kind, sig.detail, got)
            seen.add(sig.kind)
    assert len(seen) >= 12


def test_a_signal_that_says_something_else_is_caught():
    bars = breakout_bars()
    sig = next(s for s in analyze("T", bars, TODAY, SC)[0] if s.kind == "breakout")
    assert check(sig, bars) == []
    assert "closed at 101.50, not 102.50" in check(replace(sig, price=102.5), bars)[0]
    assert "prior-day high was 101.00, not 100.40" in check(replace(sig, detail="closed above the prior-day high 100.40 on 2.9x volume"), bars)[0]
    assert "volume was" in check(replace(sig, detail=sig.detail.replace("2.9x", "9.9x").replace("3.0x", "9.9x")), bars)[-1]
    fake = replace(sig, kind="bearish_engulfing", side="sell", detail="red body engulfed the prior green body, after a rise")
    assert "the candles don't have that shape" in check(fake, bars)


def test_a_level_that_was_not_reached_is_a_wording_fault():
    bars = prev_session() + [Bar(at(TODAY, i), 100.5, 100.8, 100.2, 100.5, 1000) for i in range(10)]
    bars.append(Bar(at(TODAY, 10), 100.5, 100.95, 100.3, 100.6, 1000))  # 0.05 under the prior-day high, 101
    sig = next(s for s in analyze("T", bars, TODAY, SC)[0] if s.kind == "resistance_test" and s.level == "prior-day high")
    assert sig.detail == "came within 0.05 of the prior-day high 101.00 (high 100.95) and closed below it" and check(sig, bars) == []
    loose = check(replace(sig, detail="tested the prior-day high 101.00 (high 100.95) and closed below it"), bars)
    assert loose == [f"{claims.WORDING}it came within 0.05 of 101.00 but didn't reach it (high 100.95)"]


def test_a_candle_below_a_level_is_not_at_support():
    """2026-11-25, SPY 12:30: 'engulfed at the pivot S2 708.50' on a candle that never traded above 708.36."""
    day = [Bar(at(TODAY, i), 99.2, 99.35, 99.1, 99.2, 1000) for i in range(10)]  # under the prior-day low, 99, after a break
    day += [Bar(at(TODAY, 10), 98.95, 98.97, 98.80, 98.82, 1000), Bar(at(TODAY, 11), 98.81, 98.99, 98.78, 98.96, 1000)]
    bars = prev_session() + day
    assert not [s for s in analyze("T", bars, TODAY, SC)[0] if "prior-day low" in s.detail and s.side == "buy"]
    got = claims.faults("bullish_engulfing", "buy", 98.96, "green bar engulfed the prior red bar at the prior-day low 99.00", None, bars, TODAY, SC)
    assert got == ["it closed at 98.96, below the prior-day low, which isn't support then"]
    day[-1] = Bar(at(TODAY, 11), 98.81, 99.12, 98.78, 99.1, 1000)  # back above it: support again
    assert "bullish_engulfing" in kinds(prev_session() + day)


def test_reached_and_tested_say_how_close():
    assert formations.tested("the pivot S1", 29.20, 29.17, up=False) == "tested the pivot S1 29.20 (low 29.17)"
    assert formations.tested("the pivot S1", 29.20, 29.23, up=False) == "came within 0.03 of the pivot S1 29.20 (low 29.23)"


@pytest.mark.parametrize("seed", range(8))
def test_the_charts_labels_come_out_the_same_the_long_way(seed):
    bars = random_bars(seed, 500)
    assert claims.qqe_labels([b.c for b in bars], SC) == {s.index: s.side for s in qqe([b.c for b in bars])}
    assert claims.level_break_labels(bars, SC) == {x.index: (x.side, x.kind, x.level) for x in level_breaks(bars)}
    assert claims.squeeze_labels(bars, SC) == {x.index: x.side for x in squeeze(bars)[0]}
    m = smart_money(bars)
    assert len(m.structures) > 10
    for x in m.structures:
        assert claims.structure_faults(bars, x.index, x.pivot, x.level, x.side, INTERNAL if x.internal else 50) == []
    for x in m.blocks:
        assert claims.zone_faults(bars, x.index, x.top, x.bottom, x.side) == []
    x = next(x for x in m.structures if x.side == "long")
    assert "not 999.00" in claims.structure_faults(bars, x.index, x.pivot, 999.0, "long", INTERNAL)[0]
    assert claims.structure_faults(bars, x.index + 1, x.pivot, x.level, "long", INTERNAL)  # a candle late


def test_a_live_cross_is_checked_against_the_minutes_around_it():
    around = [Bar(at(TODAY, 0), 100, 100.6, 99.9, 100.5, 10), Bar(at(TODAY, 1), 100.5, 101.2, 100.4, 101.1, 10)]
    assert claims.live_cross_faults("buy", 101.1, 101.0, around) == []
    assert "didn't cross 99.00" in claims.live_cross_faults("buy", 101.1, 99.0, around)[0]  # crossed long before
    assert "didn't trade" in claims.live_cross_faults("buy", 103.0, 101.0, around)[0]
    assert claims.live_cross_faults("buy", 101.1, 101.0, []) is None
