"""The morning report (outlook.py): its levels and words come from the candles before it, it's made once a day, and the page follows it."""

from __future__ import annotations

import json
import random
import re
import types
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from test_scan import TZ

from paperbot import outlook
from paperbot.features import Bar
from paperbot.outlook import Point, Zone, build, follow, gather, to_json, visits, zones
from paperbot.web import server

DAY = date(2026, 10, 26)  # a Monday
AT = datetime(2026, 10, 26, 11, 0, tzinfo=TZ)


def weekdays(n: int, last: date = DAY) -> list[date]:
    out, d = [], last
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def walk(seed: int = 1, n: int = 60, price: float = 700.0) -> list[Bar]:
    """Half-hour candles around the clock on `n` weekdays, quiet outside regular hours."""
    r, out = random.Random(seed), []
    for d in weekdays(n):
        t = datetime.combine(d, time(0), tzinfo=TZ)
        for k in range(48):
            at = t + timedelta(minutes=30 * k)
            rth = time(9, 30) <= at.time() < time(16)
            o, price = price, price * (1 + r.gauss(0, 0.0022 if rth else 0.0006))
            wick = abs(r.gauss(0, 0.6 if rth else 0.15))
            out.append(Bar(at, o, max(o, price) + wick, min(o, price) - wick, price, 5e6 if rth else 2e4))
    return out


@pytest.mark.parametrize("seed", range(12))
def test_levels_are_on_the_right_side_and_made_of_real_prices(seed):
    bars = walk(seed)
    r = build("SPY", bars, AT)
    before = [b for b in bars if b.t < AT]
    assert r.price == before[-1].c and r.key.high <= r.price
    nearest = [z.low for _, z in r.up]
    assert r.up and min(nearest) > r.price and all(z.high < r.key.low for _, z in r.down)
    ups = [z for _, z in r.up]
    assert len({id(z) for z in ups}) == len(ups) and all(a.high < b.low for a, b in zip(ups, ups[1:], strict=False))
    highs_lows = {round(v, 6) for b in before for v in (b.h, b.l)}
    for z in [r.key, *ups, *[z for _, z in r.down]]:
        for p in z.points:
            if any(w in p.why for w in ("swing", "high", "low", "lowest", "highest")) and "pivot" not in p.why:
                assert round(p.price, 6) in highs_lows, p


def test_the_named_prices_are_what_they_say():
    bars = walk(3)
    found = {p.why.split(" (")[0]: p.price for p in outlook.points([b for b in bars if b.t < AT], AT, 5.0)[0]}
    days = outlook.sessions(bars)
    d, last = [x for x in days if x[0] < DAY][-1]
    mid = (last.h + last.l + last.c) / 3
    night = [b for b in bars if datetime.combine(d, time(16), tzinfo=TZ) <= b.t < datetime.combine(DAY, time(9, 30), tzinfo=TZ)]
    for name, want in (("last session's low", last.l), ("pivot S2", mid - (last.h - last.l)), ("overnight low", min(b.l for b in night))):
        if name in found:
            assert found[name] == pytest.approx(want)
    assert "pivot S2" in found


def test_nothing_after_the_report_is_used():
    bars = walk(5)
    cut = [b for b in bars if b.t < AT]
    assert to_json(build("SPY", bars, AT), bars) == to_json(build("SPY", cut, AT), cut)


def test_the_page_gets_the_words_the_levels_and_the_chart():
    bars = walk(2)
    j = to_json(build("SPY", bars, AT), bars)
    key = f"{j['key']['price']:.2f}"
    assert j["title"] == f"SPY: {key} is the level to hold" and j["id"] == "2026-10-26-1100-SPY"
    assert key in j["bull"]["text"] and key in j["bear"]["text"] and key in j["support"][0]
    assert j["bull"]["levels"][0]["what"] == "First upside target" and all(x["why"] for x in j["bull"]["levels"] + j["bear"]["levels"])
    assert j["facts"][0].startswith(("Opened at", "So far today", "Now"))
    c = j["chart"]
    assert c["bars"][-1][4] == j["price"] and len(c["ema20"]) == len(c["bars"]) - 19 and len(c["sma200"]) == len(c["bars"]) - 199
    starts = [datetime.fromtimestamp(b[0], TZ).hour for b in c["bars"]]
    assert set(starts) == {0, 4, 8, 12, 16, 20}
    assert json.dumps(j) and not re.search(r"nan|None", json.dumps([j["facts"], j["support"], j["outlook"]]))
    assert "Bearish scenario" in outlook.as_text(j)


def test_too_little_history_makes_no_report():
    bars = walk(1, n=15)
    assert build("SPY", bars, AT) is None


def test_zones_join_prices_that_are_close_but_not_without_end():
    pts = [Point(100 + 0.05 * i, 1, f"p{i}") for i in range(60)]  # a ladder: each a step from the next
    made = zones(pts, adr=3.0)
    assert len(made) >= 3 and all(z.high - z.low <= 1.0 + 1e-9 for z in made) and sum(len(z.points) for z in made) == 60
    assert len(zones([Point(100, 1, "a"), Point(101, 1, "b")], adr=3.0)) == 2


def bar(minute: int, o: float, c: float, lo: float | None = None, hi: float | None = None, step: int = 5) -> Bar:
    return Bar(AT + timedelta(minutes=minute), o, hi or max(o, c), lo or min(o, c), c, 1)


def plain_report(key=100.0):
    level = lambda what, v: {"what": what, "near": v, "far": v, "low": v, "high": v, "why": ["x"]}
    return {"at": AT.isoformat(), "key": {"price": key}, "bull": {"levels": [level("First upside target", 101.0), level("Major resistance", 103.0)]},
            "bear": {"levels": [level("First support below", 98.0)]}}


def test_following_a_report():
    r = plain_report()
    assert follow(r, [])["status"] == "waiting"
    dip = [bar(5 * i, 100.5, 100.5, lo=99.5 if i == 2 else None) for i in range(7)]  # a wick below isn't a loss
    assert follow(r, dip)["status"] == "holding" and follow(r, dip)["low"] == 99.5
    lost = [bar(5 * i, 100.5, 100.5) for i in range(5)] + [bar(25, 100.5, 99.8)]
    got = follow(r, lost)
    assert got["status"] == "lost" and got["lost_at"] == (AT + timedelta(minutes=30)).isoformat()
    assert follow(r, lost[:-1])["status"] == "holding"  # the half hour isn't over
    back = lost + [bar(30 + 5 * i, 99.8, 100.2) for i in range(5)] + [bar(55, 100.2, 101.2)]
    got = follow(r, back)
    assert got["status"] == "reclaimed" and got["lost_at"] < got["reclaimed_at"]
    assert got["hit"] == [{"what": "First upside target", "at": (AT + timedelta(minutes=55)).isoformat()}]
    after_hours = [Bar(datetime.combine(DAY, time(16, 30), tzinfo=TZ), 90, 90, 90, 90, 1)]
    assert follow(r, dip + after_hours) == follow(r, dip)


def test_visits_to_a_level():
    closes = [104, 104, 101, 100.2, 101, 103, 104, 100.1, 98, 98, 104, 104, 100.3]
    bars = [Bar(AT + timedelta(minutes=30 * i), c, c + 0.1, c - 0.1, c, 1) for i, c in enumerate(closes)]
    held, lost, now = visits(bars, 100.0, adr=10.0)  # near: 0.6, clear: 1.5
    assert [b.c for b in held] == [100.2] and [b.c for b in lost] == [98] and now


def test_candles_from_the_three_sources_fit_together():
    end = AT + timedelta(minutes=7)
    full30 = [b for b in walk(4) if time(4) <= b.t.time() < time(20)]
    stub = Bar(datetime.combine(DAY, time(10, 30), tzinfo=TZ), 1, 1, 1, 1, 1)  # the late source's last candle, incomplete
    full30 = [b for b in full30 if b.t < stub.t] + [stub]
    five = [Bar(datetime.combine(DAY, time(10, 30), tzinfo=TZ) + timedelta(minutes=5 * i), 700 + i, 701 + i, 699 + i, 700.5 + i, 10) for i in range(9)]
    calls = []
    out = gather(end, lambda minutes, start, stop: full30, lambda start, stop: calls.append((start, stop)) or walk(4), latest=lambda start: five)
    times = [b.t for b in out]
    assert times == sorted(set(times)) and all(b.t + timedelta(minutes=5) <= end for b in out)
    by = {b.t: b for b in out}
    assert by[stub.t].o == 700 and by[stub.t].c == 705.5 and by[stub.t].v == 60  # the 5-minute candles, not the stub
    assert by[datetime.combine(DAY, time(11), tzinfo=TZ)].c == 706.5  # only its closed 5-minute candle
    assert {b.t.hour for b in out} >= {0, 1, 2, 3, 20, 21} and (end - calls[0][0]).days >= 180


def test_yesterdays_outcome_is_written_down_once(tmp_path):
    bars = walk(6)
    d = weekdays(3)[0]
    at = datetime.combine(d, time(11), tzinfo=TZ)
    path = outlook.save(tmp_path, to_json(build("SPY", bars, at), bars), "morning")
    outlook.finish(tmp_path, "QQQ", bars, DAY)
    assert "outcome" not in json.loads(path.read_text())
    outlook.finish(tmp_path, "SPY", bars, DAY)
    done = json.loads(path.read_text())
    assert done["outcome"]["status"] in ("holding", "lost", "reclaimed") and done["kind"] == "morning"
    outlook.finish(tmp_path, "SPY", [], DAY)
    assert json.loads(path.read_text()) == done


# --- once a day, on the page ----------------------------------------------------------------------------


def central(h: int, m: int, day: int = 26) -> datetime:
    return datetime(2026, 10, day, h, m, tzinfo=ZoneInfo("America/Chicago"))


def test_the_report_is_due_once_a_weekday_from_ten_central(cfg):
    assert not server.report_due(cfg, central(9, 59)) and server.report_due(cfg, central(10, 0))
    assert server.report_due(cfg, central(14, 30)) and not server.report_due(cfg, central(15, 1))  # late is fine, after the close isn't
    assert not server.report_due(cfg, central(10, 0, day=25))  # a Sunday
    folder = cfg.log_dir / "reports"
    bars = walk(2)
    j = to_json(build("SPY", bars, AT), bars)
    outlook.save(folder, j, "now")
    assert server.report_due(cfg, central(10, 5))  # one made on request isn't the day's
    outlook.save(folder, j, "morning")
    assert not server.report_due(cfg, central(10, 5)) and server.report_due(cfg, central(10, 5, day=27))
    cfg.daily_report.enabled = False
    assert not server.report_due(cfg, central(10, 5, day=27))


def run_schedule(monkeypatch, cfg, codes, start, steps):
    """Run the schedule's loop with a fake clock; `codes` are what each attempt returns."""
    clock, ran = [start], []
    monkeypatch.setattr(server.Config, "load", classmethod(lambda cls, path: cfg))

    def run(cmd, **kw):
        ran.append((clock[0], cmd[-2:]))
        code = codes[min(len(ran), len(codes)) - 1]
        if code == 0:
            bars = walk(2)
            at = clock[0].astimezone(TZ).replace(second=0)
            outlook.save(cfg.log_dir / "reports", to_json(build("SPY", bars, AT), bars) | {"id": f"{at:%Y-%m-%d-%H%M}-SPY", "day": f"{at:%Y-%m-%d}"}, "morning")
        return types.SimpleNamespace(returncode=code, stdout="SPY: made\n", stderr="")

    def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)
        if clock[0] >= start + timedelta(minutes=steps):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        server.morning_reports(Path("config.yaml"), run=run, sleep=sleep, now=lambda: clock[0])
    return ran


def test_the_schedule_makes_it_once(monkeypatch, cfg):
    ran = run_schedule(monkeypatch, cfg, [0], central(9, 50), 120)
    assert [(t.astimezone(ZoneInfo("America/Chicago")).strftime("%H:%M"), c) for t, c in ran] == [("10:00", ["outlook", "--morning"])]


def test_a_failure_is_tried_again_and_a_holiday_is_not(monkeypatch, cfg):
    ran = run_schedule(monkeypatch, cfg, [1, 1, 0], central(10, 0), 60)
    gaps = [(b[0] - a[0]).total_seconds() for a, b in zip(ran, ran[1:], strict=False)]
    assert len(ran) == 3 and gaps == [300, 300]
    assert len(run_schedule(monkeypatch, cfg, [3], central(10, 0, day=27), 60)) == 1


def test_the_page_lists_shows_and_announces_reports(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    config = tmp_path / "config.yaml"
    config.write_text(open("config.yaml").read())
    web = TestClient(server.create_app(config), base_url="http://127.0.0.1")
    assert web.get("/api/reports").json() == {"reports": []} and web.get("/api/status").json()["report"] is None
    today = datetime.now(TZ).replace(second=0, microsecond=0)
    bars = walk(2)
    j = to_json(build("SPY", bars, AT), bars)
    new = j | {"id": f"{today:%Y-%m-%d}-1100-SPY", "day": f"{today:%Y-%m-%d}", "at": today.isoformat()}
    outlook.save(tmp_path / "logs" / "reports", j, "morning")
    outlook.save(tmp_path / "logs" / "reports", new | {"id": new["id"].replace("1100", "1200")}, "now")
    assert web.get("/api/status").json()["report"] is None  # only a morning report of today is announced
    outlook.save(tmp_path / "logs" / "reports", new, "morning")
    assert web.get("/api/status").json()["report"]["id"] == new["id"]
    listed = web.get("/api/reports").json()["reports"]
    assert [r["kind"] for r in listed] == ["now", "morning", "morning"] and "chart" not in listed[0]
    shown = web.get(f"/api/reports/{j['id']}").json()
    assert shown["chart"]["bars"] and shown["outcome"]["status"] == "waiting"
    assert web.get("/api/reports/..%2f..%2fconfig").status_code == 404 and web.get("/api/reports/nope").status_code == 404
