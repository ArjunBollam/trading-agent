"""`paperbot scan`: chart signals, the read-only Robinhood client, and the scanner's dedupe and context."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from paperbot.config import ScanConfig
from paperbot.features import Bar
from paperbot.journal import Journal
from paperbot.notify import Alerts
from paperbot.patterns import analyze, levels, rsi_series, session_vwaps
from paperbot.robinhood import (
    READ_ONLY_TOOLS,
    Holding,
    Live,
    OptionHolding,
    ReadOnlyViolation,
    Snapshot,
    _call,
    parse_bars,
    parse_holdings,
    parse_option_holdings,
    parse_quotes,
)
from paperbot.scanner import Scanner, format_result, next_scan_start, scan_forever, weekdays_back

TZ = ZoneInfo("America/New_York")
TODAY, PREV = date(2026, 10, 26), date(2026, 10, 23)
SC = ScanConfig()


def at(day: date, i: int) -> datetime:
    """The i-th 5-minute bar of the session."""
    return datetime(day.year, day.month, day.day, 9, 30, tzinfo=TZ) + timedelta(minutes=5 * i)


def prev_session(price: float = 100.0) -> list[Bar]:
    """A quiet prior day: high 101, low 99, close 100."""
    out = []
    for i in range(78):
        c = price + (0.2 if i % 2 else -0.2)
        out.append(Bar(at(PREV, i), price, price + 0.3, price - 0.3, c, 1000))
    out[10] = Bar(at(PREV, 10), price, 101.0, price - 0.3, price, 1000)
    out[20] = Bar(at(PREV, 20), price, price + 0.3, 99.0, price, 1000)
    out[-1] = Bar(at(PREV, 77), price, price + 0.3, price - 0.3, 100.0, 1000)
    return out


def kinds(bars, **kw):
    return {s.kind for s in analyze("T", bars, TODAY, SC, **kw)[0]}


def test_breakout_above_prior_day_high_on_volume():
    today = [Bar(at(TODAY, i), 100.5, 100.8, 100.2, 100.5 + (0.1 if i % 2 else -0.1), 1000) for i in range(10)]
    today.append(Bar(at(TODAY, 10), 100.6, 101.6, 100.5, 101.5, 3000))
    sigs, _ = analyze("T", prev_session() + today, TODAY, SC)
    b = [s for s in sigs if s.kind == "breakout"]
    assert b and b[0].side == "buy" and b[0].level == "prior-day high"
    assert "3.0x volume" in b[0].detail


def test_support_test_that_held():
    today = [Bar(at(TODAY, i), 99.8, 99.95, 99.7, 99.8, 1000) for i in range(8)]
    today.append(Bar(at(TODAY, 8), 99.8, 99.85, 99.55, 99.6, 1000))
    today.append(Bar(at(TODAY, 9), 99.6, 99.65, 99.05, 99.4, 1000))
    sigs, _ = analyze("T", prev_session() + today, TODAY, SC)
    s = [x for x in sigs if x.kind == "support_test"]
    assert s and s[0].side == "buy" and s[0].level in ("prior-day low", "pivot S1")
    assert "breakdown" not in {x.kind for x in sigs}


def test_vwap_reclaim():
    today = [Bar(at(TODAY, 0), 100.0, 100.2, 99.8, 100.0, 5000)]
    today += [Bar(at(TODAY, i), 99.6, 99.7, 99.4, 99.5, 1000) for i in range(1, 6)]
    today.append(Bar(at(TODAY, 6), 99.5, 100.4, 99.5, 100.3, 1000))
    assert "vwap_reclaim" in kinds(prev_session() + today)


def test_rsi_oversold_fires_once_as_an_event():
    today, price = [], 100.0
    for i in range(30):
        price -= 0.25
        today.append(Bar(at(TODAY, i), price + 0.25, price + 0.3, price - 0.05, price, 1000))
    bars = prev_session() + today
    hits = [n for n in range(len(prev_session()) + 2, len(bars) + 1) if "rsi_oversold" in kinds(bars[:n])]
    assert len(hits) == 1
    r = rsi_series([b.c for b in bars[:hits[0]]])
    assert r[-2] >= 30 > r[-1]


def test_no_signals_before_todays_first_bar():
    sigs, summary = analyze("T", prev_session(), TODAY, SC)
    assert sigs == [] and summary.price == 100.0


def test_daily_golden_cross():
    start = datetime(2026, 1, 1, tzinfo=TZ)
    closes = [100.0] * 150 + [90.0] * 49 + [80.0]  # the 50-day sits below the 200-day...
    daily = [Bar(start + timedelta(days=i), c, c, c, c, 1) for i, c in enumerate(closes)]
    quiet = [Bar(at(TODAY, 0), 100, 100.5, 99.5, 100, 1000)]
    assert "golden_cross" not in kinds(prev_session() + quiet, daily=daily)
    today = quiet + [Bar(at(TODAY, 1), 100, 700, 100, 700, 1000)]  # ...until today's price lifts it (> ~597)
    assert "golden_cross" in kinds(prev_session() + today, daily=daily)


def test_read_only_tools_cannot_trade():
    assert {"get_option_positions", "get_option_instruments"} <= READ_ONLY_TOOLS
    for name in READ_ONLY_TOOLS:
        assert name.startswith("get_"), name

    class Client:
        called = False

        async def call_tool(self, name, args):
            Client.called = True

    with pytest.raises(ReadOnlyViolation):
        asyncio.run(_call(Client(), "place_equity_order", {"symbol": "NVDA"}))
    assert not Client.called


def test_parse_robinhood_payloads():
    holdings = parse_holdings([
        {"symbol": "NVDA", "quantity": "-42.000000", "average_buy_price": "216.36", "type": "short"},
        {"symbol": "NFLX", "quantity": "10.000000", "average_buy_price": "64.36", "type": "long"},
        {"symbol": "OLD", "quantity": "0.000000", "type": "long"},
    ])
    assert holdings == [Holding("NVDA", -42.0, 216.36), Holding("NFLX", 10.0, 64.36)]
    bars = parse_bars([{"symbol": "SPY", "bars": [
        {"begins_at": "2026-10-26T13:35:00Z", "open_price": "2", "high_price": "3", "low_price": "1",
         "close_price": "2.5", "volume": 10},
        {"begins_at": "2026-10-26T13:30:00Z", "open_price": "1", "high_price": "2", "low_price": "1",
         "close_price": "2", "volume": 5},
        {"begins_at": "2026-10-26T13:40:00Z", "open_price": "2", "high_price": "2", "low_price": "2",
         "close_price": "2", "volume": 0, "interpolated": True},
    ]}])
    assert [b.c for b in bars["SPY"]] == [2.0, 2.5]


class FakeSource:
    def __init__(self, bars, holdings, options=()):
        self.bars, self.holdings, self.options, self.calls = bars, holdings, list(options), []
        self.prices, self.marks, self.live_calls = {}, {}, 0

    def snapshot(self, account, symbols, bar_minutes, intraday_start, daily_start, extended=False):
        self.calls.append(daily_start)
        self.extended = extended
        return Snapshot(self.holdings, self.bars, {}, self.options)

    def live(self, account, symbols, positions=None):
        self.live_calls += 1
        self.reused = getattr(self, "reused", 0) + (positions is not None)
        return Live(self.holdings, self.options, dict(self.prices), dict(self.marks))


class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, title, message, priority, tags):
        self.sent.append((title, message, priority))

    def poll_commands(self):
        return []


def breakout_bars():
    today = [Bar(at(TODAY, i), 100.5, 100.8, 100.2, 100.5, 1000) for i in range(10)]
    today.append(Bar(at(TODAY, 10), 100.6, 101.6, 100.5, 101.5, 3000))
    return prev_session() + today


def make_scanner(cfg, source, now, only=None):
    journal = Journal(cfg.log_dir, now)
    rec = Recorder()
    return Scanner(source, cfg, "123", journal, Alerts(rec, cfg.alerts, journal, now), now, only), rec


def test_scanner_flags_signals_against_a_short_and_dedupes(cfg):
    clock = [at(TODAY, 11) + timedelta(seconds=10)]  # the breakout bar has just closed
    now = lambda: clock[0]
    source = FakeSource({"NVDA": breakout_bars()}, [Holding("NVDA", -42, 100.0)])
    scanner, rec = make_scanner(cfg, source, now)
    r = scanner.scan()
    f = next(x for x in r.found if x.signal.kind == "breakout")
    assert f.against and "against your position" in f.text() and "short 42" in f.text()
    assert len(rec.sent) == 1  # one alert per symbol per scan, listing each signal
    title, message, priority = rec.sent[0]
    assert title.endswith(": NVDA") and priority == "high" and "Breakout at 101.50" in message
    assert "NVDA" in format_result(r)

    sent = len(rec.sent)
    assert scanner.scan().found == [] and len(rec.sent) == sent  # same bar, nothing new
    assert source.calls[0] is not None and source.calls[1] is None  # daily bars once a day

    restarted, rec2 = make_scanner(cfg, source, now)  # a restart remembers today's signals
    assert restarted.scan().found == [] and rec2.sent == []


def test_light_volume_breakout_is_weak_and_not_alerted(cfg):
    today = [Bar(at(TODAY, i), 100.5, 100.8, 100.2, 100.5, 1000) for i in range(10)]
    today.append(Bar(at(TODAY, 10), 100.6, 101.6, 100.5, 101.5, 900))
    clock = [at(TODAY, 11) + timedelta(seconds=10)]
    scanner, rec = make_scanner(cfg, FakeSource({"T": prev_session() + today}, []), lambda: clock[0])
    r = scanner.scan()
    b = next(f for f in r.found if f.signal.kind == "breakout")
    assert b.signal.weak and "light volume" in b.text()
    assert all("Breakout" not in m for _, m, _ in rec.sent)


def test_chop_around_vwap_is_not_a_cross():
    """Closes a hair either side of VWAP, inside the ATR filter, aren't reclaims or losses."""
    today = [Bar(at(TODAY, i), 100.0, 100.4, 99.6, 100.0 + (0.01 if i % 2 else -0.01), 1000) for i in range(20)]
    for n in range(3, len(today) + 1):
        assert not {"vwap_reclaim", "vwap_lost"} & kinds(prev_session() + today[:n])


def test_closing_auction_volume_is_not_a_spike():
    today = [Bar(at(TODAY, i), 100.0, 100.3, 99.7, 100.0 + (0.1 if i % 2 else -0.1), 1000) for i in range(76)]
    today.append(Bar(at(TODAY, 76), 100.0, 100.3, 99.9, 100.2, 8000))  # 15:50
    assert "volume_spike_up" not in kinds(prev_session() + today)
    today[-1] = Bar(at(TODAY, 75), 100.0, 100.3, 99.9, 100.2, 8000)  # the same bar at 15:45 is a spike
    assert "volume_spike_up" in kinds(prev_session() + today[:75] + [today[-1]])


def test_scanner_ignores_the_bar_still_forming(cfg):
    clock = [at(TODAY, 10) + timedelta(minutes=2)]  # the breakout bar is only 2 minutes old
    scanner, _ = make_scanner(cfg, FakeSource({"NVDA": breakout_bars()}, []), lambda: clock[0])
    r = scanner.scan()
    assert all(f.signal.kind != "breakout" for f in r.found)


def quiet_after_breakout():
    """The breakout bar at 10:20, then five quiet bars that hold above the broken level."""
    bars = breakout_bars()
    return bars + [Bar(at(TODAY, i), 101.3, 101.4, 101.2, 101.3 + (0.05 if i % 2 else -0.05), 1000) for i in range(11, 16)]


def test_catch_up_finds_signals_from_earlier_bars_once(cfg):
    clock = [at(TODAY, 16) + timedelta(seconds=10)]  # 1h50m after the breakout bar, like a 2-hourly run
    source = FakeSource({"NVDA": quiet_after_breakout()}, [Holding("NVDA", -42, 100.0)])
    last_bar_only, _ = make_scanner(cfg, source, lambda: clock[0])
    assert all(f.signal.kind != "breakout" for f in last_bar_only.scan(send_alerts=False).found)

    scanner, rec = make_scanner(cfg, source, lambda: clock[0])
    found = scanner.scan(catch_up=True).found
    b = next(f for f in found if f.signal.kind == "breakout" and f.signal.level == "prior-day high")
    assert b.signal.bar_time == at(TODAY, 10) and b.text().startswith("10:20 Breakout")
    assert len(rec.sent) == 1 and "10:20 Breakout" in rec.sent[0][1]

    clock[0] += timedelta(hours=2)  # the next scheduled run is a new process: it reads the journal
    again, rec2 = make_scanner(cfg, source, lambda: clock[0])
    assert all(f.signal.kind != "breakout" for f in again.scan(catch_up=True).found) and rec2.sent == []


def test_print_only_scan_does_not_hide_signals_from_the_alerting_run(cfg):
    clock = [at(TODAY, 11) + timedelta(seconds=10)]
    source = FakeSource({"NVDA": breakout_bars()}, [])
    peek, rec = make_scanner(cfg, source, lambda: clock[0])
    assert any(f.signal.kind == "breakout" for f in peek.scan(send_alerts=False).found) and rec.sent == []
    real, rec2 = make_scanner(cfg, source, lambda: clock[0])
    assert any(f.signal.kind == "breakout" for f in real.scan(catch_up=True).found)
    assert len(rec2.sent) == 1


def test_long_alerts_keep_the_newest_signals_under_ntfys_limit():
    from paperbot.patterns import ChartSignal
    from paperbot.scanner import Found, alert_text
    group = [Found(ChartSignal("T", "breakout", "buy", 100.0, at(TODAY, i), "x" * 300, "prior-day high"), None)
             for i in range(40)]
    text = alert_text(group)
    assert len(text.encode()) <= 3800 and text.startswith("(+") and "earlier signals" in text
    assert text.endswith(group[-1].text())
    assert alert_text(group[:2]) == group[0].text() + "\n" + group[1].text()


def test_parse_option_positions():
    positions = [
        {"option_id": "a", "chain_symbol": "SPY", "type": "long", "quantity": "2.0000", "average_price": "29.0000",
         "expiration_date": "2026-10-27", "trade_value_multiplier": "100.0000"},
        {"option_id": "b", "chain_symbol": "TSLA", "type": "short", "quantity": "1.0000", "average_price": "-150.0000",
         "expiration_date": "2026-10-30", "trade_value_multiplier": "100.0000"},
        {"option_id": "gone", "chain_symbol": "AAPL", "type": "long", "quantity": "1.0000", "average_price": "40"},
    ]
    instruments = {"a": {"id": "a", "type": "put", "strike_price": "765.0000", "expiration_date": "2026-10-27"},
                   "b": {"id": "b", "type": "call", "strike_price": "410.0000", "expiration_date": "2026-10-30"}}
    put, call = parse_option_holdings(positions, instruments)  # a contract that can't be looked up is skipped
    assert put == OptionHolding("SPY", "put", 765.0, date(2026, 10, 27), 2.0, 0.29, "a")
    assert put.direction == -1 and put.label == "2 SPY 765 puts expiring 10/27"
    assert call.qty == -1 and call.avg_price == 1.5 and call.direction == -1  # a short call gains when it falls
    assert call.label == "1 short TSLA 410 call expiring 10/30"


def test_a_buy_signal_goes_against_a_long_put(cfg):
    clock = [at(TODAY, 11) + timedelta(seconds=10)]
    put = OptionHolding("SPY", "put", 100.0, TODAY, 2, 0.29)
    scanner, rec = make_scanner(cfg, FakeSource({"SPY": breakout_bars()}, [], [put]), lambda: clock[0])
    r = scanner.scan()
    f = next(x for x in r.found if x.signal.kind == "breakout")
    assert f.against and "You hold 2 SPY 100 puts expiring 10/26 (avg 0.29): this signal goes against it." in f.text()
    assert rec.sent[0][2] == "high"
    out = format_result(r)
    assert "options" in out and "option: 2 SPY 100 puts expiring 10/26, avg 0.29" in out


def test_only_scans_the_named_symbols(cfg):
    clock = [at(TODAY, 11) + timedelta(seconds=10)]
    source = FakeSource({"NVDA": breakout_bars(), "SPY": breakout_bars()}, [Holding("NVDA", -42, 100.0)])
    scanner, rec = make_scanner(cfg, source, lambda: clock[0], only={"SPY"})
    r = scanner.scan()
    assert [s.symbol for s in r.summaries] == ["SPY"] and {f.signal.symbol for f in r.found} == {"SPY"}
    assert [t for t, _, _ in rec.sent] and all(t.endswith(": SPY") for t, _, _ in rec.sent)
    assert "NVDA" not in format_result(r)


def test_scans_running_side_by_side_dont_both_alert(cfg):
    clock = [at(TODAY, 10) + timedelta(seconds=10)]  # before the breakout bar closes
    source = FakeSource({"NVDA": breakout_bars()}, [])
    live, _ = make_scanner(cfg, source, lambda: clock[0])
    scheduled, rec_sched = make_scanner(cfg, source, lambda: clock[0])
    live.scan(), scheduled.scan()
    clock[0] = at(TODAY, 11) + timedelta(seconds=10)
    sent_live = {f.signal.key for f in live.scan().found if f.signal.bar_time == at(TODAY, 10)}
    assert ("NVDA", "break", "prior-day high") in sent_live
    # the scheduled scanner read the journal at start, but rereads it after its fetch
    again = {f.signal.key for f in scheduled.scan(catch_up=True).found if f.signal.bar_time == at(TODAY, 10)}
    assert not again & sent_live and not any("10:20 Breakout" in m for _, m, _ in rec_sched.sent)


def test_parse_quotes():
    prices, marks = parse_quotes(
        [{"quote": {"symbol": "SPY", "last_trade_price": "768.71"}}, {"quote": {"symbol": "X", "last_trade_price": None}}],
        [{"quote": {"instrument_id": "a", "mark_price": "0.165"}}])
    assert prices == {"SPY": 768.71} and marks == {"a": 0.165}


def test_live_tick_alerts_a_cross_inside_the_forming_bar_once(cfg):
    """After the 10:15 bar closes at 100.5, the live price runs through the prior-day high (101) at 10:21."""
    bars = breakout_bars()[:-1]  # quiet day so far, closes around 100.5
    put = OptionHolding("SPY", "put", 100.0, TODAY, 2, 0.30, "p1")
    source = FakeSource({"SPY": bars}, [], [put])
    clock = [at(TODAY, 10) + timedelta(seconds=5)]
    scanner, rec = make_scanner(cfg, source, lambda: clock[0])
    scanner.scan()
    sent = len(rec.sent)
    source.prices, source.marks = {"SPY": 100.6}, {"p1": 0.25}
    clock[0] += timedelta(seconds=30)
    status = scanner.tick()
    assert "SPY 100.60" in status and "2 SPY 100 puts expiring 10/26: mark 0.25, -$10 (-17%)" in status
    assert len(rec.sent) == sent  # no level crossed yet

    source.prices, source.marks = {"SPY": 101.4}, {"p1": 0.10}
    clock[0] = at(TODAY, 10) + timedelta(minutes=1, seconds=5)
    out = scanner.tick()
    assert "crossed above the prior-day high 101.00 inside the 10:20 candle; not confirmed until it closes at 10:25" in out
    title, message, priority = rec.sent[-1]
    assert title.startswith("LIVE above") and "prior-day high" in title and priority == "high"
    assert "(mark 0.10, avg 0.30, -$40, -67%): this move goes against it." in message

    n = len(rec.sent)
    source.prices = {"SPY": 101.5}
    clock[0] += timedelta(seconds=30)
    scanner.tick()
    assert len(rec.sent) == n  # still above: nothing new
    source.prices = {"SPY": 100.95}  # back inside the buffer around 101: no cross either way
    clock[0] += timedelta(seconds=30)
    scanner.tick()
    assert len(rec.sent) == n


def test_live_loop_scans_at_bar_closes_and_ticks_in_between(cfg):
    cfg.scan.tick_seconds = 30
    source = FakeSource({"SPY": breakout_bars()[:-1]}, [])
    clock = [at(TODAY, 10) + timedelta(seconds=5)]
    scanner, _ = make_scanner(cfg, source, lambda: clock[0])
    source.prices = {"SPY": 100.5}
    waits, printed = [], []

    def sleep(seconds):
        waits.append(seconds)
        if len(waits) >= 12:
            raise KeyboardInterrupt
        clock[0] += timedelta(seconds=seconds)

    with pytest.raises(KeyboardInterrupt):
        scan_forever(scanner, print_fn=printed.append, sleep=sleep)
    scans = [p for p in printed if p.startswith("Scan at")]
    assert [p[8:24] for p in scans] == ["2026-10-26 10:20", "2026-10-26 10:25"]  # each 5 s after a bar closes
    assert source.live_calls == 10  # every 30 s in between: 10:20:35 ... 10:24:35, then 10:25:35
    assert all(w == 30 for w in waits)


def test_live_list_changes_apply_to_a_running_scan(cfg, tmp_path):
    from paperbot.livelist import LiveList

    live = LiveList(tmp_path / "live_symbols.json", ["spy"])
    assert live.symbols() == ["SPY"] and live.info()["source"] == "config"
    assert live.add(["qqq", "SPY"], by="ai") == ["SPY", "QQQ"] and live.info()["by"] == "ai"
    assert live.remove(["SPY"]) == ["QQQ"]
    with pytest.raises(ValueError):
        live.add(["not a ticker"])
    (tmp_path / "live_symbols.json").write_text("{broken")  # a half-written file keeps the last good list
    assert live.symbols() == ["QQQ"]
    assert live.reset() == ["SPY"]

    cfg.scan.tick_seconds = 10
    source = FakeSource({"SPY": breakout_bars()[:-1], "QQQ": breakout_bars()[:-1]}, [])
    clock = [at(TODAY, 10) + timedelta(seconds=5)]
    scanner, _ = make_scanner(cfg, source, lambda: clock[0], only={"SPY"})
    printed, waits = [], []

    def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 2:
            live.add(["QQQ"], by="web")  # added between two checks
        if len(waits) >= 4:
            raise KeyboardInterrupt
        clock[0] += timedelta(seconds=seconds)

    with pytest.raises(KeyboardInterrupt):
        scan_forever(scanner, print_fn=printed.append, sleep=sleep, symbols=live.symbols)
    assert scanner.only == {"SPY", "QQQ"} and any("live symbols: QQQ, SPY" in p for p in printed)
    scans = [p for p in printed if p.startswith("Scan at")]
    assert len(scans) == 2 and "QQQ" in scans[1]  # scanned right away, not at the next bar close


def test_live_checks_reuse_positions_between_refreshes(cfg):
    cfg.scan.live_positions_seconds = 60
    source = FakeSource({"SPY": breakout_bars()[:-1]}, [])
    clock = [at(TODAY, 10) + timedelta(seconds=5)]
    scanner, _ = make_scanner(cfg, source, lambda: clock[0])
    scanner.scan()
    source.prices = {"SPY": 100.5}
    for _ in range(9):  # every 10 s for 90 s: positions come from the scan, then are re-read once at 60 s
        clock[0] += timedelta(seconds=10)
        scanner.tick()
    assert source.live_calls == 9 and source.reused == 8


def test_the_page_gets_the_forming_candle_from_live_prices(cfg, tmp_path):
    import json

    source = FakeSource({"SPY": breakout_bars()[:-1]}, [])
    clock = [at(TODAY, 10) + timedelta(seconds=5)]
    journal = Journal(cfg.log_dir, lambda: clock[0])
    scanner = Scanner(source, cfg, "123", journal, Alerts(Recorder(), cfg.alerts, journal, lambda: clock[0]),
                      lambda: clock[0], state_path=tmp_path / "scan_state.json")
    scanner.scan()
    state = json.loads((tmp_path / "scan_state.json").read_text())
    assert state["symbols"]["SPY"]["forming"] is None and len(state["symbols"]["SPY"]["bars"]) == 10
    for px in (100.6, 100.9, 100.4, 100.7):  # four checks inside the 10:20 candle
        source.prices = {"SPY": px}
        clock[0] += timedelta(seconds=10)
        scanner.tick()
    view = json.loads((tmp_path / "scan_state.json").read_text())["symbols"]["SPY"]
    assert view["forming"] == [int(at(TODAY, 10).timestamp()), 100.6, 100.9, 100.4, 100.7] and view["price"] == 100.7


def bar_at(day: date, hh: int, mm: int, price: float, v: float = 1000, spread: float = 0.3) -> Bar:
    return Bar(datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ), price, price + spread, price - spread, price, v)


def test_extended_hours_leave_the_regular_session_levels_alone():
    prev = prev_session() + [bar_at(PREV, 16, 30, 100, 50, spread=5)]  # an after-hours spike to 105 / 95
    pre = [bar_at(TODAY, 8, 0, 100, 5000, spread=10)]  # a pre-market swing to 110 / 90 on heavy volume
    reg = [Bar(at(TODAY, i), 100, 100.5, 99.5, 100, 1000) for i in range(8)]
    post = [bar_at(TODAY, 16, 0, 120, 100)]
    sup, res = levels(pre + reg, prev, 5)
    lv = {x.name: x.price for x in sup + res}
    assert lv["prior-day high"] == 101 and lv["prior-day low"] == 99 and lv["prior-day close"] == 100
    assert lv["opening-range high"] == 100.5 and lv["high of day"] == 100.5 and lv["low of day"] == 99.5
    vw = session_vwaps(pre + reg + post)
    assert vw[0] is None and vw[-2] == pytest.approx(100) and vw[-1] == pytest.approx(100)  # after hours: the day's VWAP


def test_live_price_is_the_latest_trade_in_or_out_of_hours():
    prices, _ = parse_quotes([
        {"quote": {"symbol": "SOXL", "last_trade_price": "101.09", "venue_last_trade_time": "2026-10-27T19:59:59.95Z",
                   "last_non_reg_trade_price": "99.32", "venue_last_non_reg_trade_time": "2026-10-27T21:54:19.73Z"}},
        {"quote": {"symbol": "SPY", "last_trade_price": "770", "venue_last_trade_time": "2026-10-27T15:00:00Z",
                   "last_non_reg_trade_price": "765", "venue_last_non_reg_trade_time": "2026-10-27T12:00:00Z"}},
        {"quote": {"symbol": "QQQ", "last_trade_price": "600"}},
    ], [])
    assert prices == {"SOXL": 99.32, "SPY": 770.0, "QQQ": 600.0}


def test_bars_are_fetched_in_calls_small_enough_for_the_connector():
    from paperbot.robinhood import symbols_per_call

    monday = datetime(2026, 10, 26, 10, 0, tzinfo=TZ)
    assert weekdays_back(monday, 2) == datetime(2026, 10, 22, 0, 0, tzinfo=TZ)  # Thursday and Friday warm up
    assert symbols_per_call(5 * 192) == 2 and symbols_per_call(5 * 78) == 5 and symbols_per_call(5 * 960) == 1
    assert symbols_per_call(10) == 10


def test_next_scan_start():
    friday_night = datetime(2026, 10, 30, 20, 30, tzinfo=TZ)
    assert next_scan_start(friday_night, ["pre", "regular", "post"]) == datetime(2026, 11, 2, 4, 0, tzinfo=TZ)
    assert next_scan_start(datetime(2026, 10, 26, 17, 0, tzinfo=TZ), ["regular"]) == datetime(2026, 10, 27, 9, 30, tzinfo=TZ)


@pytest.mark.parametrize("sessions, start, scanned, until", [
    (["regular"], (15, 50), ["15:50", "15:55", "16:00"], "Tue 09:30"),  # 16:00 scans the 15:55 candle
    (["pre", "regular", "post"], (19, 50), ["19:50", "19:55", "20:00"], "Tue 04:00"),
])
def test_live_loop_scans_the_last_candle_then_waits_for_the_next_session(cfg, sessions, start, scanned, until):
    cfg.scan.sessions, cfg.scan.tick_seconds = sessions, 0
    bars = [bar_at(TODAY, 4 + i // 12, 5 * (i % 12), 100) for i in range(16 * 12)]  # 4:00 to 19:55
    source = FakeSource({"SPY": bars}, [])
    clock = [datetime(2026, 10, 26, *start, 5, tzinfo=TZ)]
    scanner, _ = make_scanner(cfg, source, lambda: clock[0])
    printed, waits = [], []

    def sleep(seconds):
        waits.append(seconds)
        if len(waits) >= 4:
            raise KeyboardInterrupt
        clock[0] += timedelta(seconds=seconds)

    with pytest.raises(KeyboardInterrupt):
        scan_forever(scanner, print_fn=printed.append, sleep=sleep)
    assert [p[19:24] for p in printed if p.startswith("Scan at")] == scanned
    assert any(f"nothing to scan until {until} ET" in p for p in printed)
    assert source.extended == (sessions != ["regular"])


def test_live_loop_started_between_sessions_shows_the_last_session_without_alerting(cfg):
    bars = [bar_at(TODAY, 4 + i // 12, 5 * (i % 12), 100 + (5 if i == 150 else 0)) for i in range(16 * 12)]
    source = FakeSource({"SPY": bars}, [])
    clock = [datetime(2026, 10, 27, 1, 0, 5, tzinfo=TZ)]  # after midnight: today has no candles yet
    scanner, rec = make_scanner(cfg, source, lambda: clock[0])

    def sleep(seconds):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        scan_forever(scanner, print_fn=lambda _: None, sleep=sleep)
    view = scanner.view["SPY"]
    assert len(view["bars"]) == 16 * 12 and view["bars"][0][6] == 1 and view["bars"][66][6] == 0  # pre-market, then 9:30
    assert rec.sent == [] and "SPY" not in scanner.watch
