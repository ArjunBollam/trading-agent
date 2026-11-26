"""Support / resistance breaks and squeeze releases (overlays.py) and how the scanner hands them to the dashboard."""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from test_minutes import MinuteSource, minute_bars, setup
from test_qqe import five_minute_bars, walk
from test_scan import PREV, TODAY, TZ, FakeSource, at, breakout_bars, make_scanner

from paperbot.features import Bar
from paperbot.overlays import level_breaks, overnight_gap, pivot_levels, squeeze


def bar(i: int, o: float, c: float, v: float = 1000, up: float = 0.05, down: float = 0.05) -> Bar:
    t = datetime(2026, 10, 26, 9, 30, tzinfo=TZ) + timedelta(minutes=5 * i)
    return Bar(t, o, max(o, c) + up, min(o, c) - down, c, v)


def peak_then_range() -> list[Bar]:
    """A peak at 105 on candle 20, then a quiet stretch around 100."""
    closes = [100 + 0.2 * i for i in range(21)] + [104 - 0.25 * i for i in range(1, 17)] + [100.0] * 10
    return [bar(i, closes[i - 1] if i else 100, c, up=0.2 if i == 20 else 0.05) for i, c in enumerate(closes)]


def random_bars(seed: int, n: int = 400) -> list[Bar]:
    r, c, out = random.Random(seed), 100.0, []
    for i in range(n):
        o, c = c, c * (1 + r.gauss(0, 0.002 if (i // 60) % 2 else 0.0006))
        out.append(bar(i, o, c, r.lognormvariate(8, 0.6), abs(r.gauss(0, 0.07)), abs(r.gauss(0, 0.07))))
    return out


def test_a_pivot_is_a_level_only_once_it_is_confirmed():
    bars = peak_then_range()
    levels = pivot_levels(bars)
    assert levels[35][0] is None  # the peak on candle 20 needs 15 candles after it, and counts from the next one
    assert levels[36][0] == bars[20].h


def test_a_close_through_resistance_needs_volume_or_a_long_wick():
    bars, n = peak_then_range(), len(peak_then_range())
    high = bars[20].h
    assert level_breaks([*bars, bar(n, 104, high + 0.5, 1000)]) == []  # no volume
    up, = level_breaks([*bars, bar(n, 104, high + 0.5, 5000)])
    assert (up.index, up.side, up.kind, up.level) == (n, "long", "break", high) and up.volume > 20
    wick, = level_breaks([*bars, bar(n, 104, high + 0.5, 1000, down=3)])  # a lower wick longer than the body
    assert (wick.side, wick.kind) == ("long", "wick")
    assert level_breaks([*bars, bar(n, 104, high - 0.1, 9000)]) == []  # a close under it isn't a break


def test_signals_never_change_as_candles_are_added():
    for seed in range(10):
        bars = random_bars(seed)
        for cut in (150, 260, 399):
            assert level_breaks(bars[:cut]) == [x for x in level_breaks(bars) if x.index < cut]
            assert squeeze(bars[:cut])[0] == [x for x in squeeze(bars)[0] if x.index < cut]


def test_a_squeeze_releases_in_the_direction_of_momentum():
    wide = [bar(i, 100, 100 + 0.4 * (-1) ** i, up=0.5, down=0.5) for i in range(45)]
    quiet = [bar(45 + i, 100, 100 + 0.02 * (-1) ** i, up=0.4, down=0.4) for i in range(30)]
    _, on = squeeze(wide + quiet)
    assert on > 0  # small closes inside wide ranges: the bands are inside the channels
    for step, side in ((0.9, "long"), (-0.9, "short")):
        move = [bar(75 + i, 100 + step * i, 100 + step * (i + 1), up=0.05, down=0.05) for i in range(8)]
        found, still = squeeze(wide + quiet + move)
        assert found[-1].index >= 75 and found[-1].side == side and found[-1].candles >= on and still == 0
        assert (found[-1].momentum > 0) == (side == "long")


def test_a_break_by_the_overnight_gap_is_marked():
    yesterday = peak_then_range()
    n, high = len(yesterday), max(b.h for b in yesterday)
    low = pivot_levels(yesterday)[-1][1]
    assert low is None  # only a peak so far, so only resistance

    def next_day(o: float, c: float, at_bar: int = 0) -> Bar:
        t = datetime(2026, 10, 27, 9, 30, tzinfo=TZ) + timedelta(minutes=5 * at_bar)
        return Bar(t, o, max(o, c) + 0.05, min(o, c) - 0.05, c, 6000)

    gap, = level_breaks([*yesterday, next_day(high + 1, high + 1.5)])  # opened above resistance
    assert (gap.index, gap.side, gap.gap) == (n, "long", True)
    real, = level_breaks([*yesterday, next_day(high - 1, high + 0.5)])  # opened under it, and broke it
    assert (real.side, real.gap) == ("long", False)
    later, = level_breaks([*yesterday, next_day(high - 1, high - 0.5), next_day(high + 0.2, high + 0.6, 1)])
    assert (later.index, later.gap) == (n + 1, False)  # not the session's first candle

    bars = [*yesterday, next_day(high + 1, high + 1.5)]
    assert overnight_gap(bars, n) and not overnight_gap(bars, n - 1) and not overnight_gap(bars, 0)
    assert not overnight_gap([*yesterday, next_day(yesterday[-1].c + 0.01, yesterday[-1].c + 2)], n)  # no gap to speak of


def test_a_squeeze_released_by_the_overnight_gap_is_marked():
    quiet = [bar(i, 100, 100 + 0.02 * (-1) ** i, up=0.4, down=0.4) for i in range(60)]
    t = datetime(2026, 10, 27, 9, 30, tzinfo=TZ)
    released = squeeze([*quiet, Bar(t, 95, 95.2, 94.8, 94.9, 1000)])[0][-1]
    assert (released.index, released.side, released.gap) == (60, "short", True)
    same_day = squeeze([*quiet, bar(60, 95, 94.9)])[0][-1]
    assert (same_day.index, same_day.gap) == (60, False)


def test_too_few_candles_give_nothing():
    few = random_bars(1, 12)
    assert level_breaks(few) == [] and squeeze(few) == ([], 0) and level_breaks([]) == [] and squeeze([]) == ([], 0)


def test_the_scanner_shows_todays_overlays_and_each_can_be_switched_off(cfg):
    bars = five_minute_bars(walk(3, 156))
    cfg.scan.chart_sessions = 0
    clock = [at(TODAY, 78) + timedelta(seconds=10)]
    scanner, rec = make_scanner(cfg, FakeSource({"SPY": bars}, []), lambda: clock[0])
    r = scanner.scan()
    view = scanner.view["SPY"]
    today = {int(at(TODAY, k).timestamp()) for k in range(78)}
    marks = view["sr"]["breaks"] + view["sqz"]["signals"]
    assert marks and all(m["t"] in today and m["side"] in ("long", "short") for m in marks)
    assert [(s["side"], s["t"]) for s in view["sqz"]["signals"]] == [
        (x.side, int(bars[x.index].t.timestamp())) for x in squeeze(bars)[0] if bars[x.index].t.date() == TODAY]
    levels = view["sr"]["levels"]
    assert levels[0][0] == int(at(TODAY, 0).timestamp()) and all(a[1:] != b[1:] for a, b in zip(levels, levels[1:], strict=False))
    assert not [f for f in r.found if f.signal.kind in ("sr", "sqz", "break", "squeeze")]  # shown only

    cfg.scan.sr_breaks = cfg.scan.squeeze = False
    scanner, rec = make_scanner(cfg, FakeSource({"SPY": bars}, []), lambda: clock[0])
    scanner.scan()
    assert "qqe" in scanner.view["SPY"] and not {"sr", "sqz"} & set(scanner.view["SPY"])


def test_the_one_minute_charts_have_their_own_overlays(cfg, tmp_path):
    import json

    minutes = minute_bars(PREV, 390, walk(1, 390)) + minute_bars(TODAY, 60, walk(2, 60))
    scanner, _, path = setup(cfg, tmp_path, MinuteSource({"SPY": breakout_bars()}, minutes), at(TODAY, 11) + timedelta(seconds=5))
    scanner.scan()
    one = json.loads(path.read_text())["symbols"]["SPY"]
    seen = [b for b in minutes if b.t < at(TODAY, 11)]
    assert [s["t"] for s in one["sqz"]["signals"]] == [
        int(seen[x.index].t.timestamp()) for x in squeeze(seen)[0] if seen[x.index].t.date() == TODAY]
    assert one["sr"]["levels"][0][0] == int(at(TODAY, 0).timestamp()) and "qqe" in one
