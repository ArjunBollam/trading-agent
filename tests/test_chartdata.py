"""One chart for the Charts tab (paperbot/chartdata.py), and the alerts (paperbot/alerts.py), on made-up market data."""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
import yaml
from starlette.testclient import TestClient

from paperbot import alerts as al
from paperbot import chartdata
from paperbot.charting import Print
from paperbot.config import Config
from paperbot.features import Bar

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 25, 12, 0, tzinfo=TZ)


class FakeMarket:
    """Two sessions of 1-minute candles (4:00 to 20:00) moving in a wave, trades each 10 seconds, daily candles."""

    def __init__(self, now=NOW, shift=0.0):
        self.now, self.shift = now, shift
        self.calls = 0

    def _price(self, t):
        k = (t - datetime(2026, 11, 24, tzinfo=TZ)).total_seconds() / 60
        return 700 + 5 * math.sin(k / 45) + k * 0.002 + self.shift

    def minutes(self, symbol, sessions=1):
        self.calls += 1
        out = []
        for d in (date(2026, 11, 24), date(2026, 11, 25))[-sessions:]:
            t = datetime.combine(d, time(4), tzinfo=TZ)
            while t < datetime.combine(d, time(20), tzinfo=TZ) and t + timedelta(minutes=1) <= self.now:
                o, c = self._price(t), self._price(t + timedelta(minutes=1))
                scale = 2.0 if symbol == "QQQ" else 1.0
                out.append(Bar(t, o * scale, max(o, c) * scale + 0.05, min(o, c) * scale - 0.05, c * scale, 1000.0))
                t += timedelta(minutes=1)
        return out, None

    def daily(self, symbol, years=1):
        start = date(2025, 1, 2)
        out, d, p = [], start, 500.0
        while d < self.now.date():
            if d.weekday() < 5:
                out.append(Bar(datetime.combine(d, time(0), tzinfo=TZ), p, p + 3, p - 3, p + 1, 1e6))
                p += 0.5
            d += timedelta(days=1)
        return out

    def prints(self, symbol, day):
        t = datetime.combine(day, time(9, 30), tzinfo=TZ)
        out = []
        while t < min(datetime.combine(day, time(16), tzinfo=TZ), self.now):
            out.append(Print(t, round(self._price(t), 2), 100.0))
            t += timedelta(seconds=10)
        return out

    def night(self, symbol, sessions=2, minutes=5):
        """1- or 5-minute candles around the clock from Tuesday 20:00 (the 24-hour market)."""
        self.night_minutes = minutes
        out, t, step = [], datetime(2026, 11, 24, 20, tzinfo=TZ), timedelta(minutes=minutes)
        while t + step <= self.now:
            o, c = self._price(t), self._price(t + step)
            out.append(Bar(t, o, max(o, c) + 0.05, min(o, c) - 0.05, c, 500.0))
            t += step
        return out

    def last_price(self, symbol):
        return self._price(self.now), 700.0


@pytest.fixture
def cfg():
    return Config.load("config.yaml")


def build(cfg, market=None, now=NOW, **kw):
    return chartdata.build(market or FakeMarket(now), cfg.scan, TZ, chartdata.ChartRequest.from_json({"symbol": "SPY", **kw}), now)


def test_five_minute_chart_with_indicators_and_drawings(cfg):
    r = build(cfg, tf="5m", sessions=2, studies=[{"id": "r", "type": "rsi"}], overlays={"qqe": True, "smc": True},
              profile=True, tpo=True)
    xs = [b[0] for b in r["bars"]]
    assert xs == sorted(set(xs)) and all(x % 300 == 0 for x in xs) and r["synthetic"] is False
    assert r["bars"][0][7] == int(datetime(2026, 11, 24, 4, tzinfo=TZ).timestamp())
    assert len(r["studies"][0]["outputs"][0]["values"]) == len(r["bars"])
    assert set(r["overlays"]) == {"qqe", "smc"}
    assert len(r["profiles"]) == 2 and len(r["tpo"]) == 2
    assert r["profiles"][-1]["from"] == int(datetime(2026, 11, 25, 4, tzinfo=TZ).timestamp())  # the whole session, with extended hours
    assert r["tpo"][-1]["from"] == int(datetime(2026, 11, 25, 9, 30, tzinfo=TZ).timestamp())  # the TPO: regular hours only
    assert r["bars"][-1][6] == 0  # 11:55 is regular hours


def test_replay_uses_nothing_after_its_moment(cfg):
    """Bar replay: as of 10:00, every candle, indicator value and drawing is what it was at 10:00."""
    full = build(cfg, tf="5m", sessions=2, studies=[{"type": "ema", "inputs": {"length": 20}}], overlays={"qqe": True, "sr": True})
    until = int(datetime(2026, 11, 25, 10, 0, tzinfo=TZ).timestamp())
    then = build(cfg, tf="5m", sessions=2, until=until, studies=[{"type": "ema", "inputs": {"length": 20}}], overlays={"qqe": True, "sr": True})
    assert then["bars"][-1][7] == until - 300 and all(b[7] < until for b in then["bars"])
    n = len(then["bars"])
    assert then["bars"] == full["bars"][:n]
    assert then["studies"][0]["outputs"][0]["values"] == full["studies"][0]["outputs"][0]["values"][:n]
    assert all(q["t"] < until for q in then["overlays"]["qqe"])
    # the same as building from scratch with a clock stopped at 10:00
    stopped = build(cfg, FakeMarket(datetime(2026, 11, 25, 10, 0, tzinfo=TZ)), tf="5m", sessions=2, until=until,
                    studies=[{"type": "ema", "inputs": {"length": 20}}], overlays={"qqe": True, "sr": True})
    assert stopped["bars"] == then["bars"] and stopped["overlays"] == then["overlays"]


def test_charts_off_the_clock_count_steps_not_time(cfg):
    for kind in ("renko", "linebreak", "kagi", "pnf"):
        r = build(cfg, tf="5m", type=kind, box=1.0)
        xs = [b[0] for b in r["bars"]]
        assert r["synthetic"] and xs == [chartdata.SYNTHETIC_ORIGIN + 60 * i for i in range(len(xs))], kind
        assert all(b[7] <= NOW.timestamp() for b in r["bars"])
    assert build(cfg, tf="5m", type="kagi", box=1.0)["columns"]
    assert build(cfg, tf="100T", sessions=1)["synthetic"]


def test_trade_charts_and_the_footprint(cfg):
    secs = build(cfg, tf="30s")
    assert len(secs["bars"]) == 300 and secs["bars"][0][5] == 300  # 9:30 to 12:00 of trades every 10 s: 3 to a candle
    assert any("IEX" in n for n in secs["notes"])
    rng = build(cfg, tf="R0.5")
    assert all(round(b[2] - b[3], 6) <= 0.5 for b in rng["bars"])
    fp = build(cfg, tf="5m", footprint=True, sessions=1)
    assert fp["footprint"] and set(map(int, fp["footprint"])) <= {b[0] for b in fp["bars"]}
    one = next(iter(fp["footprint"].values()))
    assert one["volume"] == sum(s + b for _, s, b in one["rows"]) + one["unsigned"]
    assert one["unsigned"] == 100  # the session's first trade: no earlier price to sign it by
    assert "footprint" in " ".join(build(cfg, tf="4m", footprint=True)["notes"]).lower()


def test_trade_charts_on_a_holiday_show_the_last_day_that_traded(cfg):
    class Holiday(FakeMarket):
        def prints(self, symbol, day):
            return [] if day == date(2026, 11, 26) else super().prints(symbol, day)

    thanksgiving = datetime(2026, 11, 26, 12, 0, tzinfo=TZ)
    r = build(cfg, Holiday(thanksgiving), now=thanksgiving, tf="100T")
    days = {datetime.fromtimestamp(b[7], TZ).date() for b in r["bars"]}
    assert days == {date(2026, 11, 25)}


def test_formula_and_daily_charts(cfg):
    r = build(cfg, symbol="SPY/QQQ", tf="15m")
    assert r["bars"] and all(abs(b[4] - 0.5) < 0.01 for b in r["bars"])
    d = build(cfg, tf="W")
    assert d["bars"] and all(datetime.fromtimestamp(b[7], TZ).weekday() == 0 for b in d["bars"][1:])
    with pytest.raises(ValueError):
        build(cfg, symbol="SPY/QQQ", tf="100T")
    with pytest.raises(ValueError):
        chartdata.ChartRequest.from_json({"symbol": "SPY", "type": "spiral"})


def test_csv_export(cfg):
    r = build(cfg, tf="1h", studies=[{"type": "sma", "inputs": {"length": 3}}])
    text = chartdata.csv_for(r, TZ)
    head, first = text.splitlines()[:2]
    assert head == "time,open,high,low,close,volume,SMA 3" and first.startswith("2026-11-24T04:00:00-05:00,")


# ---------- alerts ----------

def alert(**kw):
    base = {"symbols": ["SPY"], "tf": "1m", "trigger": "bar",
            "conditions": [{"left": {"kind": "price"}, "op": "crosses_above", "right": {"kind": "value", "value": 708}}]}
    return al.validate(base | kw, {"Tech": ["QQQ", "SPY"]})


def test_alerts_are_checked_before_they_are_kept():
    with pytest.raises(ValueError, match="symbol or a watchlist"):
        al.validate({"conditions": []}, {})
    with pytest.raises(ValueError, match="no watchlist"):
        al.validate({"watchlist": "Nope"}, {})
    with pytest.raises(ValueError, match="two numbers"):
        alert(conditions=[{"left": {"kind": "value", "value": 1}, "op": "above", "right": {"kind": "value", "value": 2}}])
    with pytest.raises(ValueError, match="minute, hour or daily"):
        alert(tf="100T")
    a = alert(conditions=[{"left": {"kind": "study", "type": "rsi", "inputs": {"length": 14}}, "op": "below", "right": {"kind": "value", "value": 30}}])
    assert al.describe(a) == "Relative Strength Index 14 is below 30 (1m)"


def crossing_time(market, level):
    """The first minute after 9:30 whose candle closes above `level` after one that didn't."""
    bars, _ = market.minutes("SPY", 1)
    for a, b in zip(bars, bars[1:]):
        if b.t >= datetime(2026, 11, 25, 9, 30, tzinfo=TZ) and a.c <= level < b.c:
            return b.t
    raise AssertionError("no cross")


def test_an_alert_fires_once_per_bar_and_a_once_alert_stops(tmp_path):
    store = al.AlertStore(tmp_path)
    market = FakeMarket()
    at = crossing_time(market, 708)  # 9:40: the fake price climbs from 707.27 at 9:30 to 709.10 at 10:11
    store.add(alert(), NOW)
    store.add(alert(trigger="once", name="one time"), NOW)
    m = FakeMarket(at + timedelta(minutes=1))  # the crossing candle has just finished
    sent = []
    fired = al.evaluate(store, m, {}, m.now, lambda title, msg: sent.append((title, msg)))
    assert len(fired) == 2 and len(sent) == 2 and "crossed above 708" in fired[0]["message"]
    assert al.evaluate(store, m, {}, m.now) == []  # same candle: not again
    alerts = store.load()
    assert alerts[0]["status"] == "active" and alerts[1]["status"] == "triggered"
    assert len(store.events()) == 2


def test_bar_close_waits_for_the_candle_and_alerts_can_expire(tmp_path):
    store = al.AlertStore(tmp_path)
    at = crossing_time(FakeMarket(), 708)
    assert at.minute % 5 == 0  # so the 5-minute candle it's in is still forming a minute later
    store.add(alert(tf="5m", trigger="close"), NOW)
    store.add(alert(expires=(at - timedelta(hours=1)).isoformat()), NOW)
    early = FakeMarket(at + timedelta(minutes=1))
    assert al.evaluate(store, early, {}, early.now) == []  # the 1-minute candle crossed, the 5-minute one hasn't closed
    assert store.load()[1]["status"] == "expired"
    closed = FakeMarket(at + timedelta(minutes=5, seconds=25))  # closed, and settled (alerts.SETTLE)
    fired = al.evaluate(store, closed, {}, closed.now)
    assert len(fired) == 1 and fired[0]["tf"] == "5m"


def test_watchlist_alerts_check_every_symbol(tmp_path):
    store = al.AlertStore(tmp_path)
    store.add(al.validate({"watchlist": "Tech", "tf": "1m", "trigger": "bar",
                           "conditions": [{"left": {"kind": "price"}, "op": "above", "right": {"kind": "value", "value": 1}}]},
                          {"Tech": ["QQQ", "SPY"]}), NOW)
    fired = al.evaluate(store, FakeMarket(), {"Tech": ["QQQ", "SPY"]}, NOW)
    assert sorted(e["symbol"] for e in fired) == ["QQQ", "SPY"]


# ---------- the endpoints ----------

@pytest.fixture
def web(tmp_path):
    from paperbot.web.server import create_app

    base = yaml.safe_load(open("config.yaml").read())
    base["log_dir"] = str(tmp_path / "logs")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(base))
    client = TestClient(create_app(path, token="t0k", market=FakeMarket(datetime.now(TZ))), base_url="http://127.0.0.1")
    client.post_ok = lambda url, body=None: client.post(url, json=body or {}, headers={"X-Paperbot-Token": "t0k"})
    return client


def test_chart_endpoint_and_csv(web):
    r = web.post_ok("/api/chart-data", {"symbol": "SPY", "tf": "15m", "studies": [{"type": "rsi"}]})
    assert r.status_code == 200 and r.json()["tf"] == "15m" and r.json()["studies"][0]["name"] == "RSI 14"
    csv = web.post_ok("/api/chart-data?format=csv", {"symbol": "SPY", "tf": "1h"})
    assert csv.headers["content-type"].startswith("text/csv") and 'filename="SPY_1h_candles.csv"' in csv.headers["content-disposition"]
    assert web.post_ok("/api/chart-data", {"symbol": "SPY", "tf": "7x"}).status_code == 400
    assert web.post("/api/chart-data", json={"symbol": "SPY"}).status_code == 403  # needs the page's token
    assert len(web.get("/api/studies").json()["studies"]) >= 40


def test_watchlists_and_alerts_endpoints(web):
    w = web.get("/api/watchlists").json()
    assert w["lists"][0]["symbols"] == ["SPY"]
    saved = web.post_ok("/api/watchlists", {"lists": [{"name": "Tech", "symbols": ["nvda", "aapl"]}, {"name": "ETFs", "symbols": ["SPY"]}],
                                            "current": "ETFs"}).json()
    assert saved["lists"][0]["symbols"] == ["NVDA", "AAPL"] and saved["current"] == "ETFs"
    assert web.post_ok("/api/watchlists", {"lists": [{"name": "A"}, {"name": "A"}]}).status_code == 400
    made = web.post_ok("/api/alerts", {"watchlist": "Tech", "tf": "5m", "conditions": [
        {"left": {"kind": "price"}, "op": "crosses_below", "right": {"kind": "study", "type": "vwap"}}]}).json()
    a = made["alerts"][0]
    assert a["description"] == "Price crosses below VWAP (5m)"
    paused = web.post_ok(f"/api/alerts/{a['id']}", {"action": "pause"}).json()["alerts"][0]
    assert paused["status"] == "paused"
    assert web.post_ok(f"/api/alerts/{a['id']}", {"action": "delete"}).json()["alerts"] == []
    assert web.post_ok("/api/alerts/nope", {"action": "pause"}).status_code == 404
    q = web.post_ok("/api/quotes", {"symbols": ["spy"]}).json()["quotes"][0]
    assert q["symbol"] == "SPY" and q["prev"] == 700.0


def test_the_24_hour_session_charts_the_night_too(cfg):
    r = build(cfg, tf="15m", session="24h")
    hours = {datetime.fromtimestamp(b[7], TZ).hour for b in r["bars"]}
    assert {20, 23, 0, 2} <= hours and r["session"] == "24h" and "24-hour" in r["source"]
    assert all(b[7] % 900 == 0 for b in r["bars"])  # lined up from midnight: :00, :15, :30, :45
    one = FakeMarket()
    r1 = build(cfg, one, tf="1m", session="24h")
    assert one.night_minutes == 1 and "1-minute" in r1["source"]  # 1-minute candles for scalping, around the clock
    assert {datetime.fromtimestamp(b[7], TZ).hour for b in r1["bars"]} >= {21, 2}
    with pytest.raises(ValueError, match="IEX trades"):
        build(cfg, tf="100T", session="24h")
    old = chartdata.ChartRequest.from_json({"symbol": "SPY", "ext": False})
    assert old.session == "rth"  # a page from before the session picker


def test_overnight_alerts_use_the_24_hour_candles_and_hours(tmp_path):
    a = al.validate({"symbols": ["SPY"], "tf": "5m", "trigger": "bar", "session": "24h",
                     "conditions": [{"left": {"kind": "price"}, "op": "above", "right": {"kind": "value", "value": 1}}]}, {})
    assert a["session"] == "24h" and al.describe(a).endswith("(5m, around the clock)")
    one = al.validate({"symbols": ["SPY"], "tf": "1m", "session": "24h",
                       "conditions": [{"left": {"kind": "price"}, "op": "above", "right": {"kind": "value", "value": 1}}]}, {})
    assert one["tf"] == "1m" and one["session"] == "24h"
    night = datetime(2026, 11, 25, 23, 30, tzinfo=TZ)  # Wednesday night
    assert not al.checking_hours(night) and al.checking_hours(night, overnight=True)
    assert not al.checking_hours(datetime(2026, 11, 28, 12, tzinfo=TZ), overnight=True)  # Saturday
    assert al.checking_hours(datetime(2026, 11, 29, 21, tzinfo=TZ), overnight=True)  # Sunday 9 PM
    store = al.AlertStore(tmp_path)
    store.add(a, night)
    m = FakeMarket(night)
    fired = al.evaluate(store, m, {}, night)
    assert len(fired) == 1 and fired[0]["symbol"] == "SPY"


def test_chart_signals_come_from_regular_hours_like_the_dashboard(cfg):
    from paperbot import charting as ch
    from paperbot.drawn import overlay_json

    r = build(cfg, tf="1m", session="ext", sessions=2, overlays={"qqe": True, "sr": True})
    regular = [datetime.fromtimestamp(b[7], TZ) for b in r["bars"] if b[6] == 0]
    assert r["overlays"]["qqe"] and all(datetime.fromtimestamp(q["t"], TZ) in regular for q in r["overlays"]["qqe"])
    # the same as working them out on the regular-hours candles alone
    bars = [Bar(datetime.fromtimestamp(b[7], TZ), b[1], b[2], b[3], b[4], b[5]) for b in r["bars"]]
    rth = [b for b in bars if ch.regular(b.t)]
    alone = overlay_json(rth, cfg.scan, lambda k: int(rth[k].t.timestamp()), which={"qqe": True, "sr": True})
    assert [q["t"] for q in r["overlays"]["qqe"]] == [q["t"] for q in alone["qqe"]]
    assert any("regular-hours candles" in n for n in r["notes"])
    # the same on the 24-hour chart; daily charts have no extended hours to leave out
    night = build(cfg, tf="1m", session="24h", overlays={"qqe": True})
    assert any("regular-hours candles" in n for n in night["notes"])
    daily = build(cfg, tf="1D", overlays={"qqe": True})
    assert not any("regular-hours candles" in n for n in daily["notes"])
