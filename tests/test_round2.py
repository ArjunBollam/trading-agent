"""Round 2: fill alerts and execution stats, entry filters (delta, open interest, earnings),
early-close handling, the config audit, and a fuzz test of the risk gate."""

from __future__ import annotations

import dataclasses
from datetime import timedelta

from fakes import CALL

from paperbot.performance import execution
from paperbot.watcher import watch_tick


def fill_events(app):
    return app.journal.events(app.today(), "fill")


def test_entry_fill_alert_with_slippage(app, broker, clock, channel):
    watch_tick(app)  # first tick only sets the baseline: nothing from before startup is replayed
    assert app.funnel.enter(CALL, 1, "test", "s1").status == "submitted"
    order = next(o for o in broker.orders.values() if o.is_open and o.side == "buy")
    clock.advance(seconds=30)
    broker.fill_at_limit(order.id)
    watch_tick(app)
    watch_tick(app)
    alerts = [(t, m) for t, m, _ in channel.sent if t.startswith("Filled")]
    assert alerts == [("Filled (entry)", f"Bought 1 {CALL} @ 2.06 (mid was 2.05)")]
    assert fill_events(app)[0]["slippage"] == 0.01


def test_exit_fill_alert_has_trade_pnl(app, broker, clock, channel):
    broker.add_position(CALL, 1, 2.0)
    watch_tick(app)
    clock.advance(seconds=30)
    broker.set_quote(CALL, 3.20, 3.30)  # take profit
    watch_tick(app)
    msg = next(m for t, m, _ in channel.sent if t == "Filled (exit)")
    assert msg.startswith(f"Sold 1 {CALL} @ 3.20") and "Trade P&L +120.00" in msg


def test_execution_stats():
    submitted = [{"kind": "entry", "client_order_id": "a"}, {"kind": "entry", "client_order_id": "b"},
                 {"kind": "exit", "client_order_id": "c"}]
    fills = [{"client_order_id": "a", "side": "buy", "slippage": 0.02},
             {"client_order_id": "c", "side": "sell", "slippage": 0.04}]
    x = execution(submitted, fills)
    assert (x.entries_sent, x.entries_filled, x.fill_rate, x.entry_slippage, x.exit_slippage) == (2, 1, 50.0, 0.02, 0.04)


# ---- entry filters: delta band, open interest, earnings --------------------------------------

from datetime import date, datetime  # noqa: E402

from fakes import ET  # noqa: E402

from paperbot.config import Earnings  # noqa: E402
from paperbot.models import Order, OrderRequest  # noqa: E402
from paperbot.protect import protect  # noqa: E402


def open_stops(broker):
    return [o for o in broker.orders.values() if o.is_open and o.is_stop]

MSFT_CALL = "MSFT261106C00420000"


def entry(symbol=CALL, qty=1, limit=2.06):
    return OrderRequest(symbol, "buy", qty, "limit", "day", "pb-entry-test-1", "buy_to_open", limit_price=limit)


def check(app, req):
    return app.gate.check(req, app.funnel.context(req.symbol, entry=True), kind="entry")


def test_delta_band(app, broker):
    broker.deltas[CALL] = 0.85
    ok, why = check(app, entry())
    assert not ok and "outside 0.30-0.70" in why
    broker.deltas[CALL] = None
    assert check(app, entry()) == (False, "delta unknown")
    broker.deltas[CALL] = 0.45
    assert check(app, entry())[0]


def test_open_interest_floor(app, broker):
    broker.open_interest[CALL] = 50
    ok, why = check(app, entry())
    assert not ok and "open interest 50" in why


def test_no_entries_before_earnings(app, broker, clock):
    broker.set_quote(MSFT_CALL, 2.00, 2.10)
    ok, why = check(app, entry(MSFT_CALL))  # Monday: MSFT reports Wednesday after the close
    assert not ok and "earnings 2026-10-28" in why
    clock.dt = datetime(2026, 10, 29, 10, 40, tzinfo=ET)  # the day after: allowed again
    assert check(app, entry(MSFT_CALL))[0]


def test_positions_are_closed_before_an_earnings_gap(app, broker, clock):
    broker.set_quote(MSFT_CALL, 2.00, 2.10)
    broker.add_position(MSFT_CALL, 1, 2.0)
    clock.dt = datetime(2026, 10, 28, 15, 29, tzinfo=ET)
    assert protect(app, "w1").exits == []
    clock.dt = datetime(2026, 10, 28, 15, 31, tzinfo=ET)
    rep = protect(app, "w2")
    assert [r.status for r in rep.exits] == ["submitted"] and MSFT_CALL not in broker.pos
    assert app.journal.events(app.today(), "order_submitted")[-1]["reason"] == "earnings"


def test_earnings_window_counts_trading_days(app):
    assert Earnings(symbol="AAPL", date=date(2026, 11, 2), time="bmo").last_session_before() == date(2026, 10, 30)
    # AAPL reports Mon Nov 2 after the close: blocked Thu Oct 29 through Mon Nov 2.
    assert app.gate.earnings_block("AAPL", date(2026, 10, 29)) is not None
    assert app.gate.earnings_block("AAPL", date(2026, 10, 28)) is None


# ---- reliability: early closes, broker stop fallback, heartbeat, account checks --------------

from datetime import time  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

from paperbot.heartbeat import Heartbeat  # noqa: E402
from paperbot.models import Account  # noqa: E402
from paperbot.runner import AccountNotReady, check_account  # noqa: E402


def test_early_close_moves_every_cutoff_earlier(app, broker, clock):
    day = date(2026, 11, 27)  # NYSE closes at 13:00 the day after Thanksgiving
    broker.early_close[day] = time(13, 0)
    clock.dt = datetime(2026, 11, 27, 12, 20, tzinfo=ET)
    app.gate.ensure_session(broker, day)
    assert app.gate.effective(time(15, 15), clock()) == time(12, 15)
    assert not app.gate.entries_allowed(clock())  # past the shifted 12:15 cutoff
    assert app.gate.in_trading_window(clock())  # window now ends 12:45
    clock.dt = datetime(2026, 11, 27, 12, 50, tzinfo=ET)
    assert not app.gate.in_trading_window(clock())


def test_early_close_shifts_close_all_at(app, broker, clock, cfg):
    cfg.exits.close_all_at = time(15, 50)
    broker.early_close[date(2026, 11, 27)] = time(13, 0)
    broker.add_position("SPY261204C00580000", 1, 2.0)
    broker.set_quote("SPY261204C00580000", 2.0, 2.1)
    clock.dt = datetime(2026, 11, 27, 12, 51, tzinfo=ET)  # 15:50 shifted 3 hours earlier is 12:50
    rep = protect(app, "w1")
    assert [r.status for r in rep.exits] == ["submitted"]


def test_broker_stop_rejections_fall_back_to_code_stops(app, broker, channel):
    from paperbot.broker import BrokerError

    def reject(req):
        if req.type == "stop":
            raise BrokerError("order type stop is not supported for options")
        raise AssertionError("unexpected order")
    broker.submit_order = reject
    broker.add_position(CALL, 1, 2.0)
    for i in range(4):
        protect(app, f"w{i}")
    assert app.funnel.stops_disabled and "not supported" in app.funnel.stops_disabled
    assert [t for t, _, _ in channel.sent].count("Broker stops unavailable") == 1
    assert "Order error" not in [t for t, _, _ in channel.sent]


def test_heartbeat_pings_ok_at_most_once_a_minute_and_fail_at_once(app, clock):
    hits = []
    client = httpx.Client(transport=httpx.MockTransport(lambda r: hits.append(str(r.url)) or httpx.Response(200)))
    hb = Heartbeat("https://hc-ping.com/abc", app.journal, clock, http=client)
    hb.beat(True)
    hb.beat(True)
    hb.beat(False)
    clock.advance(seconds=61)
    hb.beat(True)
    assert hits == ["https://hc-ping.com/abc", "https://hc-ping.com/abc/fail", "https://hc-ping.com/abc"]
    Heartbeat(None, app.journal, clock).beat(True)  # no URL configured: nothing happens


def test_account_checks(app, broker):
    check_account(app)
    broker.acct = Account("PA1", 1e5, 1e5, 1e5, options_level=1)
    with pytest.raises(AccountNotReady, match="level 2"):
        check_account(app)
    broker.acct = Account("PA1", 1e5, 1e5, 1e5, trading_blocked=True)
    with pytest.raises(AccountNotReady, match="blocked"):
        check_account(app)


# ---- parameter audit ------------------------------------------------------------------------

from paperbot.audit import account_findings, audit, implied  # noqa: E402


def test_implied_numbers(cfg):
    m = implied(cfg, 100_000)
    assert m["per_trade_risk_usd"] == 500 and round(m["max_position_risk_usd"], 6) == 1050  # 2000 x 35% x 1.5
    assert round(m["breakeven_win_rate_pct"], 1) == 36.8
    assert round(m["round_trip_cost_pct_of_premium"], 1) == 6.0  # 10% spread, entry at mid + 0.2 x half-spread


def test_audit_flags_risky_combinations(cfg):
    cfg.exits.broker_stop = False
    cfg.risk.earnings = [e for e in cfg.risk.earnings if e.symbol != "NVDA"]
    cfg.llm.provider = "groq"
    cfg.schedule.decision_interval_minutes = 5
    warns = [f.message for f in audit(cfg, date(2026, 10, 25)) if f.level == "warn"]
    assert any("broker_stop is off" in w for w in warns)
    assert any("NVDA" in w for w in warns)
    assert any("Groq tokens" in w for w in warns)


def test_audit_notes_expired_calendar_entries(cfg):
    infos = [f.message for f in audit(cfg, date(2026, 12, 1)) if f.level == "info"]
    assert any("FOMC" in i and "past" in i for i in infos) and any("MSFT earnings" in i for i in infos)


def test_account_findings(broker):
    broker.acct = Account("PA1", 1e5, 1e5, 1e5, options_level=3)
    broker.account_configuration = lambda: {"no_shorting": False}
    found = account_findings(broker)
    assert found[0].level == "ok" and any("no_shorting" in f.message for f in found)


# ---- runaway protection ---------------------------------------------------------------------


def test_runaway_order_loop_trips_halt_all(app, broker, cfg, channel):
    cfg.risk.max_order_actions_per_10min = 5
    req = entry()
    results = [app.funnel._submit(dataclasses.replace(req, client_order_id=f"pb-entry-loop-{i}"), "entry", "bug")
               for i in range(8)]
    assert [r.status for r in results].count("submitted") == 5
    assert "runaway protection" in app.killswitch.all_halted()
    assert any(t.startswith("Runaway protection") and p == "urgent" for t, _, p in channel.sent)
    assert results[-1].status == "rejected" and len(broker.submitted) == 5


def test_repeated_broker_errors_trip_halt_all(app, broker, cfg):
    from paperbot.broker import BrokerError

    def reject(req):
        raise BrokerError("insufficient buying power")
    broker.submit_order = reject
    for i in range(6):
        app.funnel._submit(dataclasses.replace(entry(), client_order_id=f"pb-entry-err-{i}"), "entry", "x")
    assert "order errors" in app.killswitch.all_halted()


def test_the_window_slides(app, broker, cfg, clock):
    cfg.risk.max_order_actions_per_10min = 5
    for i in range(4):
        app.funnel._submit(dataclasses.replace(entry(), client_order_id=f"pb-entry-a-{i}"), "entry", "x")
    clock.advance(minutes=11)
    for i in range(4):
        app.funnel._submit(dataclasses.replace(entry(), client_order_id=f"pb-entry-b-{i}"), "entry", "x")
    assert app.killswitch.all_halted() is None


def test_fast_decaying_options_are_refused(app, broker):
    broker.thetas[CALL] = -0.50  # 24% of the 2.05 mid per day
    ok, why = check(app, entry())
    assert not ok and "time decay 24.4%/day" in why
    broker.thetas[CALL] = None
    assert check(app, entry()) == (False, "theta unknown")


def test_monte_carlo(clock):
    from paperbot.performance import closed_trades, monte_carlo

    t = clock()
    orders = []
    for i, exit_px in enumerate([2.6, 1.3, 2.4, 1.4, 2.8, 1.3, 2.2, 1.9, 2.5, 1.3, 2.6, 1.4]):
        orders += [fill(CALL, "buy", 1, 2.0, t + timedelta(minutes=i * 10)),
                   fill(CALL, "sell", 1, exit_px, t + timedelta(minutes=i * 10 + 5))]
    trades = closed_trades(orders)
    mc = monte_carlo(trades, threshold=200)
    assert mc.runs == 5000 and 0 < mc.median_max_drawdown <= mc.p95_max_drawdown
    assert 0 <= mc.p_drawdown_over <= 100 and 0 <= mc.p_end_below_start <= 100
    assert monte_carlo(trades[:5], threshold=200) is None  # too few trades


def fill(symbol, side, qty, price, at):
    from paperbot.models import Order

    cid = f"pb-{'entry' if side == 'buy' else 'exit'}-mc-{at:%H%M}-{side}"
    return Order(cid, cid, symbol, side, qty, qty, "limit", "filled", price, None, at, at, price)


# ---- MAE/MFE and implied move ---------------------------------------------------------------


def test_excursions_are_tracked_and_logged_on_close(app, broker):
    broker.add_position(CALL, 1, 2.0)
    protect(app, "w1")
    broker.set_quote(CALL, 1.55, 1.65)  # -20%
    protect(app, "w2")
    broker.set_quote(CALL, 3.20, 3.30)  # +62%: take profit closes it
    protect(app, "w3")
    protect(app, "w4")
    e = app.journal.events(app.today(), "excursion")
    assert len(e) == 1 and e[0]["mae_pct"] == -20.0 and e[0]["mfe_pct"] == 62.5


def test_excursion_summary():
    from paperbot.performance import ClosedTrade, excursion_summary

    t0 = datetime(2026, 10, 26, 10, 0, tzinfo=ET)
    trades = [ClosedTrade(CALL, 1, 2.0, 2.0 + (0.5 if i % 2 else -0.6), t0, t0 + timedelta(minutes=i), "pb-exit-x")
              for i in range(8)]
    ex = [{"symbol": CALL, "mae_pct": -5.0 - i, "mfe_pct": 10.0 + i} for i in range(8)]
    text = excursion_summary(trades, ex)
    assert "winners' worst dip: median" in text and "losers' best rally" in text


def test_implied_move_in_prompt(app, broker):
    from paperbot.prompt import implied_moves
    from paperbot.research import build_snapshot

    snap = build_snapshot(app)
    moves = implied_moves(snap)
    assert moves["SPY"]["implied_pct"] == round(0.18 / 252 ** 0.5 * 100, 2)
    assert moves["SPY"]["range_used"] > 0


def test_same_direction_cap(app, broker):
    broker.add_position("QQQ261030C00500000", 1, 2.0)
    broker.add_position("AAPL261106C00230000", 1, 2.0)
    ok, why = check(app, entry())  # a third call
    assert not ok and "already 2 calls" in why
    broker.set_quote("SPY261030P00580000", 1.90, 2.00)
    assert check(app, entry("SPY261030P00580000", limit=1.96))[0]  # a put is fine


# ---- operations: startup warnings, Friday reminder, rejections, replay, kill switch ---------


def test_startup_config_warnings(app, cfg, channel):
    from paperbot.runner import warn_config

    cfg.risk.earnings = []  # AAPL, MSFT, NVDA now have no earnings dates
    warn_config(app)
    msg = next(m for t, m, _ in channel.sent if t == "Config warnings")
    assert "no upcoming earnings date" in msg and "check-config" in msg


def test_friday_summary_reminds_to_update_dates(app, broker, channel, clock):
    clock.dt = datetime(2026, 10, 30, 16, 5, tzinfo=ET)  # a Friday
    app.journal.log("cycle_end", slot="x")
    broker.market_open = False
    watch_tick(app)
    assert "Weekend check" in next(m for t, m, _ in channel.sent if t == "Daily summary")


def test_rejection_summary_groups_reasons():
    from paperbot.performance import rejection_summary

    rej = [{"kind": "entry", "why": f"spread {x}% > max_spread_pct 10%"} for x in (11.2, 14.0, 19.5)]
    rej.append({"kind": "entry", "why": "delta unknown"})
    text = rejection_summary(rej)
    assert "3 x spread # > max_spread_pct #" in text and "1 x delta unknown" in text


def test_replay_timeline(make_app):
    from fakes import FakeLLM, decision

    from paperbot.graph import run_cycle
    from paperbot.replay import timeline

    app = make_app(FakeLLM(decision({"action": "buy_to_open", "symbol": CALL, "qty": 1, "reason": "trend"})))
    run_cycle(app)
    lines = timeline(app.journal, app.today())
    assert any("AI: test view -> buy_to_open 1" in line for line in lines)
    assert any(line.split()[1] == "order:" for line in lines)


def test_kill_switch_cli_needs_no_keys_or_network(tmp_path, monkeypatch):
    import shutil

    from conftest import ROOT
    from paperbot.__main__ import main

    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    for key in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "GROQ_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    assert main(["--config", str(tmp_path / "config.yaml"), "halt", "--all"]) == 0
    assert (tmp_path / "logs" / "HALT_ALL").exists()
    assert main(["--config", str(tmp_path / "config.yaml"), "resume"]) == 0
    assert not (tmp_path / "logs" / "HALT_ALL").exists()


def test_soft_loss_limit_halves_risk_and_alerts_once(app, broker, channel):
    assert check(app, entry(qty=4))[0]  # $824 premium, $433 padded risk < $500
    broker.set_equity(98_900)  # down $1,100: past the 1% soft limit
    ok, why = check(app, entry(qty=4))
    assert not ok and "halved" in why
    assert check(app, entry(qty=1))[0]  # $108 padded risk < $250
    protect(app, "w1")
    protect(app, "w2")
    assert [t for t, _, _ in channel.sent].count("Soft loss limit") == 1


def test_trailing_stop_moves_in_steps_and_never_trips_runaway_protection(app, broker, cfg, clock):
    cfg.exits.trail_start_pct, cfg.exits.trail_pct, cfg.exits.take_profit_pct = 20, 12, 300
    broker.add_position(CALL, 1, 2.0)
    for i in range(120):  # 60 minutes of 30 s ticks with the price creeping up a cent a tick
        mid = 2.05 + 0.01 * i
        broker.set_quote(CALL, round(mid - 0.02, 2), round(mid + 0.02, 2))
        protect(app, f"w{i}")
        clock.advance(seconds=30)
    assert app.killswitch.all_halted() is None
    stops = [r for r in broker.submitted if r.type == "stop"]
    assert 3 <= len(stops) <= 25  # it trailed, but in steps
    assert open_stops(broker)[0].stop_price > 2.0  # and ended well above entry


def test_stop_that_fills_during_replacement_is_not_replaced(app, broker, monkeypatch):
    broker.add_position(CALL, 1, 2.0)
    protect(app, "w1")
    old = open_stops(broker)[0]
    position = broker.positions()[0]

    def fills_instead(order_id):  # the stop triggers just as paperbot cancels it
        broker._fill(broker.orders[order_id], broker.orders[order_id].stop_price)
    monkeypatch.setattr(broker, "cancel_order", fills_instead)
    res = app.funnel.place_stop(position, "w2", price=1.40, replacing=[old])
    assert res.status == "skipped" and "filled while replacing" in res.detail
    assert [r.type for r in broker.submitted].count("stop") == 1  # no stop placed for a closed position


def test_entry_without_a_quote_says_so(app, broker):
    del broker.quotes[CALL]
    res = app.funnel.enter(CALL, 1, "x", "s1")
    assert res.status == "rejected" and res.detail == "no valid quote"


def test_external_positions_are_flagged_once_and_optionally_left_alone(app, broker, cfg, channel):
    from paperbot.models import Position

    broker.pos[CALL] = Position(CALL, 1, 2.0, 2.05, "us_option")  # no paperbot entry order behind it
    protect(app, "w1")
    protect(app, "w2")
    assert [t for t, _, _ in channel.sent].count("Position not opened by paperbot") == 1
    assert open_stops(broker)  # managed by default
    cfg.risk.manage_external_positions = False
    broker.set_quote(CALL, 3.20, 3.30)  # would be a take-profit if managed
    protect(app, "w3")
    assert CALL in broker.pos


def test_ai_outage_alerts_are_spaced_out(make_app, channel):
    import httpx2
    import anthropic

    from fakes import FakeLLM, decision
    from paperbot.graph import run_cycle

    err = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    app = make_app(FakeLLM(*([err] * 5), decision()))
    for _ in range(6):
        run_cycle(app)
    titles = [t for t, _, _ in channel.sent]
    assert titles.count("Claude call failed") == 2  # the 1st and 4th failure
    assert "Claude is answering again" in titles and app.ai_failures == 0


def test_edge_check(clock):
    from paperbot.performance import ClosedTrade, edge_check

    t0 = clock()
    winners = [ClosedTrade(CALL, 1, 2.0, 2.0 * (1 + r / 100), t0, t0 + timedelta(minutes=i), "pb-exit-x")
               for i, r in enumerate([30, -20, 25, -15, 40, -20, 35, -10, 30, -20] * 4)]
    e = edge_check(winners)
    assert e.sharpe_per_trade > 0 and 50 < e.psr <= 100 and e.min_trades_95 > 1
    losers = [ClosedTrade(CALL, 1, 2.0, 2.0 * (1 + r / 100), t0, t0, "x") for r in [-20, 10, -30, 5, -25, 10, -20, 5, -15, 0]]
    assert edge_check(losers).min_trades_95 is None and edge_check(losers[:5]) is None


def test_preflight_checks(make_app, broker, clock):
    from fakes import FakeLLM, decision

    from paperbot.preflight import run_checks

    app = make_app(FakeLLM(decision()))
    broker.acct = Account("PA1", 1e5, 1e5, 1e5, options_level=3)
    broker.account_configuration = lambda: {"no_shorting": True}
    clock.dt = datetime.now(ET)  # the fake broker's clock follows this
    results = dict((line.split(":")[0], ok) for ok, line in run_checks(app))
    assert results["broker"] and results["account"] and results["clock"] and results["ai"] and results["kill switch"]
    app.killswitch.halt("test", scope="manual")
    assert not dict((line.split(":")[0], ok) for ok, line in run_checks(app))["kill switch"]


# ---- order effects and daylight saving time --------------------------------------------------


def test_candidate_order_rotates_between_cycles():
    from paperbot.models import Contract
    from paperbot.prompt import rotated

    def c(sym, right):
        return Contract(f"{sym}261106{right}00100000", sym, right, 100.0, date(2026, 11, 6), 1.0, 1.1)
    cands = {"SPY": [c("SPY", "C"), c("SPY", "P")], "QQQ": [c("QQQ", "C"), c("QQQ", "P")]}
    a = [x.symbol for x in rotated(cands, datetime(2026, 10, 26, 10, 0, tzinfo=ET), 15)]
    b = [x.symbol for x in rotated(cands, datetime(2026, 10, 26, 10, 15, tzinfo=ET), 15)]
    assert sorted(a) == sorted(b) and a != b
    assert a[0][3:] != b[0][3:] or a[0][:3] != b[0][:3]


def test_market_hours_survive_the_dst_change(app, cfg):
    # US clocks fall back on Sunday 2026-11-01. 09:50 New York time is an entry time on both sides.
    before = datetime(2026, 10, 30, 9, 50, tzinfo=ET)
    after = datetime(2026, 11, 2, 9, 50, tzinfo=ET)
    assert before.utcoffset() != after.utcoffset()
    assert app.gate.entries_allowed(before) and app.gate.entries_allowed(after)
    from datetime import timezone

    assert not app.gate.entries_allowed(datetime(2026, 11, 2, 14, 40, tzinfo=timezone.utc))  # 09:40 EST
    assert app.gate.entries_allowed(datetime(2026, 11, 2, 14, 50, tzinfo=timezone.utc))  # 09:50 EST
    from paperbot.features import session_start

    assert session_start(after, ET).utcoffset().total_seconds() == -5 * 3600


def test_rehearsal_runs_when_closed_and_never_trades(make_app, broker):
    from fakes import FakeLLM, decision

    from paperbot.graph import run_cycle

    broker.market_open = False
    app = make_app(FakeLLM(decision({"action": "buy_to_open", "symbol": CALL, "qty": 1, "reason": "x"})), dry_run=True)
    app.rehearsal = True
    state = run_cycle(app)
    assert state.get("halt") is None and state["results"][0].status == "dry_run"
    assert broker.submitted == []
    assert "REHEARSAL" in app.llm.client.calls[0]["messages"][0]["content"]


def test_tight_spread_waives_the_open_interest_floor(app, broker):
    broker.open_interest[CALL] = 50
    broker.set_quote(CALL, 2.04, 2.08)  # 1.9% spread: liquid despite low OI
    assert check(app, entry(limit=2.06))[0]


def test_daily_token_budget_stops_ai_cycles(make_app, cfg, channel):
    from fakes import FakeLLM

    from paperbot.graph import run_cycle

    cfg.llm.provider = "groq"
    cfg.llm.groq.daily_token_budget = 5000
    app = make_app(FakeLLM())  # an Anthropic-shaped fake; never called here
    app.journal.log("llm_turn", usage={"prompt_tokens": 4800, "completion_tokens": 300})
    state = run_cycle(app)
    assert "token budget" in state["halt"] and app.llm.client.calls == []
    run_cycle(app)
    assert [t for t, _, _ in channel.sent].count("AI token budget used") == 1


def test_prompt_makes_max_qty_a_hard_ceiling(app):
    from paperbot.prompt import cycle_prompt, system_prompt
    from paperbot.research import build_snapshot

    assert "qty must be at most the max_qty shown for that exact contract" in system_prompt(app)
    prompt = cycle_prompt(app, build_snapshot(app))
    assert "a hard ceiling, not a target" in prompt and "must not exceed that contract's max_qty" in prompt


# ---- trading profiles and aggressive scalping ------------------------------------------------


def test_profiles_switch_and_load(tmp_path):
    import shutil

    from conftest import ROOT
    from paperbot.__main__ import PROFILES, compare_profiles
    from paperbot.config import Config

    for f in PROFILES.values():
        assert Config.load(ROOT / f)  # every profile file validates
    agg = Config.load(ROOT / "config.aggressive.yaml")
    assert agg.risk.min_dte == 0 and agg.exits.close_at_dte == -1 and agg.schedule.trigger_cycle_on_signal
    for f in PROFILES.values():
        shutil.copy(ROOT / f, tmp_path / f)
    table = compare_profiles(tmp_path)
    assert "aggressive" in table and "0-1" in table and "15:15" in table


def test_profile_flag_picks_the_profile_file(tmp_path, monkeypatch):
    import shutil

    from conftest import ROOT
    from paperbot.__main__ import main

    for f in ("config.yaml", "config.aggressive.yaml"):
        shutil.copy(ROOT / f, tmp_path / f)
    assert main(["--config", str(tmp_path / "config.yaml"), "--profile", "aggressive", "halt"]) == 0
    assert (tmp_path / "logs" / "HALT").exists()  # same logs folder as the default profile


@pytest.mark.parametrize("patch, msg", [
    ({"exits": {"close_at_dte": -1, "close_all_at": None}}, "needs exits.close_all_at"),
    ({"exits": {"close_all_at": "15:45"}}, "15:30 or earlier"),
    ({"schedule": {"no_new_entries_after": "15:28"}}, "15:25 or earlier"),
])
def test_same_day_expiry_guards(patch, msg):
    import yaml
    from pydantic import ValidationError

    from conftest import ROOT
    from paperbot.config import Config

    raw = yaml.safe_load((ROOT / "config.aggressive.yaml").read_text())
    for section, values in patch.items():
        raw[section].update(values)
    with pytest.raises(ValidationError, match=msg):
        Config.model_validate(raw)


def test_dte_close_off_for_same_day_scalps(cfg):
    from paperbot.exits import exit_reason
    from paperbot.models import Position, Quote

    cfg.exits.close_at_dte = -1
    p = Position("SPY261026C00580000", 1, 2.0, 2.0, "us_option")  # expires today
    assert exit_reason(p, Quote(p.symbol, 2.0, 2.1), date(2026, 10, 26), cfg.exits) is None


def test_setup_alert_triggers_an_extra_cycle(app, broker, cfg, clock):
    from paperbot.watcher import maybe_signals

    cfg.schedule.trigger_cycle_on_signal = True
    broker.stock_bars = lambda symbol, timeframe, limit, start=None: _bars() if symbol == "SPY" else []
    broker.stocks["SPY"] = dataclasses.replace(broker.stocks["SPY"], prev_close=99.0)
    clock.dt = datetime(2026, 10, 26, 10, 30, tzinfo=ET)
    maybe_signals(app)
    assert app.cycle_requested


def test_triggered_cycle_gets_its_own_slot(make_app, clock):
    from fakes import FakeLLM, decision

    from paperbot.graph import run_cycle

    app = make_app(FakeLLM(decision(), decision()))
    assert run_cycle(app)["slot"] == "20261026T1030"  # 15-minute slots
    assert run_cycle(app, triggered=True)["slot"] == "20261026T1030s1040"


def _bars():
    t0 = datetime(2026, 10, 26, 9, 30, tzinfo=ET)
    return [{"t": (t0 + timedelta(minutes=i)).isoformat(), "o": 100 + i * 0.05 - 0.05, "h": 100 + i * 0.05 + 0.02,
             "l": 100 + i * 0.05 - 0.07, "c": 100 + i * 0.05, "v": 1000} for i in range(60)]


# ---- same-cycle flips and the re-entry cooldown ----------------------------------------------

AAPL_PUT, AAPL_CALL = "AAPL261106P00230000", "AAPL261106C00230000"
SELL_PUT = {"action": "sell_to_close", "symbol": AAPL_PUT, "qty": 1, "reason": "reversed"}
BUY_CALL = {"action": "buy_to_open", "symbol": AAPL_CALL, "qty": 1, "reason": "now bullish"}


@pytest.mark.parametrize("order", [[SELL_PUT, BUY_CALL], [BUY_CALL, SELL_PUT]])
def test_same_cycle_flip_is_blocked_by_the_cooldown(make_app, broker, order):
    from fakes import FakeLLM, decision

    from paperbot.graph import run_cycle

    broker.set_quote(AAPL_PUT, 2.00, 2.10)
    broker.set_quote(AAPL_CALL, 2.00, 2.10)
    broker.add_position(AAPL_PUT, 1, 2.0, opened_minutes_ago=40)
    broker.hold_fills = True  # the exit is sent but hasn't filled yet, as in the live flip
    state = run_cycle(make_app(FakeLLM(decision(*order))))
    by_kind = {r.kind: r for r in state["results"]}
    assert by_kind["exit"].status == "submitted"
    assert by_kind["entry"].status == "rejected" and "re-entry cooldown" in by_kind["entry"].detail
    assert [r.side for r in broker.submitted if r.type != "stop"] == ["sell"]


def test_a_resting_protective_stop_does_not_start_the_cooldown(app, broker):
    broker.set_quote(AAPL_PUT, 2.00, 2.10)
    broker.set_quote(AAPL_CALL, 2.00, 2.10)
    broker.add_position(AAPL_PUT, 1, 2.0, opened_minutes_ago=40)
    protect(app, "w1")
    assert open_stops(broker)
    assert check(app, entry(AAPL_CALL))[0]


def test_stale_strong_open_neither_alerts_nor_triggers_a_cycle(app, broker, cfg, clock, channel):
    from paperbot.watcher import maybe_signals

    cfg.schedule.trigger_cycle_on_signal = True
    rows = _bars()  # rising all morning: a strong open, but no fresh opening-range break later
    broker.stock_bars = lambda symbol, timeframe, limit, start=None: rows if symbol == "SPY" else []
    broker.stocks["SPY"] = dataclasses.replace(broker.stocks["SPY"], prev_close=99.0)
    app.journal.log("signal", symbol="SPY", kind="orb_up", message="already sent at 09:50")
    clock.dt = datetime(2026, 10, 26, 11, 36, tzinfo=ET)
    maybe_signals(app)
    assert not any(t == "Setup: SPY" for t, _, _ in channel.sent)
    assert app.cycle_requested is False


# ---- ET times in gate messages, and fill alerts reported once ---------------------------------


def test_cooldown_message_uses_market_time(app, broker, clock):
    from datetime import timezone

    sold_utc = datetime(2026, 10, 26, 15, 46, tzinfo=timezone.utc)  # 11:46 ET
    clock.dt = datetime(2026, 10, 26, 11, 50, tzinfo=ET)
    o = Order("s1", "pb-exit-x", "SPY261027C00767000", "sell", 1, 1, "limit", "filled", 2.0, None, sold_utc, sold_utc, 2.0)
    broker.orders[o.id] = o
    ok, why = check(app, entry())
    assert not ok and "sold 11:46 ET" in why


def test_a_fill_is_alerted_once_even_if_the_broker_updates_it(make_app, broker, clock, channel):
    from paperbot.watcher import report_fills

    app = make_app()
    report_fills(app)  # baseline
    assert app.funnel.enter(CALL, 1, "x", "s1").status == "submitted"
    order = next(o for o in broker.orders.values() if o.is_open and o.side == "buy")
    clock.advance(seconds=10)
    broker.fill_at_limit(order.id)
    report_fills(app)
    clock.advance(seconds=10)
    broker.orders[order.id] = dataclasses.replace(broker.orders[order.id], filled_at=clock())  # broker moves filled_at
    report_fills(app)
    assert [t for t, _, _ in channel.sent].count("Filled (entry)") == 1

    restarted = make_app()  # a restart: fresh memory, same journal
    restarted.last_fill_check = clock() - timedelta(minutes=5)
    report_fills(restarted)
    assert [t for t, _, _ in channel.sent].count("Filled (entry)") == 1
