"""The Charts tab's intervals, chart types and profiles (paperbot/charting.py)."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from paperbot import charting as ch
from paperbot.features import Bar

TZ = ZoneInfo("America/New_York")


def at(h, m=0, s=0, day=25):
    return datetime(2026, 11, day, h, m, s, tzinfo=TZ)


def minute_bars(start, closes, vol=100.0):
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        out.append(Bar(start + timedelta(minutes=i), prev, max(prev, c) + 0.05, min(prev, c) - 0.05, c, vol))
        prev = c
    return out


@pytest.mark.parametrize("text,unit,size,label", [
    ("1", "m", 1, "1m"), ("5m", "m", 5, "5m"), ("90", "m", 90, "90m"), ("2h", "m", 120, "2h"), ("4H", "m", 240, "4h"),
    ("30s", "s", 30, "30s"), ("D", "D", 1, "1D"), ("1d", "D", 1, "1D"), ("W", "W", 1, "1W"), ("3M", "M", 3, "3M"),
    ("100T", "T", 100, "100T"), ("R0.5", "R", 0.5, "R0.5"), ("0.25R", "R", 0.25, "R0.25"), ("1440", "D", 1, "1D"),
])
def test_intervals_read_like_tradingview(text, unit, size, label):
    iv = ch.parse_interval(text)
    assert (iv.unit, iv.size, iv.label) == (unit, size, label)


@pytest.mark.parametrize("text", ["", "x", "0m", "1.5m", "5000m", "-1"])
def test_bad_intervals_are_refused(text):
    with pytest.raises(ValueError):
        ch.parse_interval(text)


def test_minutes_line_up_from_each_session_open():
    bars = minute_bars(at(9, 0), [100 + i * 0.01 for i in range(120)])
    ext = ch.resample_minutes(bars, 60, extended=True)
    assert [b.t.time().isoformat() for b in ext] == ["09:00:00", "10:00:00"]
    rth = ch.resample_minutes(bars, 60, extended=False)
    assert [b.t.time().isoformat() for b in rth] == ["09:30:00", "10:30:00"]  # like an RTH chart: 9:30, 10:30
    assert rth[0].o == bars[30].o and rth[0].c == bars[89].c and rth[0].v == 6000
    assert rth[1].c == bars[-1].c  # the forming one


def test_days_into_weeks_and_months():
    days = [Bar(datetime(2026, 10, 26, tzinfo=TZ) + timedelta(days=i), 1 + i, 2 + i, i, 1.5 + i, 10) for i in range(14) if
            (datetime(2026, 10, 26) + timedelta(days=i)).weekday() < 5]
    weeks = ch.resample_days(days, "W")
    assert [w.t.date().isoformat() for w in weeks] == ["2026-10-26", "2026-11-02"]
    assert weeks[0].o == days[0].o and weeks[0].c == days[4].c and weeks[0].v == 50
    months = ch.resample_days(days, "M")
    assert [m.t.date().isoformat() for m in months] == ["2026-10-26", "2026-11-02"]


def prints(prices, start=None, size=10.0):
    start = start or at(10)
    return [ch.Print(start + timedelta(seconds=i), p, size) for i, p in enumerate(prices)]


def test_seconds_and_ticks_from_trades():
    ps = prints([10, 11, 9, 12, 13])
    secs = ch.second_bars(ps, 2)
    assert [(b.o, b.h, b.l, b.c, b.v) for b in secs] == [(10, 11, 10, 11, 20), (9, 12, 9, 12, 20), (13, 13, 13, 13, 10)]
    ticks = ch.tick_bars(ps, 3)
    assert [(b.o, b.c, b.v) for b in ticks] == [(10, 9, 30), (12, 13, 20)]


def test_range_bars_cover_exactly_their_range_and_fill_gaps():
    ps = prints([100.0, 100.2, 100.5, 100.6, 101.7, 101.0])
    bars = ch.range_bars(ps, 0.5)
    for b in bars[:-1]:
        assert b.h - b.l == pytest.approx(0.5)
        assert b.c in (b.h, b.l)  # closes at the edge
    assert [round(b.o, 2) for b in bars] == [100.0, 100.5, 101.0, 101.5, 101.2]  # each opens where the last closed
    assert bars[2].v == 0  # the jump from 100.6 to 101.7 left a candle no trade was in
    assert sum(b.v for b in bars) == pytest.approx(60)


def test_heikin_ashi_formula():
    bars = [Bar(at(10), 10, 12, 9, 11, 1), Bar(at(10, 1), 11, 13, 10, 12, 1)]
    ha = ch.heikin_ashi(bars)
    assert ha[0].c == pytest.approx(10.5) and ha[0].o == pytest.approx(10.5)
    assert ha[1].o == pytest.approx((ha[0].o + ha[0].c) / 2) and ha[1].c == pytest.approx(11.5)
    assert ha[1].h == 13 and ha[1].l == 10


def test_renko_turns_only_after_two_boxes():
    bars = minute_bars(at(10), [100.2, 101.1, 102.3, 101.4, 100.9, 99.8])
    bricks = ch.renko(bars, 1.0)
    assert [(b.o, b.c) for b in bricks] == [(100, 101), (101, 102), (101, 100)]  # the turn needs a close at or below 100, two boxes down


def test_line_break_needs_three_lines_to_turn():
    bars = minute_bars(at(10), [10, 11, 12, 13, 11.5, 9.5])
    lines = ch.line_break(bars, 3)
    assert [(b.o, b.c) for b in lines] == [(10, 10), (10, 11), (11, 12), (12, 13), (12, 9.5)]  # 11.5 isn't below 10, the last 3 lines' low


def test_kagi_turns_and_thickness():
    bars = minute_bars(at(10), [10, 12, 11, 13, 9, 10.5])
    cols = ch.kagi(bars, 1.0)
    assert [(c.start, c.end) for c in cols] == [(10, 12), (12, 11), (11, 13), (13, 9), (9, 10.5)]
    assert cols[0].thick is True and cols[3].switch == 11 and cols[4].thick is False  # thin once below the 11 waist


def test_point_and_figure_columns():
    bars = minute_bars(at(10), [100, 101.2, 103.4, 102.5, 100.1, 99.0, 101.0, 102.2])
    cols = ch.point_figure(bars, 1.0, 3)
    assert [(c.up, c.bottom, c.top) for c in cols] == [(True, 100, 103), (False, 99, 102), (True, 100, 102)]


def test_volume_profile_keeps_every_share_and_finds_the_busy_price():
    bars = [Bar(at(10), 100, 101, 100, 100.5, 1000), Bar(at(10, 1), 100.5, 100.6, 100.4, 100.5, 9000),
            Bar(at(10, 2), 100.5, 102, 100.5, 101.9, 1000)]
    vp = ch.volume_profile(bars, rows=20)
    assert sum(u + d for _, u, d in vp.rows) == pytest.approx(11000)
    assert 100.4 <= vp.poc <= 100.6
    assert vp.val <= vp.poc <= vp.vah


def test_tpo_letters_by_half_hour():
    bars = minute_bars(at(9, 30), [100 + (i % 30) * 0.02 for i in range(60)]) + minute_bars(at(10, 30), [101 + i * 0.01 for i in range(30)])
    t = ch.tpo(bars, step=0.1)
    letters = "".join(s for _, s in t.rows)
    assert set(letters) == {"A", "B", "C"}
    assert t.ib_low == min(b.l for b in bars[:60]) and t.ib_high == max(b.h for b in bars[:60])
    assert t.val <= t.poc <= t.vah


def test_csv_has_the_studies_too():
    text = ch.to_csv([Bar(at(10), 1, 2, 0.5, 1.5, 100)], {"RSI 14": [None]})
    assert text.splitlines() == ["time,open,high,low,close,volume,RSI 14", "2026-11-25T10:00:00-05:00,1,2,0.5,1.5,100,"]
