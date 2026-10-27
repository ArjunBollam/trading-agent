"""The exit simulator's trade replay, on hand-built bars."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from fakes import ET

from paperbot.features import Bar
from paperbot.simulate import ExitParams, Sample, grid, run, simulate_trade

T0 = datetime(2026, 10, 19, 10, 30, tzinfo=ET)
EXPIRY = date(2026, 10, 30)


def bars(*ohlc):
    return [Bar(T0 + timedelta(minutes=5 * i), o, h, l, c, 100) for i, (o, h, l, c) in enumerate(ohlc)]


def sim(b, p, cost=0.0, expiry=EXPIRY):
    return simulate_trade(b, 2.0, p, expiry, close_at_dte=1, tz=ET, cost_pct=cost)


def test_stop_fills_at_the_stop_price():
    r = sim(bars((2.0, 2.05, 1.9, 1.95), (1.9, 1.95, 1.2, 1.3)), ExitParams(35, 60))
    assert (r.reason, round(r.ret_pct, 1)) == ("stop", -35.0)


def test_gap_through_the_stop_fills_at_the_open():
    r = sim(bars((2.0, 2.05, 1.9, 1.95), (1.0, 1.1, 0.9, 1.0)), ExitParams(35, 60))
    assert (r.reason, round(r.ret_pct, 1)) == ("stop (gap)", -50.0)


def test_stop_and_target_in_one_bar_counts_as_the_stop():
    r = sim(bars((2.0, 3.5, 1.2, 3.0)), ExitParams(35, 60))
    assert r.reason == "stop"


def test_take_profit_and_cost():
    r = sim(bars((2.0, 2.1, 1.9, 2.0), (2.1, 3.3, 2.0, 3.1)), ExitParams(35, 60), cost=2.0)
    assert (r.reason, round(r.ret_pct, 1)) == ("take_profit", 58.0)


def test_breakeven_moves_the_stop_up():
    b = bars((2.0, 2.1, 1.95, 2.0), (2.0, 2.7, 2.0, 2.65), (2.6, 2.6, 1.9, 1.95))
    assert sim(b, ExitParams(35, 60, breakeven_pct=30)).ret_pct == 0.0  # stopped at entry
    assert sim(b, ExitParams(35, 60)).reason == "horizon"  # without it, still held


def test_time_stop_and_expiry_close():
    b = bars(*[(2.0, 2.05, 1.95, 2.0)] * 20)
    assert sim(b, ExitParams(35, 60, max_hold_minutes=30)).reason == "time_stop"
    near = [Bar(datetime(2026, 10, 29, 9, 30, tzinfo=ET), 2.0, 2.0, 2.0, 2.0, 1)]
    assert sim(b[:1] + near, ExitParams(35, 60), expiry=date(2026, 10, 30)).reason == "expiry"


def test_run_and_grid():
    rows = [{"t": (T0 + timedelta(minutes=5 * i)).isoformat(), "o": 2.0, "h": 2.0 + 0.1 * i, "l": 1.95, "c": 2.0 + 0.1 * i,
             "v": 1} for i in range(15)]
    samples = [Sample("2026-10-19", "SPY261030C00580000", str(EXPIRY), 2.0, rows)]
    variants = grid(ExitParams(35, 60, 30))
    assert ExitParams(35, 60, 30) in variants and all(v.breakeven_pct is None or v.breakeven_pct < v.tp_pct for v in variants)
    assert {v.stop_pct for v in variants} == {24, 35, 46} and {v.tp_pct for v in variants} == {40, 60, 90}
    out = run(samples, variants, close_at_dte=1, tz=ET, cost_pct=0)
    assert all(s.n == 1 for s in out)
    assert round(next(s for s in out if s.params == ExitParams(24, 40)).avg, 6) == 40.0


def test_rank_stability_detects_a_consistent_ranking():
    from paperbot.simulate import rank_stability

    rows = lambda up: [{"t": (T0 + timedelta(minutes=5 * i)).isoformat(), "o": 2.0, "h": 2.0 + up * i, "l": 2.0 - 0.02 * i,
                        "c": 2.0 + up * i, "v": 1} for i in range(40)]
    samples = [Sample(f"2026-09-{d:02d}", "SPY261030C00580000", str(EXPIRY), 2.0, rows(0.03)) for d in range(1, 21)]
    variants = grid(ExitParams(35, 60, 30))
    assert rank_stability(samples, variants, 1, ET, 0) == 1.0  # identical days rank identically
    assert rank_stability(samples[:4], variants, 1, ET, 0) is None  # too few days
