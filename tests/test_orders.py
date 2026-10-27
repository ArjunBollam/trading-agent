"""The order funnel and the code-run protection pass (stops, exits, loss limit)."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from fakes import CALL, Order

import paperbot
from paperbot.models import Position
from paperbot.orders import client_order_id
from paperbot.protect import protect

PKG = Path(paperbot.__file__).parent


def test_only_the_funnel_places_or_cancels_orders():
    pattern = re.compile(r"\.(submit_order|close_all|cancel_order)\(")
    offenders = [
        f.name for f in PKG.glob("*.py")
        if f.name not in ("orders.py", "broker.py") and pattern.search(f.read_text())
    ]
    assert offenders == []


def test_research_tools_cannot_trade(app):
    from paperbot.research import ResearchTools

    names = {d["name"] for d in ResearchTools(app).definitions}
    assert names == {"get_stock_bars", "get_stock_snapshot", "get_option_chain", "get_option_quotes", "get_news"}
    assert all(d["strict"] and d["input_schema"]["additionalProperties"] is False for d in ResearchTools(app).definitions)


# ---- funnel ---------------------------------------------------------------------------------


def test_entry_is_a_limit_near_mid(app, broker):
    res = app.funnel.enter(CALL, 2, "SPY breaking out", "20261026T1040")
    assert res.status == "submitted"
    req = broker.submitted[-1]
    # mid 2.05 + 0.2 * (ask - mid) = 2.06, never above the 2.10 ask
    assert (req.type, req.limit_price, req.time_in_force, req.position_intent) == ("limit", 2.06, "day", "buy_to_open")


def test_client_order_ids_are_deterministic_per_cycle():
    a = client_order_id("entry", "20261026T1040", CALL, "buy", 1)
    assert a == client_order_id("entry", "20261026T1040", CALL, "buy", 1)
    assert a != client_order_id("entry", "20261026T1050", CALL, "buy", 1)
    assert a.startswith("pb-entry-") and len(a) < 128


def test_resending_the_same_order_is_not_filled_twice(app, broker):
    app.funnel.enter(CALL, 1, "x", "s1")
    again = app.funnel._submit(broker.submitted[-1], "entry", "retry")
    assert again.status == "duplicate"
    assert len(broker.submitted) == 1


def test_dry_run_sends_nothing(make_app, broker):
    app = make_app(dry_run=True)
    assert app.funnel.enter(CALL, 1, "x", "s1").status == "dry_run"
    assert broker.submitted == []


def test_exit_cancels_the_stop_then_sells_at_the_bid(app, broker):
    broker.add_position(CALL, 2, 2.0)
    protect(app, "w1")
    stop = next(o for o in broker.orders.values() if o.is_open and o.is_stop)
    res = app.funnel.exit(CALL, None, "take_profit", "w2", discretionary=False)
    assert res.status == "submitted"
    assert stop.id in broker.cancelled
    req = broker.submitted[-1]
    assert (req.type, req.limit_price, req.qty, req.position_intent) == ("limit", 2.00, 2, "sell_to_close")
    assert CALL not in broker.pos


def test_unfilled_exit_is_replaced_by_a_market_order(app, broker, clock):
    broker.add_position(CALL, 1, 2.0)
    broker.hold_fills = True
    assert app.funnel.exit(CALL, None, "expiry", "w1", discretionary=False).status == "submitted"
    assert app.funnel.exit(CALL, None, "expiry", "w2", discretionary=False).status == "skipped"  # still fresh
    clock.advance(seconds=61)
    assert app.funnel.exit(CALL, None, "expiry", "w3", discretionary=False).status == "submitted"
    assert [r.type for r in broker.submitted] == ["limit", "market"]


def test_rejected_claude_sell_keeps_the_broker_stop(app, broker):
    broker.add_position(CALL, 1, 2.0, opened_minutes_ago=5)
    protect(app, "w1")
    res = app.funnel.exit(CALL, 1, "Claude changed its mind", "c1", discretionary=True)
    assert res.status == "rejected" and "min_hold" in res.detail
    assert broker.cancelled == []
    assert any(o.is_open and o.is_stop for o in broker.orders.values())


# ---- protection pass ------------------------------------------------------------------------


def open_stops(broker):
    return [o for o in broker.orders.values() if o.is_open and o.is_stop]


def test_every_position_gets_one_broker_stop(app, broker):
    broker.add_position(CALL, 2, 2.0)
    protect(app, "w1")
    protect(app, "w2")
    stops = open_stops(broker)
    assert len(stops) == 1
    assert (stops[0].stop_price, stops[0].qty) == (1.30, 2)
    assert broker.submitted[-1].time_in_force == "gtc"


def test_stop_is_replaced_when_the_position_changes(app, broker):
    broker.add_position(CALL, 1, 2.0)
    protect(app, "w1")
    broker.pos[CALL] = Position(CALL, 3, 2.10, 2.05, "us_option")  # added 2 more at 2.15
    protect(app, "w2")
    stops = open_stops(broker)
    assert len(stops) == 1 and stops[0].qty == 3 and stops[0].stop_price == 1.36


def test_broker_stop_fires_between_checks(app, broker):
    broker.add_position(CALL, 1, 2.0)
    protect(app, "w1")
    broker.set_quote(CALL, 1.25, 1.35)  # bid through the 1.30 stop: the broker fills it, no code needed
    assert CALL not in broker.pos
    rep = protect(app, "w2")
    assert rep.exits == [] and open_stops(broker) == []


def test_code_stop_loss_when_broker_stops_are_off(app, broker, cfg):
    cfg.exits.broker_stop = False
    broker.add_position(CALL, 1, 2.0)
    broker.set_quote(CALL, 1.20, 1.30)
    rep = protect(app, "w1")
    assert [r.status for r in rep.exits] == ["submitted"]
    assert (broker.submitted[-1].type, broker.submitted[-1].limit_price) == ("limit", 1.20)
    assert CALL not in broker.pos


def test_take_profit_and_expiry_exits(app, broker):
    broker.add_position(CALL, 1, 2.0)
    broker.add_position("SPY261027P00580000", 1, 2.0)  # 1 day left: close it
    broker.set_quote("SPY261027P00580000", 1.95, 2.05)
    protect(app, "w1")
    broker.set_quote(CALL, 3.20, 3.30)
    rep = protect(app, "w2")
    assert [r.status for r in rep.exits] == ["submitted"]
    assert broker.pos == {}


def test_stale_entries_and_orphan_sells_are_cancelled(app, broker, clock):
    app.funnel.enter(CALL, 1, "x", "s1")
    entry_id = next(o.id for o in broker.orders.values() if o.is_open)
    broker.orders["orphan"] = Order("orphan", "pb-stop-x", "QQQ261030C00500000", "sell", 1, 0, "stop", "new",
                                    None, 1.0, clock())
    protect(app, "w1")
    assert "orphan" in broker.cancelled and entry_id not in broker.cancelled
    clock.advance(minutes=5)
    protect(app, "w2")
    assert entry_id in broker.cancelled


def test_daily_loss_limit_flattens_once_and_halts_for_the_day(app, broker, channel):
    broker.add_position(CALL, 1, 2.0)
    broker.set_equity(97_900)
    rep = protect(app, "w1")
    assert rep.flattened and broker.closed_all == 1 and broker.pos == {}
    assert app.killswitch.entries_halted(date(2026, 10, 26))
    assert any(p == "urgent" for _, _, p in channel.sent)
    protect(app, "w2")
    assert broker.closed_all == 1
