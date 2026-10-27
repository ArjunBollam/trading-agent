"""Symbols, config validation, the risk gate and the exit rules."""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta

import pytest
import yaml
from fakes import CALL, ET, PUT
from pydantic import ValidationError

from paperbot.broker import AlpacaPaperBroker, LiveTradingRefused
from paperbot.config import Config
from paperbot.exits import EXPIRY, STOP_LOSS, TAKE_PROFIT, exit_reason, protective_stop_price
from paperbot.models import OptionSymbol, Order, OrderRequest, Position, Quote, round_to_tick

from conftest import ROOT


def entry(symbol=CALL, qty=1, limit=2.06, cid="pb-entry-test-1"):
    return OrderRequest(symbol, "buy", qty, "limit", "day", cid, "buy_to_open", limit_price=limit)


def sell(symbol=CALL, qty=1, type="limit", limit=2.00, stop=None, cid="pb-exit-test-1"):
    return OrderRequest(symbol, "sell", qty, type, "day", cid, "sell_to_close", limit_price=limit, stop_price=stop)


# ---- models and config ----------------------------------------------------------------------


def test_occ_symbol_parsing():
    occ = OptionSymbol.parse(CALL)
    assert (occ.underlying, occ.expiry, occ.right, occ.strike) == ("SPY", date(2026, 10, 30), "C", 580.0)
    assert occ.dte(date(2026, 10, 26)) == 4
    assert OptionSymbol.parse("SPY") is None
    assert OptionSymbol.parse("SPY261340C00580000") is None  # month 13


def test_tick_rounding():
    assert round_to_tick(3.47, "AAPL", "down") == 3.45  # $0.05 ticks at $3+
    assert round_to_tick(2.337, "AAPL", "down") == 2.33
    assert round_to_tick(5.123, "SPY", "down") == 5.12  # SPY trades in pennies at every price
    assert round_to_tick(3.41, "AAPL", "up") == 3.45


def test_repo_config_loads():
    cfg = Config.load(ROOT / "config.yaml")
    assert cfg.llm.provider in ("anthropic", "xai", "groq")
    assert cfg.llm.anthropic.model == "claude-opus-5-5"
    assert cfg.risk.min_hold_minutes >= 1


@pytest.mark.parametrize("patch", [
    {"risk": {"min_hold_minutes": 0}},       # would silently disable the churn guard
    {"exits": {"close_at_dte": 2}},          # >= min_dte: entries would be closed at once
    {"risk": {"max_dte": 1}},                # below min_dte
])
def test_config_rejects_values_that_disable_a_limit(patch):
    raw = yaml.safe_load((ROOT / "config.yaml").read_text())
    for section, values in patch.items():
        raw[section].update(values)
    with pytest.raises(ValidationError):
        Config.model_validate(raw)


def test_broker_refuses_live_keys_and_always_uses_paper_url():
    with pytest.raises(LiveTradingRefused):
        AlpacaPaperBroker("AKLIVEKEY", "secret")
    b = AlpacaPaperBroker("PKPAPERKEY", "secret")
    assert b._trading._base_url.value == "https://paper-api.alpaca.markets"


# ---- risk gate: entries ---------------------------------------------------------------------


def check(app, req, kind="entry", discretionary=False):
    ctx = app.funnel.context(req.symbol, entry=kind == "entry")
    return app.gate.check(req, ctx, kind=kind, discretionary=discretionary)


def test_valid_entry_passes(app):
    assert check(app, entry()) == (True, "ok")


@pytest.mark.parametrize("req, why", [
    (entry(symbol="SPY"), "not an option"),
    (entry(symbol="TSLA261030C00200000"), "not in the universe"),
    (entry(qty=6), "max_contracts_per_order"),
    (entry(qty=0), "positive whole number"),
    (entry(symbol="SPY261027C00580000"), "outside 2-14"),        # 1 day to expiration
    (entry(symbol="SPY261127C00580000"), "outside 2-14"),        # 32 days
    (entry(limit=2.20), "above the ask"),
    (dataclasses.replace(entry(), type="market", limit_price=None), "limit orders"),
])
def test_entry_rejections(app, req, why):
    ok, reason = check(app, req)
    assert not ok and why in reason


def test_no_entries_after_cutoff(app, clock):
    clock.dt = datetime(2026, 10, 26, 15, 20, tzinfo=ET)
    ok, reason = check(app, entry())
    assert not ok and "no new entries" in reason


def test_wide_spread_rejected_on_fresh_quote(app, broker):
    broker.set_quote(CALL, 1.50, 2.10)
    ok, reason = check(app, entry(limit=1.80))
    assert not ok and "spread" in reason


def test_only_one_working_entry_per_contract(app):
    assert app.funnel.enter(CALL, 1, "first", "s1").status == "submitted"
    ok, reason = check(app, entry())
    assert not ok and "already working" in reason


def test_max_positions(app, broker):
    for sym in ("QQQ261030C00500000", "AAPL261030C00230000", "MSFT261030C00420000"):
        broker.add_position(sym, 1, 2.0)
    ok, reason = check(app, entry())
    assert not ok and "max_positions" in reason


def test_max_position_usd_counts_what_is_held(app, broker):
    broker.add_position(CALL, 5, 2.0)
    ok, reason = check(app, entry(qty=5))
    assert not ok and "max_position_usd" in reason


def test_max_entries_per_day(app, broker, clock):
    for i in range(6):
        t = clock() - timedelta(minutes=30 + i)
        broker.orders[f"e{i}"] = Order(f"e{i}", f"pb-entry-x-{i}", PUT, "buy", 1, 1, "limit", "filled", 2.0, None, t, t, 2.0)
    ok, reason = check(app, entry())
    assert not ok and "entries today" in reason


def test_reentry_cooldown_after_selling_the_underlying(app, broker, clock):
    t = clock() - timedelta(minutes=10)
    broker.orders["s1"] = Order("s1", "pb-exit-x", PUT, "sell", 1, 1, "limit", "filled", 2.0, None, t, t, 2.0)
    ok, reason = check(app, entry())
    assert not ok and "cooldown" in reason


def test_entry_must_fit_remaining_daily_loss_budget(app, broker, cfg):
    cfg.risk.soft_loss_limit_pct = None  # isolate the budget rule from the soft limit's halving
    broker.set_equity(98_300)  # down $1,700 of a $2,000 limit
    assert check(app, entry(qty=1))[0]  # $206 premium, $108 to the padded stop
    ok, reason = check(app, entry(qty=4))  # $824 premium, $433 to the padded stop
    assert not ok and "remaining daily loss budget" in reason


def test_daily_loss_limit_threshold(app, broker):
    broker.set_equity(98_001)
    assert not app.gate.daily_loss_breached(broker.account())
    broker.set_equity(98_000)
    assert app.gate.daily_loss_breached(broker.account())


# ---- risk gate: exits and stops -------------------------------------------------------------


def test_cannot_sell_what_is_not_held(app):
    ok, reason = check(app, sell(), kind="exit")
    assert not ok and "not held" in reason


def test_cannot_sell_shares_reserved_by_a_resting_stop(app, broker):
    broker.add_position(CALL, 2, 2.0)
    assert app.funnel.place_stop(broker.positions()[0], "s1").status == "submitted"
    ok, reason = check(app, sell(qty=2), kind="exit")
    assert not ok and "available to sell" in reason


def test_min_hold_blocks_claude_but_not_code_exits(app, broker):
    broker.add_position(CALL, 1, 2.0, opened_minutes_ago=5)
    ok, reason = check(app, sell(), kind="exit", discretionary=True)
    assert not ok and "min_hold_minutes" in reason
    assert check(app, sell(), kind="exit", discretionary=False) == (True, "ok")


def test_stop_above_the_bid_is_rejected(app, broker):
    broker.add_position(CALL, 1, 2.0)
    req = dataclasses.replace(sell(type="stop", limit=None, stop=2.00), time_in_force="gtc")
    ok, reason = check(app, req, kind="stop")
    assert not ok and "code exit handles it" in reason


# ---- exit rules -----------------------------------------------------------------------------


def pos(entry_px=2.0, symbol=CALL):
    return Position(symbol, 1, entry_px, entry_px, "us_option")


@pytest.mark.parametrize("bid, ask, expected", [
    (1.20, 1.30, STOP_LOSS),     # mid 1.25 = -37.5%
    (3.20, 3.30, TAKE_PROFIT),   # mid 3.25 = +62.5%
    (2.00, 2.10, None),
])
def test_exit_reason(cfg, bid, ask, expected):
    assert exit_reason(pos(), Quote(CALL, bid, ask), date(2026, 10, 26), cfg.exits) == expected


def test_close_before_expiry(cfg):
    assert exit_reason(pos(), Quote(CALL, 2.0, 2.1), date(2026, 10, 29), cfg.exits) == EXPIRY


def test_protective_stop_price(cfg):
    assert protective_stop_price(pos(2.0), cfg.exits) == 1.30
    assert protective_stop_price(pos(5.0, "AAPL261030C00230000"), cfg.exits) == 3.25
