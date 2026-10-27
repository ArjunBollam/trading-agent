"""The fast loop, phone commands, and alert delivery."""

from __future__ import annotations

import json
import threading
from datetime import datetime

import httpx
from fakes import CALL, ET, RecordingChannel

from paperbot.app import build_app
from paperbot.notify import NtfyChannel, TelegramChannel
from paperbot.runner import AlreadyRunning, single_instance
from paperbot.watcher import watch_tick


def test_phone_halt(app, channel):
    channel.commands = ["halt"]
    watch_tick(app)
    assert app.killswitch.entries_halted(app.today()) == "halted from phone"
    assert any(t == "Halted" for t, _, _ in channel.sent)


def test_phone_flatten(app, broker, channel):
    broker.add_position(CALL, 1, 2.0)
    channel.commands = ["flatten"]
    watch_tick(app)
    assert broker.closed_all == 1 and broker.pos == {}
    assert (app.cfg.log_dir / "HALT_2026-10-26").exists()


def test_phone_status_and_unknown(app, broker, channel):
    broker.add_position(CALL, 1, 2.0)
    channel.commands = ["status", "buy 100 nvda"]
    watch_tick(app)
    titles = [t for t, _, _ in channel.sent]
    assert "Status" in titles and "Unknown command" in titles
    status = next(m for t, m, _ in channel.sent if t == "Status")
    assert "$100,000" in status and CALL in status
    assert all(r.side != "buy" or r.type == "stop" for r in broker.submitted)  # no buys from the phone


def test_halt_all_stops_everything(app, broker):
    app.killswitch.halt("manual", scope="all")
    assert watch_tick(app) is None and "clock" not in broker.calls


def test_halt_still_manages_exits(app, broker):
    app.killswitch.halt("manual", scope="manual")
    broker.add_position(CALL, 1, 2.0)
    watch_tick(app)
    assert any(o.is_open and o.is_stop for o in broker.orders.values())


def test_daily_summary_once_after_close(app, broker, channel, clock):
    app.journal.log("cycle_end", slot="x")
    app.journal.log("llm_turn", usage={"prompt_tokens": 4000, "completion_tokens": 100})
    app.journal.log("llm_turn", usage={"input_tokens": 500, "output_tokens": 50})
    clock.dt = datetime(2026, 10, 26, 16, 5, tzinfo=ET)
    broker.market_open = False
    watch_tick(app)
    watch_tick(app)
    assert [t for t, _, _ in channel.sent].count("Daily summary") == 1
    summary = next(m for t, m, _ in channel.sent if t == "Daily summary")
    assert "AI tokens: 4,650" in summary and "no closed trades" in summary


def test_no_daily_summary_when_the_market_never_opened(app, broker, channel, clock):
    clock.dt = datetime(2026, 10, 25, 16, 40, tzinfo=ET)  # a Sunday
    app.journal.log("cycle_end", slot="x", halt="market closed")
    broker.market_open = False
    watch_tick(app)
    assert "Daily summary" not in [t for t, _, _ in channel.sent]


def test_alert_failures_never_raise(cfg, broker, clock):
    app = build_app(cfg, broker, None, RecordingChannel(fail=True), now=clock)
    app.alerts.send("x", "y")
    assert app.journal.events(app.today(), "alert_failed")


def test_alerts_dedupe(app, channel, clock):
    app.alerts.send("same", "message")
    app.alerts.send("same", "message")
    clock.advance(minutes=11)
    app.alerts.send("same", "message")
    assert len(channel.sent) == 2


def test_ntfy_send_and_poll(tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={})
        lines = [json.dumps({"event": "open", "id": "a"}), json.dumps({"event": "message", "id": "m1", "message": " HALT "})]
        return httpx.Response(200, text="\n".join(lines))

    ch = NtfyChannel("https://ntfy.sh", "alerts-topic", "control-topic", "tok", tmp_path / "s.json",
                     http=httpx.Client(transport=httpx.MockTransport(handler)))
    ch.send("Entry: buy 1 SPY", "limit @ 2.06", "high", ["chart"])
    post = seen[0]
    assert str(post.url) == "https://ntfy.sh/alerts-topic"
    assert post.headers["Title"] == "Entry: buy 1 SPY" and post.headers["Priority"] == "high"
    assert post.headers["Authorization"] == "Bearer tok"

    assert ch.poll_commands() == ["halt"]
    assert seen[1].url.params["since"] == "10m"
    ch.poll_commands()
    assert seen[2].url.params["since"] == "m1"  # never replays an old command


def test_telegram_ignores_other_chats(tmp_path):
    updates = {"result": [
        {"update_id": 5, "message": {"chat": {"id": 999}, "text": "/flatten"}},
        {"update_id": 6, "message": {"chat": {"id": 42}, "text": "/status"}},
    ]}
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=updates)))
    ch = TelegramChannel("token", "42", tmp_path / "t.json", http=client)
    assert ch.poll_commands() == ["status"]


def test_single_instance_lock(tmp_path):
    with single_instance(tmp_path / "lock"):
        result = {}

        def other():
            try:
                with single_instance(tmp_path / "lock"):
                    result["got"] = True
            except AlreadyRunning:
                result["got"] = False

        t = threading.Thread(target=other)
        t.start()
        t.join()
        assert result["got"] is False
