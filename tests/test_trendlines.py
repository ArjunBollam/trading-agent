"""Drawn and found trend lines, the break-and-retest tracker, and alerts on lines (paperbot/trendlines.py, alerts.py)."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from paperbot import alerts as al
from paperbot.features import Bar
from paperbot.trendlines import Drawings, Line, auto_lines, line_from_json, market_clock, track

from test_chartdata import NOW, FakeMarket, web  # noqa: F401 - the web fixture

TZ = ZoneInfo("America/New_York")


def at(day, h, m=0):
    return datetime(2026, 11, day, h, m, tzinfo=TZ)


def test_the_market_clock_skips_nights_and_weekends():
    assert market_clock(at(23, 4)) - market_clock(at(20, 20)) == 0  # Friday's close to Monday's pre-market open
    assert market_clock(at(21, 12)) == market_clock(at(20, 20))  # a Saturday is still Friday's close
    assert market_clock(at(24, 0)) - market_clock(at(23, 0)) == 16 * 3600  # a weekday: one 4:00-20:00 session
    assert market_clock(at(23, 10), "rth") - market_clock(at(23, 9, 30), "rth") == 1800
    assert market_clock(at(23, 8), "rth") == market_clock(at(23, 9, 30), "rth")  # before the open, for an RTH line


def test_a_line_is_straight_in_trading_time():
    line = Line("a", "trend", at(20, 10), 100.0, at(20, 11), 101.0)
    assert line.price_at(at(20, 10, 30)) == pytest.approx(100.5)
    assert line.price_at(at(23, 10)) == pytest.approx(116.0)  # 10 hours left on Friday, 6 on Monday: 16 more
    assert line.price_at(at(20, 9)) is None  # it starts at its first point
    assert Line("b", "trend", at(20, 10), 100.0, at(20, 11), 101.0, extend=False).price_at(at(20, 12)) is None
    assert Line("c", "hline", at(20, 10), 99.0, at(20, 10), 99.0).price_at(at(19, 10)) == 99.0
    assert Line("d", "hray", at(20, 10), 99.0, at(20, 10), 99.0).price_at(at(19, 10)) is None
    # saved with its points the other way round: still the same line
    flipped = line_from_json({"kind": "trend", "t1": at(20, 11).timestamp(), "p1": 101, "t2": at(20, 10).timestamp(), "p2": 100}, TZ)
    assert flipped.p1 == 100 and flipped.price_at(at(20, 10, 30)) == pytest.approx(100.5)
    with pytest.raises(ValueError):
        line_from_json({"kind": "circle", "t1": 0, "p1": 1}, TZ)


def candles(spec, start=None):
    """1-minute candles from (open, high, low, close) rows."""
    start = start or at(25, 10)
    return [Bar(start + timedelta(minutes=i), o, h, low, c, 1000.0) for i, (o, h, low, c) in enumerate(spec)]


FLAT = Line("r", "hline", at(25, 9), 100.0, at(25, 9), 100.0, name="")


def test_break_then_a_retest_that_holds():
    bars = candles([(99.0, 99.5, 98.8, 99.2)] * 15 + [
        (99.2, 100.6, 99.1, 100.5),   # 10:15 closes above 100: the break
        (100.5, 101.2, 100.4, 101.0),  # 10:16 pulls away
        (101.0, 101.1, 99.95, 100.4),  # 10:17 low 99.95 reaches the line, closes 100.40 above it: confirmed
        (100.4, 100.6, 99.0, 99.1),
    ])
    stages = track(FLAT, bars)
    assert [(s.stage, s.index, s.side) for s in stages] == [("broken", 15, "up"), ("confirmed", 17, "up")]
    assert "The 10:15 candle closed at 100.50, above the horizontal line 100.00 (100.00 there)." == stages[0].text
    assert "its low 99.95 reached the line (100.00 there)" in stages[1].text and "closed at 100.40, still above it" in stages[1].text
    assert all(s.index < 18 for s in stages)  # it stops once confirmed: the later close below isn't a new break


def test_a_wick_that_only_comes_close_says_so_and_a_failed_retest_resets():
    bars = candles([(99.0, 99.5, 98.8, 99.2)] * 15 + [
        (99.2, 100.6, 99.1, 100.5),    # break up
        (100.5, 100.7, 99.7, 99.8),    # comes back and closes below: failed
        (99.8, 99.9, 99.5, 99.6),
        (99.6, 100.9, 99.6, 100.8),    # a new break up
        (100.8, 100.9, 100.01, 100.5),  # low 100.01: not at the line, within a tenth of ATR
    ])
    stages = track(FLAT, bars)
    assert [s.stage for s in stages] == ["broken", "failed", "broken", "confirmed"]
    assert "closed at 99.80, below the line again" in stages[1].text
    assert "its low 100.01 came within 0.01 of the line (100.00 there; the tolerance is a tenth of ATR" in stages[3].text


def test_a_falling_line_breaks_down_too():
    line = Line("u", "trend", at(25, 10), 100.0, at(25, 10, 20), 102.0)  # rising 0.1 a minute
    bars = candles([(100.5 + 0.1 * i, 100.8 + 0.1 * i, 100.3 + 0.1 * i, 100.6 + 0.1 * i) for i in range(10)] + [
        (101.4, 101.5, 100.6, 100.7),   # 10:10, line at 101.00: closes below it
        (100.7, 101.12, 100.5, 100.9),  # 10:11, line 101.10: high 101.12 reaches it, closes below: confirmed down
    ])
    stages = track(line, bars)
    assert [(s.stage, s.side) for s in stages] == [("broken", "down"), ("confirmed", "down")]
    assert "its high 101.12 reached the line (101.10 there)" in stages[1].text


def zigzag():
    """Lower highs on a straight line (106, 105, 104 every 10 minutes), lows near 100, then a close above it."""
    rows = []
    for k in range(3):
        top = 106 - k
        for i in range(10):
            mid = 100.5 + (top - 100.5) * (1 - abs(i - 5) / 5)
            rows.append((mid - 0.1, mid + (0.5 if i == 5 else 0.1) - (0.5 if i == 5 else 0), mid - 0.2, mid))
    rows += [(101.0, 102.0, 100.8, 101.9), (101.9, 104.5, 101.8, 104.4)]
    bars = candles(rows)
    return [Bar(b.t, b.o, max(b.h, b.o, b.c), min(b.l, b.o, b.c), b.c, b.v) for b in bars]


def test_auto_lines_find_a_line_through_three_swing_highs():
    bars = zigzag()
    found = [x for x in auto_lines(bars) if x.side == "resistance"]
    assert found and found[0].touches >= 3
    best = found[0]
    assert (best.i1, best.i2) == (5, 25) and best.p1 == pytest.approx(bars[5].h)
    assert best.broken and bars[best.end].c > best.p2  # the last candle closed above it


def test_no_auto_line_when_a_candle_cuts_through():
    bars = zigzag()
    bars[10] = Bar(bars[10].t, bars[10].o, 107.0, bars[10].l, bars[10].c, bars[10].v)  # a wick far above the line
    assert not [x for x in auto_lines(bars) if x.side == "resistance" and x.i1 <= 10 <= x.i2]


# ---------- drawings, charts and alerts on lines ----------

def test_drawings_are_kept_per_symbol(tmp_path):
    store = Drawings(tmp_path / "drawings.json")
    saved = store.save("spy", [{"id": "l1", "kind": "trend", "t1": at(25, 11).timestamp(), "p1": 702, "t2": at(25, 10).timestamp(),
                                "p2": 705, "color": "#ff9800"}], TZ)
    assert saved[0]["t1"] < saved[0]["t2"] and saved[0]["p1"] == 705 and store.find("SPY", "l1")["color"] == "#ff9800"
    with pytest.raises(ValueError, match="colour"):
        store.save("SPY", [{"kind": "hline", "t1": 1, "p1": 700, "color": "red"}], TZ)
    with pytest.raises(ValueError, match="per ticker"):
        store.save("SPY/QQQ", [], TZ)
    kept = store.save("SPY", [{"kind": "trend", "t1": at(25, 10).timestamp(), "p1": 700, "t2": at(25, 11).timestamp(), "p2": 701,
                               "tf": "5"}], TZ)
    assert kept[0]["tf"] == "5m"  # the interval it was drawn on, as the page names it
    with pytest.raises(ValueError, match="interval"):
        store.save("SPY", [{"kind": "hline", "t1": 1, "p1": 700, "tf": "soon"}], TZ)
    store.save("SPY", [], TZ)
    assert store.get("SPY") == []


def test_the_drawings_endpoint_and_the_tracker_on_a_chart(web):  # noqa: F811
    line = {"id": "res", "kind": "hline", "t1": int(at(25, 4).timestamp()), "p1": 708, "color": "#2962ff"}
    r = web.post_ok("/api/drawings", {"symbol": "spy", "drawings": [line]})
    assert r.status_code == 200 and r.json()["drawings"][0]["id"] == "res"
    assert web.get("/api/drawings?symbol=SPY").json()["drawings"][0]["p1"] == 708
    assert web.post("/api/drawings", json={"symbol": "SPY", "drawings": []}).status_code == 403  # needs the page's token
    assert web.post_ok("/api/drawings", {"symbol": "SPY", "drawings": [line | {"p1": -1}]}).status_code == 400
    chart = web.post_ok("/api/chart-data", {"symbol": "SPY", "tf": "1m", "sessions": 1, "auto_lines": True}).json()
    stages = chart["line_stages"]["res"]
    assert stages and stages[0]["stage"] == "broken" and stages[0]["x"] in {b[0] for b in chart["bars"]}
    assert isinstance(chart["auto_lines"], list)


def line_alert(op="retest_up", line=None, **kw):
    line = line or {"id": "res", "kind": "hline", "t1": int(at(25, 4).timestamp()), "p1": 708}
    base = {"symbols": ["SPY"], "tf": "1m", "trigger": "bar",
            "conditions": [{"left": {"kind": "line", "symbol": "SPY", "line": line}, "op": op}]}
    return al.validate(base | kw, {"Tech": ["SPY"]})


def price_vs_line(op, line):
    return al.validate({"symbols": ["SPY"], "tf": "1m", "trigger": "bar",
                        "conditions": [{"left": {"kind": "price"}, "op": op, "right": {"kind": "line", "symbol": "SPY", "line": line}}]}, {})


def test_line_alerts_are_checked():
    a = line_alert()
    assert a["conditions"][0]["right"] == {"kind": "value", "value": 0.0}
    assert al.describe(a) == "The horizontal line 708.00 breaks up and is retested, holding (1m)"
    cross = price_vs_line("crosses_above", {"id": "t", "kind": "trend", "t1": int(at(25, 10).timestamp()), "p1": 705.05,
                                            "t2": int(at(25, 11).timestamp()), "p2": 701.36})
    assert al.describe(cross) == "Price crosses above the trend line 705.05 to 701.36 (1m)"
    with pytest.raises(ValueError, match="that line's symbol"):
        line_alert(symbols=["QQQ"])
    with pytest.raises(ValueError, match="that line's symbol"):
        line_alert(symbols=[], watchlist="Tech")
    with pytest.raises(ValueError, match="drawn line"):
        al.validate({"symbols": ["SPY"], "conditions": [{"left": {"kind": "price"}, "op": "retest"}]}, {})


def test_a_retest_alert_fires_on_the_confirming_candle_with_what_it_did(tmp_path):
    market = FakeMarket()
    bars, _ = market.minutes("SPY", 1)
    line = line_from_json({"id": "res", "kind": "hline", "t1": int(at(25, 4).timestamp()), "p1": 708}, TZ)
    stages = track(line, [b for b in bars if b.t >= at(25, 4)])
    done = next(s for s in stages if s.stage == "confirmed")
    when = [b for b in bars if b.t >= at(25, 4)][done.index].t
    store = al.AlertStore(tmp_path)
    store.add(line_alert(), NOW)
    before = FakeMarket(when)  # the confirming candle is still forming
    assert al.evaluate(store, before, {}, before.now) == []
    after = FakeMarket(when + timedelta(minutes=1, seconds=25))  # closed, and settled (alerts.SETTLE)
    fired = al.evaluate(store, after, {}, after.now)
    assert len(fired) == 1 and fired[0]["message"].startswith("SPY ") and "confirmed" in fired[0]["message"]
    assert done.text in fired[0]["message"]


def test_a_line_moved_after_the_alert_is_watched_where_it_is_now(tmp_path):
    store = al.AlertStore(tmp_path)
    store.add(price_vs_line("above", {"id": "x", "kind": "hline", "t1": int(at(25, 4).timestamp()), "p1": 1}), NOW)
    m = FakeMarket()
    assert len(al.evaluate(store, m, {}, m.now)) == 1  # price is above the line at 1
    store.save([store.load()[0] | {"fired": {}}])
    moved = {"id": "x", "kind": "hline", "t1": int(at(25, 4).timestamp()), "p1": 5000}
    assert al.evaluate(store, m, {}, m.now, resolve=lambda sym, did: moved if did == "x" else None) == []  # dragged far above


def test_the_24_hour_clock_runs_through_the_night_but_not_the_weekend():
    assert market_clock(at(25, 1), "24h") - market_clock(at(24, 23), "24h") == 2 * 3600
    assert market_clock(at(23, 20), "24h") - market_clock(at(20, 20), "24h") == 24 * 3600  # Friday 8 PM to Monday 8 PM
    assert market_clock(at(21, 12), "24h") == market_clock(at(20, 20), "24h")  # Saturday: still Friday's close
    night = line_from_json({"kind": "trend", "t1": at(24, 22).timestamp(), "p1": 700, "t2": at(24, 23).timestamp(), "p2": 701,
                            "clock": "24h"}, TZ)
    assert night.clock == "24h" and night.price_at(at(25, 2)) == pytest.approx(704.0)


def test_market_holidays_add_nothing():
    # Thanksgiving 2026 (Thursday Nov 26): Wednesday 19:00 to Friday 10:00 is one hour on Wednesday and six on Friday
    assert market_clock(at(27, 10)) - market_clock(at(25, 19)) == 7 * 3600
    assert market_clock(at(26, 12)) == market_clock(at(25, 20))  # the holiday itself stands still
    # the 24-hour market skips the holiday's whole window, Wednesday 20:00 to Thursday 20:00
    assert market_clock(at(26, 23), "24h") - market_clock(at(25, 19), "24h") == 4 * 3600
    assert market_clock(at(26, 3), "24h") == market_clock(at(25, 20), "24h")
    line = Line("h", "trend", at(25, 18), 100.0, at(25, 19), 101.0, clock="24h")
    assert line.price_at(at(26, 21)) == pytest.approx(103.0)  # 19:00-20:00 Wednesday and 20:00-21:00 Thursday: two hours on


def test_a_midnight_candle_is_named_by_its_time_and_a_daily_one_by_its_date():
    line = Line("m", "hline", at(25, 20), 100.0, at(25, 20), 100.0)
    night = [Bar(at(26, 23, 55), 99.5, 99.8, 99.4, 99.7, 1), Bar(datetime(2026, 11, 27, 0, 0, tzinfo=TZ), 99.7, 100.6, 99.7, 100.5, 1)]
    assert "The 00:00 candle closed at 100.50" in track(line, night)[0].text
    days = [Bar(datetime(2026, 11, d, tzinfo=TZ), 99.5, 99.8, 99.4, 99.7 if d == 24 else 100.5, 1) for d in (24, 25)]
    assert "The Nov 25 candle closed at 100.50" in track(line, days)[0].text
