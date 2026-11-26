"""The signal audit: it finds wrong, missing and vanished signals, asks a second source, and follows what price did."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, time, timedelta

import pytest
from test_overlays import random_bars
from test_scan import PREV, TODAY, TZ, FakeSource, Recorder, at

from paperbot.features import Bar
from paperbot.journal import Journal
from paperbot.notify import Alerts
from paperbot.scanner import Scanner
from paperbot.signal_audit import SECOND_AFTER, Audit, Candles, report


def two_days(seed=3, today=40):
    return [replace(b, t=at(PREV, i) if i < 78 else at(TODAY, i - 78)) for i, b in enumerate(random_bars(seed, 78 + today))]


def to_minutes(bars):
    """Five 1-minute candles that add up to each 5-minute one."""
    out = []
    for b in bars:
        path = [b.o, b.h, b.l, (b.o + b.c) / 2, b.c]
        for k, c in enumerate(path):
            o = path[k - 1] if k else b.o
            out.append(Bar(b.t + timedelta(minutes=k), o, max(o, c), min(o, c), c, b.v / 5))
    return out


class World:
    """A clock, the candles as Robinhood has them, a scanner that writes its state, and an audit beside it."""

    def __init__(self, cfg, tmp_path, seed=3):
        cfg.scan.tick_seconds = 0
        cfg.scan.sessions = ["regular"]
        self.cfg, self.bars = cfg, two_days(seed)
        self.minutes = to_minutes(self.bars)
        self.clock = at(TODAY, 20) + timedelta(seconds=10)
        self.full = list(self.bars)  # the second source's candles
        self.full_minutes = list(self.minutes)
        self.alerts = []
        self.down = False
        now = lambda: self.clock
        source = FakeSource({}, [])
        source.snapshot_bars = source.snapshot
        source.chart_bars = self.fetch
        self.source = source
        self.journal = Journal(cfg.log_dir, now)
        self.scanner = Scanner(source, cfg, "123", self.journal, Alerts(Recorder(), cfg.alerts, self.journal, now), now,
                               state_path=cfg.log_dir / "scan_state.json")
        self.audit = self.new_audit()

    def new_audit(self):
        return Audit(self.cfg, lambda: self.cfg.scan, Candles(self.fetch), Candles(self.fetch_full), None, self.journal,
                     lambda: self.clock, alert=lambda title, text: self.alerts.append((title, text)))

    def fetch(self, symbols, minutes, start, extended=False):
        if self.down:
            raise RuntimeError("no connection")
        rows = self.bars if minutes == 5 else self.minutes
        return {s: [b for b in rows if b.t >= start and b.t <= self.clock] for s in symbols}

    def fetch_full(self, symbols, minutes, start, extended=False):
        rows = self.full if minutes == 5 else self.full_minutes
        return {s: [b for b in rows if b.t >= start and b.t + timedelta(minutes=minutes) <= self.clock - SECOND_AFTER] for s in symbols}

    def scan(self, catch_up=False):
        self.source.bars = {"SPY": [b for b in self.bars if b.t <= self.clock]}
        self.scanner.scan(catch_up=catch_up)

    def run(self, until, scans=True, pause=()):
        """A scan after every candle and a look every minute."""
        while self.clock < until:
            self.clock += timedelta(minutes=1)
            if self.clock.minute % 5 == 0 and scans and not any(a <= self.clock < b for a, b in pause):
                self.scan()
            self.audit.cycle()

    def records(self, **want):
        return [r for r in self.audit.records.values() if all(r[k] == v for k, v in want.items())]


@pytest.fixture
def world(cfg, tmp_path):
    return World(cfg, tmp_path)


def test_a_days_signals_are_all_right(world):
    world.scan(catch_up=True)
    world.run(at(TODAY, 36) + timedelta(seconds=10))
    rows = list(world.audit.records.values())
    assert {r["source"] for r in rows} >= {"scan", "qqe", "sqz", "smc"} and {r["tf"] for r in rows} == {1, 5}
    assert [r for r in rows if r["verdict"] not in ("ok", "waiting")] == [] and world.alerts == []
    assert sum(r["verdict"] == "ok" for r in rows) > 40
    late = [r for r in rows if datetime.fromisoformat(r["time"]) < at(TODAY, 25)]
    assert late and all(r["second"] == "agrees" for r in late)
    assert all(r["result"] in ("right", "wrong", "flat") and "30" in r["after"] for r in late if r["side"])
    saved = json.loads((world.cfg.log_dir / "audit" / f"{TODAY}.json").read_text())
    assert len(saved["records"]) == len(rows) and saved["counts"]["ok"] == sum(r["verdict"] == "ok" for r in rows)
    state = json.loads((world.cfg.log_dir / "audit_state.json").read_text())
    assert "records" not in state and state["total"] == len(rows)
    assert "Audit of" in report(world.audit)


def test_what_price_did_next_is_measured_from_the_signals_close(world):
    world.scan(catch_up=True)
    world.run(at(TODAY, 32) + timedelta(seconds=10))
    r = next(r for r in world.records(source="scan") if r["result"] in ("right", "wrong"))
    closed = datetime.fromisoformat(r["time"]) + timedelta(minutes=5)
    end = next(b.c for b in world.minutes if b.t == closed + timedelta(minutes=29))
    way = 1 if r["side"] == "buy" else -1
    assert r["after"]["30"] == pytest.approx(way * (end - r["price"]) / r["price"] * 100, abs=0.001)
    assert r["after"]["best"] >= r["after"]["30"] >= r["after"]["worst"]
    assert (r["result"] == "right") == (r["after"]["30"] >= 0.02)


def test_a_signal_its_candles_dont_make_is_wrong_and_the_phone_hears_once(world):
    world.scan()
    world.run(world.clock + timedelta(minutes=4))
    b = [x for x in world.bars if x.t + timedelta(minutes=5) <= world.clock][-1]
    world.journal.log("chart_signal", symbol="SPY", kind="breakout", family="break", side="buy", level="prior-day high", price=b.c,
                      bar_time=b.t.isoformat(), detail=f"closed above the prior-day high {b.c + 5:.2f} on 2.0x volume", weak=False)
    world.run(world.clock + timedelta(minutes=3))
    r, = world.records(source="scan", kind="breakout", verdict="wrong")
    assert any("prior-day high was" in f for f in r["faults"])
    assert len(world.alerts) == 1 and "AUDIT: 1 signal to distrust: SPY" == world.alerts[0][0] and "Breakout: wrong" in world.alerts[0][1]
    again = world.new_audit()  # a restart keeps what was found, and doesn't alert it again
    again.cycle()
    assert again.records[r["key"]]["verdict"] == "wrong" and len(world.alerts) == 1


def test_signals_are_missed_while_the_scanner_is_paused_and_found_when_it_catches_up(world):
    world.scan(catch_up=True)
    paused = (at(TODAY, 24), at(TODAY, 34))
    world.run(at(TODAY, 33), pause=[paused])
    missed = world.records(source="scan", verdict="missed")
    assert missed and all(at(TODAY, 22) <= datetime.fromisoformat(r["time"]) <= at(TODAY, 32) for r in missed)
    assert "asleep or offline" in missed[0]["faults"][0]
    world.clock = at(TODAY, 34) + timedelta(seconds=10)
    world.scan(catch_up=at(TODAY, 22))
    world.run(world.clock + timedelta(minutes=3))
    assert world.records(source="scan", verdict="missed") == []
    found = [r for r in world.records(source="scan") if r["key"] in {m["key"] for m in missed}]
    assert len(found) == len(missed) and any((r["late"] or 0) > 600 for r in found)


def test_a_label_that_disappears_or_never_shows_is_reported(world):
    world.scan(catch_up=True)
    world.run(at(TODAY, 30) + timedelta(seconds=10))
    gone = next(r for r in world.records(source="qqe", tf=1, verdict="ok"))
    path = world.cfg.log_dir / "scan_chart_1m.json"

    def drop():
        data = json.loads(path.read_text())
        spy = data["symbols"]["SPY"]
        spy["qqe"] = [q for q in spy["qqe"] if q["t"] != gone["t"]]
        path.write_text(json.dumps(data))

    for _ in range(3):
        world.clock += timedelta(seconds=40)
        drop()
        world.audit.cycle()
    assert world.audit.records[gone["key"]]["verdict"] == "vanished"

    other = World.new_audit(world)  # one that never saw it
    other._path(TODAY).unlink()
    for _ in range(3):
        drop()
        other.cycle()
    assert other.records[gone["key"]]["verdict"] == "missed"


def test_a_label_the_rules_dont_call_for_is_not_right(world):
    world.scan(catch_up=True)
    path = world.cfg.log_dir / "scan_state.json"
    data = json.loads(path.read_text())
    spy = data["symbols"]["SPY"]
    t = next(b[0] for b in spy["bars"] if b[0] not in {q["t"] for q in spy["qqe"]})
    spy["qqe"].append({"t": t, "side": "long", "rsi": 50, "line": 49, "gap": False})
    path.write_text(json.dumps(data))
    world.audit.cycle()
    r, = [r for r in world.records(source="qqe", tf=5) if r["t"] == t]
    assert r["verdict"] == "changed" and "doesn't come out again" in r["faults"][0]


def test_the_second_source_disagrees_when_its_candle_is_different(world):
    world.scan(catch_up=True)
    world.run(at(TODAY, 22) + timedelta(seconds=10))
    r = world.records(source="scan", verdict="ok", second=None)[-1]
    when = datetime.fromisoformat(r["time"])
    i = next(i for i, b in enumerate(world.full) if b.t == when)
    b = world.full[i]
    world.full[i] = Bar(b.t, b.o, b.h + 3, b.l - 3, b.o + (b.o - b.c) * 4, b.v)  # nothing like it
    world.run(at(TODAY, 28) + timedelta(seconds=10))
    got = world.audit.records[r["key"]]
    assert got["verdict"] == "ok" and got["second"] == "differs"
    assert "on Robinhood" in got["second_faults"][0] and "full-market candles it isn't there" in got["second_faults"][-1]
    assert when.strftime("%H:%M") in world.audit.candle_checks["SPY 5m"]["differ"]


def test_a_failed_download_only_waits(world):
    world.scan(catch_up=True)
    world.down = True
    world.audit.cycle()
    assert world.records(verdict="waiting") and not world.records(verdict="wrong") and "candles" in world.audit.errors
    world.down = False
    world.clock += timedelta(minutes=1)
    world.audit.cycle()
    assert not world.audit.errors and world.records(verdict="ok")


def test_an_answer_cut_short_does_not_replace_a_fuller_one():
    bars = [b for b in two_days() if b.t.date() == TODAY]
    cut = [False]
    c = Candles(lambda symbols, minutes, start, extended: {s: bars[:5] if cut[0] else bars for s in symbols})
    now = at(TODAY, 40)
    c.update(["SPY"], 5, 2, True, now)
    cut[0] = True
    c.update(["SPY"], 5, 2, True, now + timedelta(minutes=1))
    assert len(c.get("SPY", 5, 2, True, TODAY)) == len(bars)


def test_the_borrowed_sign_in_is_never_renewed_or_replaced(tmp_path):
    import httpx2

    from paperbot.robinhood import RobinhoodError, RobinhoodMCP

    path = tmp_path / "token.json"
    rh = RobinhoodMCP("https://example.test/mcp", token_path=path, borrow=True)
    with pytest.raises(RobinhoodError, match="not signed in"):
        next(rh._auth().auth_flow(httpx2.Request("GET", rh.url)))
    path.write_text(json.dumps({"tokens": {"access_token": "test-token"}}))
    request = next(rh._auth().auth_flow(httpx2.Request("GET", rh.url)))
    assert request.headers["Authorization"] == "Bearer test-token"
    assert json.loads(path.read_text()) == {"tokens": {"access_token": "test-token"}}


def test_the_page_serves_the_audit(cfg, tmp_path):
    from starlette.testclient import TestClient

    from paperbot.web.server import create_app

    config = tmp_path / "config.yaml"
    config.write_text(open("config.yaml").read())
    logs = tmp_path / "logs"
    (logs / "audit").mkdir(parents=True)
    web = TestClient(create_app(config), base_url="http://127.0.0.1")
    empty = web.get("/api/audit").json()
    assert empty["records"] == [] and empty["status"] == "off"
    (logs / "audit" / "2026-10-01.json").write_text(json.dumps({"day": "2026-10-01", "records": [{"key": "k"}], "counts": {"ok": 1}}))
    old = web.get("/api/audit?date=2026-10-01").json()
    assert old["records"] == [{"key": "k"}] and old["days"] == ["2026-10-01"] and old["status"] == "past"
    stamp = datetime.combine(TODAY, time(12), tzinfo=TZ).isoformat()
    (logs / "audit_state.json").write_text(json.dumps({"updated": stamp, "day": str(TODAY), "total": 7, "counts": {"ok": 7}, "table": []}))
    pill = web.get("/api/status").json()["audit"]
    assert pill["total"] == 7 and pill["status"] in ("stale", "idle") and "table" not in pill
