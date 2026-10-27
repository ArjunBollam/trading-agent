"""The web page's API (paperbot/web/server.py): reads, changes, and the guards on changes."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml
from starlette.testclient import TestClient

from paperbot import formations
from paperbot.journal import Journal
from paperbot.web.server import create_app

TZ = ZoneInfo("America/New_York")


@pytest.fixture
def web(tmp_path):
    base = yaml.safe_load(Path("config.yaml").read_text())
    base["log_dir"] = str(tmp_path / "logs")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(base))
    app = create_app(path, token="t0k")
    client = TestClient(app, base_url="http://127.0.0.1")
    client.logs = tmp_path / "logs"
    client.post_ok = lambda url, body=None: client.post(url, json=body or {}, headers={"X-Paperbot-Token": "t0k"})
    return client


def test_page_carries_the_token_and_changes_need_it(web):
    page = web.get("/")
    assert page.status_code == 200 and 'PAPERBOT_TOKEN = "t0k"' in page.text
    assert web.post("/api/live-symbols", json={"action": "add", "symbols": ["QQQ"]}).status_code == 403
    assert web.post("/api/live-symbols", json={"action": "add", "symbols": ["QQQ"]},
                    headers={"X-Paperbot-Token": "wrong"}).status_code == 403
    assert web.post_ok("/api/live-symbols", {"action": "add", "symbols": ["qqq"]}).json()["symbols"][-1] == "QQQ"


def test_a_page_from_before_a_restart_can_get_the_new_token(web):
    r = web.get("/api/token")
    assert r.json() == {"token": "t0k"} and "access-control-allow-origin" not in r.headers
    assert r.headers["cache-control"] == "no-store"
    assert web.get("/api/token", headers={"Host": "evil.example"}).status_code == 400


def test_other_hosts_are_refused(web):
    assert web.get("/api/state", headers={"Host": "evil.example"}).status_code == 400


def test_live_symbols_round_trip(web):
    assert web.get("/api/live-symbols").json()["symbols"] == ["SPY"]
    r = web.post_ok("/api/live-symbols", {"action": "set", "symbols": ["NVDA", "TSLA"]}).json()
    assert r["symbols"] == ["NVDA", "TSLA"] and r["by"] == "web"
    assert json.loads((web.logs / "live_symbols.json").read_text())["symbols"] == ["NVDA", "TSLA"]
    assert web.post_ok("/api/live-symbols", {"action": "add", "symbols": ["not a ticker"]}).status_code == 400
    assert web.post_ok("/api/live-symbols", {"action": "reset"}).json()["symbols"] == ["SPY"]


def test_settings_are_validated_and_kept_beside_config_yaml(web):
    s = web.get("/api/settings").json()
    assert s["effective"]["tick_seconds"] == s["base"]["tick_seconds"] and "live_symbols" not in s["fields"]
    assert "description" in s["fields"]["cooldown_minutes"]
    bad = web.post_ok("/api/settings", {"changes": {"tick_seconds": 9999}})
    assert bad.status_code == 422 and "tick_seconds" in bad.json()["error"]
    assert web.post_ok("/api/settings", {"changes": {"live_symbols": ["X"]}}).status_code == 422
    ok = web.post_ok("/api/settings", {"changes": {"tick_seconds": 15, "cooldown_minutes": 20}}).json()
    assert ok["effective"]["tick_seconds"] == 15 and ok["changes"] == {"tick_seconds": 15, "cooldown_minutes": 20}
    assert web.post_ok("/api/settings", {"reset": ["tick_seconds"]}).json()["changes"] == {"cooldown_minutes": 20}
    assert web.post_ok("/api/settings", {"reset": True}).json()["changes"] == {}


def test_patterns_switch_between_alert_log_and_off(web):
    p = web.get("/api/patterns").json()
    state = {r["kind"]: r["state"] for r in p["patterns"]}
    assert state["doji"] == "log" and state["double_top"] == "alert" and len(state) == len(formations.catalog())
    p = web.post_ok("/api/patterns", {"set": {"doji": "alert", "double_top": "off", "hammer": "log"}}).json()
    state = {r["kind"]: r["state"] for r in p["patterns"]}
    assert (state["doji"], state["double_top"], state["hammer"]) == ("alert", "off", "log")
    p = web.post_ok("/api/patterns", {"groups": ["classic"]}).json()
    assert p["on"] == ["classic"] and {r["state"] for r in p["patterns"] if r["group"] == "candlestick"} == {"off"}
    assert web.post_ok("/api/patterns", {"set": {"nope": "alert"}}).status_code == 400


def test_signals_and_journal_come_from_the_journal(web):
    now = datetime.now(TZ)
    clock = [now - timedelta(seconds=30)]
    j = Journal(web.logs, lambda: clock[0])
    j.log("chart_signal", symbol="SPY", kind="double_top", family="double_top", side="sell", level=None, price=768.1,
          bar_time=now.isoformat(), detail="closed below its neckline", held=True, against_position=False, weak=False)
    clock[0] = now
    j.log("live_cross", symbol="SPY", level="VWAP", level_price=767.6, price=767.4, side="sell", against_position=True)
    j.log("alert", title="t", message="m", priority="high")
    sig = web.get("/api/signals").json()["signals"]
    assert [s["label"] for s in sig] == ["LIVE below VWAP", "Double top"] and sig[0]["against"] is True
    jr = web.get("/api/journal?event=alert").json()
    assert jr["counts"] == {"chart_signal": 1, "live_cross": 1, "alert": 1} and len(jr["events"]) == 1
    assert now.date().isoformat() in web.get("/api/dates").json()["dates"]


def test_scanner_state_and_status(web):
    assert web.get("/api/state").json()["status"] == "off"
    web.logs.mkdir(parents=True, exist_ok=True)
    state = {"updated": datetime.now(TZ).isoformat(), "tick_seconds": 10, "bar_minutes": 5,
             "sessions": ["pre", "regular", "post"], "symbols": {"SPY": {"price": 1}}, "holdings": [], "options": []}
    (web.logs / "scan_state.json").write_text(json.dumps(state))
    st = web.get("/api/state").json()
    # live during a session; between sessions idle, with when it resumes
    assert st["status"] in ("running", "idle") and st["symbols"]["SPY"]["price"] == 1
    assert ("resumes" in st) == (st["status"] == "idle")
    ov = web.get("/api/overview").json()
    assert ov["scanner"]["status"] == st["status"] and ov["halts"] == {"manual": None, "all": None, "day": None}
    assert "session" in ov
    state["updated"] = (datetime.now(TZ) - timedelta(hours=1)).isoformat()  # no check-in for an hour: stopped
    (web.logs / "scan_state.json").write_text(json.dumps(state))
    assert web.get("/api/state").json()["status"] == "stale"


def test_kill_switch(web):
    assert web.post_ok("/api/killswitch", {"action": "halt"}).json()["manual"] == "halted from the web page"
    assert (web.logs / "HALT").exists()
    assert web.post_ok("/api/killswitch", {"action": "resume"}).json()["manual"] is None
    assert web.post_ok("/api/killswitch", {"action": "explode"}).status_code == 400


def test_only_listed_commands_run(web, monkeypatch):
    import subprocess

    calls = []

    class Done:
        returncode, stdout, stderr = 0, "ok\n", ""

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd) or Done())
    assert web.post_ok("/api/commands/rm").status_code == 404
    assert web.post_ok("/api/commands/replay?date=2026-10-26;rm").status_code == 400
    r = web.post_ok("/api/commands/replay?date=2026-10-26").json()
    assert r["code"] == 0 and calls[-1][-3:] == ["replay", "--date", "2026-10-26"]
    assert {c["name"] for c in web.get("/api/commands").json()} >= {"status", "cycle", "flatten", "scan"}
