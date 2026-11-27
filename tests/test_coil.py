"""VWAP coil breakouts and breakdowns (formations.coils), on SPY's real 5-minute candles of 2026-11-25.

That morning SPY flushed from 703.18 to 695.45 at the open, made lower highs (705.05, 702.94, 701.36) into a flat
floor near 697.5, boxed in 697.38-702.02 around a flat VWAP of about 700 from 10:40 to 11:55 ET on under half the
morning's volume, and then the 12:00 candle closed at 703.81, above the box and VWAP. SPY reached 718.43 by 14:30.
"""

from __future__ import annotations

import csv
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from paperbot import claims
from paperbot.config import ScanConfig
from paperbot.explain import EXPLAIN
from paperbot.features import Bar
from paperbot.formations import COIL, catalog, coils
from paperbot.patterns import LABELS, analyze

ET = ZoneInfo("America/New_York")
DATA = Path(__file__).parent / "data" / "spy_2026-11-25_5m.csv"
SC = ScanConfig()


def spy() -> list[Bar]:
    with DATA.open() as f:
        return [Bar(datetime.strptime(r["time_et"], "%Y-%m-%d %H:%M").replace(tzinfo=ET), float(r["open"]),
                    float(r["high"]), float(r["low"]), float(r["close"]), float(r["volume"])) for r in csv.DictReader(f)]


def coil_signals(bars: list[Bar], sc: ScanConfig = SC):
    """Every coil signal as the day is scanned candle by candle."""
    day = bars[0].t.date()
    return [s for n in range(2, len(bars) + 1) for s in analyze("SPY", bars[:n], day, sc)[0] if s.kind in COIL]


def at(bars: list[Bar], hhmm: str) -> int:
    return next(i for i, b in enumerate(bars) if f"{b.t:%H:%M}" == hhmm)


def test_spy_breaks_out_of_its_coil_once_at_noon():
    bars = spy()
    found = coil_signals(bars)
    assert [(f"{s.bar_time:%H:%M}", s.kind, s.side, s.weak) for s in found] == [
        ("12:00", "vwap_coil_breakout", "buy", False)]
    s = found[0]
    assert s.price == 703.81 and s.level == "11/25 10:40"
    assert "lower highs 705.05, 702.94" in s.detail and "(descending triangle)" in s.detail
    assert "coiled 697.38-702.02 around a flat VWAP 700.10 for 80 min (10:40-11:55)" in s.detail
    assert "measured target 711.62" in s.detail  # 702.02 + (705.05 - 695.45)
    end = at(bars, "12:00") + 1
    assert claims.faults(s.kind, s.side, s.price, s.detail, s.level, bars[:end], bars[0].t.date(), SC) == []
    assert max(b.h for b in bars[end:]) > 711.62  # it got there: 711.83 at 12:50, 718.43 later


def test_the_coil_itself():
    bars = spy()
    end = at(bars, "12:00") + 1
    (c,) = coils(bars[:end], 2.21, SC)
    assert (c.top, c.bottom, c.extreme, c.shape) == (702.02, 697.38, 695.45, "descending triangle")
    assert c.dryup < 0.5 and 1.2 <= c.volume < 1.3 and c.stop < c.bottom


def test_upside_down_spy_breaks_down():
    flip = [Bar(b.t, 1400 - b.o, 1400 - b.l, 1400 - b.h, 1400 - b.c, b.v) for b in spy()]
    found = coil_signals(flip)
    assert [(f"{s.bar_time:%H:%M}", s.kind, s.side) for s in found] == [("12:00", "vwap_coil_breakdown", "sell")]
    assert "higher lows" in found[0].detail and "(ascending triangle)" in found[0].detail
    assert "coiled 697.98-702.62" in found[0].detail and "closed below the box" in found[0].detail


def test_no_coil_without_the_volume_drying_up():
    flat = [replace(b, v=300_000) for b in spy()]
    assert coil_signals(flat) == []


def test_a_break_on_light_volume_is_weak():
    bars = spy()
    i = at(bars, "12:00")
    bars[i] = replace(bars[i], v=200_000)  # under the box's average
    found = [s for s in coil_signals(bars[:i + 1]) if s.bar_time == bars[i].t]
    assert [(s.kind, s.weak) for s in found] == [("vwap_coil_breakout", True)]
    assert "light, so less reliable" in found[0].detail


def test_it_can_be_turned_off():
    assert coil_signals(spy(), SC.model_copy(update={"disabled_patterns": ["vwap_coil_breakout"]})) == []


def test_listed_labelled_and_explained():
    assert {k for g, k, _ in catalog() if g == "classic"} >= COIL
    assert COIL <= set(LABELS) and COIL <= set(EXPLAIN)
