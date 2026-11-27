"""The 1-minute line keeper (paperbot/linekeeper.py): trend lines kept drawn as the day goes on."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from paperbot.features import Bar
from paperbot.linekeeper import PREFIX, LineKeeper, _parse_id, current_lines, line_id, merge_page_save
from paperbot.trendlines import Drawings

from test_chartdata import FakeMarket, web  # noqa: F401 - the web fixture

TZ = ZoneInfo("America/New_York")
T0 = datetime(2026, 11, 27, 10, 0, tzinfo=TZ)


def path(points: list[tuple[int, float]]) -> list[Bar]:
    """1-minute candles whose closes run straight between (minute, price) points, 0.05 of wick each side."""
    out = []
    for (m1, p1), (m2, p2) in zip(points, points[1:]):
        for m in range(m1, m2):
            c = p1 + (p2 - p1) * (m - m1) / (m2 - m1)
            out.append(Bar(T0 + timedelta(minutes=m), c, c + 0.05, c - 0.05, c, 1000))
    m, c = points[-1]
    out.append(Bar(T0 + timedelta(minutes=m), c, c + 0.05, c - 0.05, c, 1000))
    return out


# a top at 10:05 (103), a lower high at 10:17 (102), then down to 100 at 10:24
FALLING = [(0, 100.0), (5, 103.0), (10, 100.5), (17, 102.0), (24, 100.0)]


def at(m: int, s: int = 30) -> datetime:
    return T0 + timedelta(minutes=m, seconds=s)


def test_the_down_line_runs_from_the_top_of_the_leg_through_the_lower_high():
    fits = current_lines(path(FALLING))
    down = fits["down"]
    assert (down.a.t, round(down.pa, 2), down.b.t, round(down.pb, 2)) == (T0 + timedelta(minutes=5), 103.05,
                                                                           T0 + timedelta(minutes=17), 102.05)
    assert down.touches == 2
    # used up (its line broke), the top can't start another
    assert "down" not in current_lines(path(FALLING), {"down": {int((T0 + timedelta(minutes=5)).timestamp())}})


def test_a_line_already_broken_is_not_drawn():
    broken = FALLING + [(30, 101.5)]  # by 10:30 closes are back over the line (101.0 there)
    assert "down" not in current_lines(path(broken))


def test_lines_start_within_45_minutes_of_their_latest_swing_point():
    long = [(0, 100.0), (5, 103.0), (30, 100.5), (55, 102.0), (60, 100.0)]  # the lower high is 50 minutes after the top
    assert "down" not in current_lines(path(long))


def test_ids_fit_a_saved_line_and_say_where_they_start():
    did = line_id("down", int(T0.timestamp()), int(T0.timestamp()) + 4000 * 60)
    assert did.startswith(PREFIX) and len(did) <= 24
    assert _parse_id(did) == ("down", int(T0.timestamp()))
    assert _parse_id("ac6c86ad") == (None, 0)


def test_the_keeper_draws_redraws_and_leaves_a_broken_line_as_it_was(tmp_path):
    k, store = LineKeeper(tmp_path), Drawings(tmp_path / "drawings.json")
    store.save("SPY", [{"id": "mine", "kind": "hline", "t1": T0.timestamp(), "p1": 99.0}], TZ)
    said = k.run("SPY", path(FALLING), TZ, at(25))
    assert said == ["SPY: drew 1m down line from 10:05 (2 touches)"]
    lines = store.get("SPY")
    assert lines[0]["id"] == "mine"  # your own lines stay as they were
    down = next(d for d in lines if d["id"].startswith(PREFIX + "d"))
    assert (down["p1"], down["p2"], down["clock"], down["tf"], down["color"]) == (103.05, 102.05, "24h", "1m", "#e040fb")
    assert k.run("SPY", path(FALLING), TZ, at(26)) == []  # nothing new: nothing changes
    # a new lower high at 10:29 (101.05, right on the line's way down): redrawn through it, same id, a third touch
    more = FALLING + [(29, 101.0), (34, 99.5)]
    said = k.run("SPY", path(more), TZ, at(35))
    again = next(d for d in store.get("SPY") if d["id"] == down["id"])
    assert said == ["SPY: redrew 1m down line from 10:05 (3 touches)"] and again["t2"] == int((T0 + timedelta(minutes=29)).timestamp())
    # closes back over it: it stays as it was (its break stays on the chart), and 10:05 can't start another line
    broke = more + [(40, 102.5)]
    k.run("SPY", path(broke), TZ, at(41))
    assert next(d for d in store.get("SPY") if d["id"] == down["id"]) == again
    assert int((T0 + timedelta(minutes=5)).timestamp()) in k.load()["SPY"]["used"]["down"]


def test_a_line_you_delete_or_move_is_yours(tmp_path):
    k, store = LineKeeper(tmp_path), Drawings(tmp_path / "drawings.json")
    k.run("SPY", path(FALLING), TZ, at(25))
    drawn = store.get("SPY")
    # moved: left where you put it, even when a new lower high would redraw it
    moved = [d | {"p1": 103.5} for d in drawn]
    store.save("SPY", moved, TZ)
    more = FALLING + [(29, 101.0), (34, 99.5)]
    k.run("SPY", path(more), TZ, at(35))
    top = int((T0 + timedelta(minutes=5)).timestamp())
    lines = store.get("SPY")
    assert lines[0] == moved[0]  # yours, where you put it
    assert [d["t1"] for d in lines[1:]] == [int((T0 + timedelta(minutes=17)).timestamp())]  # the next line starts later
    assert all(d["t1"] != top for d in lines[1:])
    # deleted: not put back, nor another line from the same top
    store.save("SPY", [], TZ)
    k.run("SPY", path(more), TZ, at(36))
    assert not [d for d in store.get("SPY") if d["id"].startswith(PREFIX + "d")]


def test_a_page_saving_a_stale_copy_doesnt_undo_the_keeper():
    old = {"id": "auto1m-dabc-c", "kind": "trend", "t1": 1, "p1": 103.05, "t2": 2, "p2": 102.05}
    new = old | {"t2": 3, "p2": 101.05}  # redrawn by the keeper since the page loaded
    fresh = {"id": "auto1m-ddef-a", "kind": "trend", "t1": 5, "p1": 99.0, "t2": 6, "p2": 98.0}  # drawn since
    mine = {"id": "you", "kind": "hline", "t1": 1, "p1": 100.0}
    current = [mine, new, fresh]
    # the page posts its own line moved and the keeper's line as it last saw it: the keeper's newest stays
    got = merge_page_save(current, [mine | {"p1": 100.5}, old], {old["id"]: old})
    assert got == [mine | {"p1": 100.5}, new, fresh]
    # you moved the keeper's line: yours
    got = merge_page_save(current, [mine, old | {"p1": 104.0}], {old["id"]: old})
    assert got[1]["p1"] == 104.0
    # you deleted it: gone (the one drawn since you looked stays)
    assert merge_page_save(current, [mine], {old["id"]: old}) == [mine, fresh]
    # an older page that doesn't say what it saw: its list as it is
    assert merge_page_save(current, [mine], None) == [mine]


def test_the_drawings_endpoint_merges_a_stale_page(web):  # noqa: F811
    line = {"id": "auto1m-dabc-c", "kind": "trend", "t1": int(T0.timestamp()), "p1": 103.05,
            "t2": int(T0.timestamp()) + 720, "p2": 102.05, "clock": "24h"}
    web.post_ok("/api/drawings", {"symbol": "SPY", "drawings": [line]})
    stale = dict(line)
    newer = line | {"t2": int(T0.timestamp()) + 1440, "p2": 101.05}
    web.post_ok("/api/drawings", {"symbol": "SPY", "drawings": [newer]})  # the keeper's redraw, as far as the page knows
    r = web.post_ok("/api/drawings", {"symbol": "SPY", "drawings": [stale], "auto_seen": {stale["id"]: stale}})
    assert r.json()["drawings"][0]["p2"] == 101.05
