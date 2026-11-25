"""The 1-minute candles behind the page's 1m charts: fetched once a minute, shown only, never in the way of a scan."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from test_qqe import walk
from test_scan import PREV, TODAY, TZ, FakeSource, Recorder, at, breakout_bars

from paperbot.features import Bar
from paperbot.journal import Journal
from paperbot.notify import Alerts
from paperbot.patterns import rsi_and_trend
from paperbot.qqe import qqe
from paperbot.robinhood import RobinhoodMCP
from paperbot.scanner import Scanner


def minute_bars(day, n: int, closes: list[float]) -> list[Bar]:
    start = datetime(day.year, day.month, day.day, 9, 30, tzinfo=TZ)
    return [Bar(start + timedelta(minutes=i), c, c + 0.1, c - 0.1, c, 100) for i, c in zip(range(n), closes, strict=False)]


class MinuteSource(FakeSource):
    def __init__(self, bars, minutes, fail=False):
        super().__init__(bars, [])
        self.minutes, self.fail, self.chart_calls = minutes, fail, []

    def chart_bars(self, symbols, bar_minutes, start, extended=False):
        self.chart_calls.append((tuple(symbols), bar_minutes, start, extended))
        if self.fail:
            raise RuntimeError("no candles")
        return {s: [b for b in self.minutes if b.t >= start] for s in symbols}


def setup(cfg, tmp_path, source, start):
    clock = [start]
    journal = Journal(cfg.log_dir, lambda: clock[0])
    scanner = Scanner(source, cfg, "123", journal, Alerts(Recorder(), cfg.alerts, journal, lambda: clock[0]),
                      lambda: clock[0], state_path=tmp_path / "scan_state.json")
    return scanner, clock, tmp_path / "scan_chart_1m.json"


def test_the_page_gets_one_minute_candles_once_a_minute(cfg, tmp_path):
    minutes = minute_bars(PREV, 390, walk(1, 390)) + minute_bars(TODAY, 60, walk(2, 60))
    source = MinuteSource({"SPY": breakout_bars()}, minutes)
    scanner, clock, path = setup(cfg, tmp_path, source, at(TODAY, 11) + timedelta(seconds=5))  # 10:25:05
    scanner.scan()
    spy = json.loads(path.read_text())["symbols"]["SPY"]
    today = [b for b in minutes if b.t.date() == TODAY]
    assert [b[0] for b in spy["bars"]] == [int(b.t.timestamp()) for b in today[:55]]  # 9:30 to 10:24, today's only
    assert len(source.chart_calls) == 1 and source.chart_calls[0][1] == 1
    assert source.chart_calls[0][2].date() < TODAY  # the first fetch warms up QQE with earlier sessions

    # QQE on the 1-minute candles, like a 1-minute RTH chart
    rth = minutes[:390] + today[:55]
    want = [(s.side, int(rth[s.index].t.timestamp())) for s in qqe([b.c for b in rth]) if rth[s.index].t.date() == TODAY]
    assert [(q["side"], q["t"]) for q in spy["qqe"]] == want

    for px in (100.6, 100.9):  # two checks in the same minute: one fetch, and the candle forming from live prices
        source.prices = {"SPY": px}
        clock[0] += timedelta(seconds=10)
        scanner.tick()
    assert len(source.chart_calls) == 1
    spy = json.loads(path.read_text())["symbols"]["SPY"]
    assert spy["forming"] == [int(datetime(2026, 10, 26, 10, 25, tzinfo=TZ).timestamp()), 100.6, 100.9, 100.6, 100.9]

    clock[0] = at(TODAY, 11) + timedelta(minutes=1, seconds=5)  # 10:26:05: only what is new
    scanner.tick()
    assert len(source.chart_calls) == 2
    assert source.chart_calls[1][2] == datetime(2026, 10, 26, 10, 23, tzinfo=TZ)
    assert len(json.loads(path.read_text())["symbols"]["SPY"]["bars"]) == 56


class MinuteFlow:
    """Stands in for the 1-minute FlowTracker."""

    def __init__(self):
        self.fail = False

    def update(self, symbol, now):
        if self.fail:
            raise RuntimeError("no tape")
        return self

    def closed(self, now):
        from types import SimpleNamespace
        return [SimpleNamespace(t=at(TODAY, 0) + timedelta(minutes=i), delta=10.4 * i, cvd=20.6 * i) for i in range(3)]


def test_rsi_trend_and_order_flow_follow_the_one_minute_candles(cfg, tmp_path):
    minutes = minute_bars(PREV, 390, walk(1, 390)) + minute_bars(TODAY, 60, walk(2, 60))
    scanner, clock, path = setup(cfg, tmp_path, MinuteSource({"SPY": breakout_bars()}, minutes), at(TODAY, 11) + timedelta(seconds=5))
    scanner.minute_flow = MinuteFlow()
    scanner.scan()
    one = json.loads(path.read_text())["symbols"]["SPY"]
    five = json.loads((tmp_path / "scan_state.json").read_text())["symbols"]["SPY"]
    assert (one["rsi"], one["trend"]) == rsi_and_trend([b for b in minutes if b.t < at(TODAY, 11)], cfg.scan)
    assert one["rsi"] is not None and one["rsi"] != five["rsi"]
    start = int(at(TODAY, 0).timestamp())
    assert one["flow"]["bars"] == [[start, 0, 0], [start + 60, 10, 21], [start + 120, 21, 41]]

    scanner.minute_flow.fail = True  # a tape outage: the candles and RSI move on, the last order flow stays
    source_prices = {"SPY": 100.0}
    scanner.source.prices = source_prices
    clock[0] += timedelta(minutes=1)
    scanner.tick()
    later = json.loads(path.read_text())["symbols"]["SPY"]
    assert len(later["bars"]) == len(one["bars"]) + 1 and later["flow"] == one["flow"]
    assert len(list(scanner.journal.events(TODAY, "minute_flow_failed"))) == 1


def test_a_failed_candle_fetch_costs_only_the_one_minute_charts(cfg, tmp_path):
    source = MinuteSource({"SPY": breakout_bars()}, [], fail=True)
    scanner, clock, path = setup(cfg, tmp_path, source, at(TODAY, 11) + timedelta(seconds=5))
    result = scanner.scan()
    assert "breakout" in {f.signal.kind for f in result.found}
    assert "SPY" in json.loads((tmp_path / "scan_state.json").read_text())["symbols"]
    assert json.loads(path.read_text())["symbols"] == {}
    assert len(list(scanner.journal.events(TODAY, "minute_bars_failed"))) == 1
    source.prices = {"SPY": 101.0}
    clock[0] += timedelta(seconds=10)
    scanner.tick()
    assert len(source.chart_calls) == 1  # not again in the same minute


def test_one_minute_scans_need_no_second_set(cfg, tmp_path):
    cfg.scan.bar_minutes = 1
    source = MinuteSource({"SPY": breakout_bars()}, [])
    scanner, _, path = setup(cfg, tmp_path, source, at(TODAY, 11) + timedelta(seconds=5))
    scanner.scan()
    assert source.chart_calls == [] and not path.exists()


def test_chart_bars_reads_a_day_at_a_time_and_skips_weekends(monkeypatch):
    calls = []

    async def call(name, args):
        calls.append((name, args))
        return {"results": []}

    async def with_client(self, work):
        return await work(call)

    class Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 26, 10, 25, tzinfo=tz)  # a Monday

    monkeypatch.setattr(RobinhoodMCP, "_with_client", with_client)
    monkeypatch.setattr("paperbot.robinhood.datetime", Now)
    rh = RobinhoodMCP("http://example.invalid")
    rh.chart_bars(["spy", "QQQ", "NVDA"], 1, datetime(2026, 10, 22, 0, 0, tzinfo=TZ), extended=True)  # from Thursday
    assert {n for n, _ in calls} == {"get_equity_historicals"}
    days = [a["start_time"][:10] for _, a in calls]
    assert days == sorted(days) and set(days) == {"2026-10-22", "2026-10-23", "2026-10-26"}  # no weekend
    assert all(len(a["symbols"]) <= 2 and a["interval"] == "minute" and a["bounds"] == "extended" for _, a in calls[:4])
    assert all("end_time" in a for _, a in calls if not a["start_time"].startswith("2026-10-26"))
    assert all("end_time" not in a for _, a in calls if a["start_time"].startswith("2026-10-26"))


@pytest.mark.parametrize("saved", [True, False])
def test_the_chart_endpoint_serves_the_one_minute_candles(tmp_path, saved):
    from pathlib import Path

    import yaml
    from starlette.testclient import TestClient

    from paperbot.web.server import create_app

    base = yaml.safe_load(Path("config.yaml").read_text())
    base["log_dir"] = str(tmp_path / "logs")
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(base))
    data = {"updated": "x", "bar_minutes": 1, "symbols": {"SPY": {"bars": [], "forming": None, "qqe": []}}}
    if saved:
        (tmp_path / "logs").mkdir()
        (tmp_path / "logs" / "scan_chart_1m.json").write_text(json.dumps(data))
    client = TestClient(create_app(tmp_path / "config.yaml", token="t"), base_url="http://127.0.0.1")
    assert client.get("/api/chart?minutes=1").json()["symbols"] == (data["symbols"] if saved else {})
    assert client.get("/api/chart?minutes=3").status_code == 400
