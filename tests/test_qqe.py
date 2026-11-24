"""QQE Long / QQE Short (qqe.py) and how the scanner hands them to the dashboard."""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta

from paperbot.features import Bar
from paperbot.qqe import qqe

from test_scan import PREV, TODAY, TZ, FakeSource, at, make_scanner


def walk(seed: int, n: int = 400) -> list[float]:
    rnd = random.Random(seed)
    out = [100.0]
    while len(out) < n:
        out.append(out[-1] * (1 + rnd.gauss(0, 0.0015)))
    return out


def test_signals_alternate_and_sit_on_the_right_side_of_the_line():
    for seed in range(20):
        sigs = qqe(walk(seed))
        assert sigs and all(a.side != b.side for a, b in zip(sigs, sigs[1:], strict=False))
        assert all((s.rsi > s.line) == (s.side == "long") for s in sigs)


def test_a_fall_then_a_rally_gives_a_short_then_a_long():
    flat = [100 + 0.05 * math.sin(i) for i in range(150)]
    fall = [flat[-1] - 0.2 * i for i in range(1, 25)]
    rally = [fall[-1] + 0.25 * i for i in range(1, 25)]
    sigs = [s for s in qqe(flat + fall + rally) if s.index >= 150]
    assert "short" in {s.side for s in sigs if s.index < 174}
    longs = [s.index for s in sigs if s.side == "long" and s.index >= 174]
    assert longs and longs[0] < 190  # soon after the turn, on a completed candle


def test_past_signals_never_change_as_candles_are_added():
    closes = walk(7)
    early, late = qqe(closes[:300]), qqe(closes)
    assert late[: len(early)] == early  # no repainting


def test_too_few_candles_or_a_flat_line_give_nothing():
    assert qqe([]) == [] and qqe([100.0] * 40) == []
    assert qqe([100.0] * 400) == []


def five_minute_bars(closes: list[float], extended: bool = False) -> list[Bar]:
    """Two sessions of regular bars (78 + 78), today's last; optionally with wild pre-market bars between."""
    out, i = [], 0
    for day in (PREV, TODAY):
        if extended and day == TODAY:
            out += [Bar(datetime(day.year, day.month, day.day, 8, 0, tzinfo=TZ) + timedelta(minutes=5 * k),
                        c := 100 + 10 * (-1) ** k, c + 1, c - 1, c, 10) for k in range(18)]
        for k in range(78):
            c = closes[i]
            out.append(Bar(at(day, k), c, c + 0.2, c - 0.2, c, 1000))
            i += 1
    return out


def test_the_scanner_shows_todays_qqe_signals_on_regular_candles_only(cfg):
    closes = walk(3, 156)
    clock = [at(TODAY, 78) + timedelta(seconds=10)]
    views = []
    for ext in (False, True):
        scanner, rec = make_scanner(cfg, FakeSource({"SPY": five_minute_bars(closes, ext)}, []), lambda: clock[0])
        scanner.scan()
        views.append(scanner.view["SPY"]["qqe"])
    assert views[0] and views[0] == views[1]  # pre-market candles don't change it
    today = {int(at(TODAY, k).timestamp()) for k in range(78)}
    assert all(q["t"] in today and q["side"] in ("long", "short") and {"rsi", "line"} <= set(q) for q in views[0])
    want = [(s.side, int(five_minute_bars(closes)[s.index].t.timestamp())) for s in qqe(closes) if s.index >= 78]
    assert [(q["side"], q["t"]) for q in views[0]] == want


def test_qqe_is_shown_only_and_can_be_switched_off(cfg):
    clock = [at(TODAY, 78) + timedelta(seconds=10)]
    source = FakeSource({"SPY": five_minute_bars(walk(3, 156))}, [])
    scanner, rec = make_scanner(cfg, source, lambda: clock[0])
    r = scanner.scan()
    assert not [f for f in r.found if "qqe" in f.signal.kind]  # never a signal, so never alerted or traded
    cfg.scan.qqe = False
    scanner, rec = make_scanner(cfg, source, lambda: clock[0])
    scanner.scan()
    assert "qqe" not in scanner.view["SPY"]
