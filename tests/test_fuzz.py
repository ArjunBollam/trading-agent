"""Randomized test of the risk gate: whatever it accepts must satisfy every entry limit.

Seeded, so failures reproduce. 6,000 random orders across symbols, sizes, prices, quotes,
greeks, times of day and drawdowns.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from fakes import ET

from paperbot.models import Account, OptionSymbol, OrderRequest, Quote
from paperbot.risk import OrderContext

SYMBOLS = ["SPY261030C00580000", "SPY261106P00570000", "QQQ261106C00500000", "TSLA261030C00200000",
           "SPY261027C00580000", "AAPL261204C00230000", "SPY", "NVDA261106P00140000"]


def test_gate_never_accepts_an_entry_that_breaks_a_limit(cfg):
    from paperbot.risk import RiskGate

    gate = RiskGate(cfg)
    r, stop = cfg.risk, cfg.exits.stop_loss_pct / 100
    rng = random.Random(1234)
    accepted = 0
    def pick(good, bad, p_good=0.85):
        return rng.choice(good) if rng.random() < p_good else rng.choice(bad)

    for _ in range(6000):
        minutes = pick(list(range(20, 345)), list(range(0, 390)))  # mostly inside 09:50-15:15
        now = datetime(2026, 10, 26, 9, 30, tzinfo=ET) + timedelta(minutes=minutes)
        bid = round(rng.uniform(0.5, 8), 2)
        ask = round(bid * pick([1.01, 1.03, 1.05, 1.08], [1.15, 1.3, 1.5]), 2)
        quote = Quote("x", bid, ask, now - timedelta(seconds=pick([1, 10, 30], [90, 600])))
        equity = 100_000 - pick([0, 300, 900], [1500, 1900, 2500])
        acct = Account("PA1", equity, 100_000, pick([50_000, 100_000], [300, 1000]))
        ctx = OrderContext(now, acct, [], [], [], quote, delta=pick([0.35, 0.5, -0.45, 0.6], [None, 0.1, 0.8]),
                           open_interest=pick([150, 5000], [None, 10]),
                           theta=pick([-0.01 * bid, -0.05 * bid], [None, -0.3 * bid]))
        mid = (bid + ask) / 2
        limit = round(pick([mid, mid + 0.2 * (ask - mid)], [ask * 1.05, bid * 0.9]), 2)
        req = OrderRequest(pick(SYMBOLS[:3] + SYMBOLS[-1:], SYMBOLS[3:]), "buy", pick([1, 2, 3], [0, 6, 20]),
                           "limit", "day", "pb-entry-f", "buy_to_open", limit_price=limit)
        ok, _ = gate.check(req, ctx, "entry")
        if not ok:
            continue
        accepted += 1
        occ = OptionSymbol.parse(req.symbol)
        order_usd = req.qty * 100 * limit
        assert occ is not None and occ.underlying in r.universe
        assert 1 <= req.qty <= r.max_contracts_per_order
        assert r.min_dte <= occ.dte(now.date()) <= r.max_dte
        assert gate.entries_allowed(now) and gate.blackout_at(now) is None
        assert limit <= quote.ask and quote.spread_pct <= r.max_spread_pct
        assert quote.age_seconds(now) <= r.max_quote_age_seconds
        assert order_usd <= r.max_position_usd and order_usd <= acct.options_buying_power
        padded = order_usd * stop * r.stop_slippage_factor
        assert padded <= 100_000 * r.max_risk_per_trade_pct / 100
        assert padded <= gate.daily_loss_limit_usd(acct) - gate.drawdown_usd(acct)
        assert ctx.delta is not None and r.delta_min <= abs(ctx.delta) <= r.delta_max
        assert (ctx.open_interest is not None and ctx.open_interest >= r.min_open_interest) \
            or quote.spread_pct <= r.oi_waived_below_spread_pct
        assert ctx.theta is not None and abs(ctx.theta) / quote.mid * 100 <= r.max_theta_pct_per_day
    assert accepted > 200  # the generator does reach the accept path
