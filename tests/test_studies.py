"""The Charts tab's indicators (paperbot/studies.py) and formula charts (paperbot/formula.py)."""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from paperbot import studies as st
from paperbot.features import Bar
from paperbot.formula import combine, is_formula, parse
from paperbot.patterns import ema_series, rsi_series

TZ = ZoneInfo("America/New_York")


def bars(n=300, seed=3):
    random.seed(seed)
    out, p = [], 100.0
    t = datetime(2026, 11, 24, 9, 30, tzinfo=TZ)
    for i in range(n):
        o = p
        p = max(1.0, p + random.gauss(0, 0.4))
        out.append(Bar(t + timedelta(minutes=5 * i), o, max(o, p) + random.random() * 0.2, min(o, p) - random.random() * 0.2, p,
                       1000 + random.random() * 5000))
    return out


def values(result, key):
    return next(o["values"] for o in result["outputs"] if o["key"] == key)


def test_ema_and_rsi_match_the_scanners_own():
    b = bars()
    closes = [x.c for x in b]
    out = st.compute(b, [{"id": "e", "type": "ema", "inputs": {"length": 21}}, {"id": "r", "type": "rsi"}])
    for got, want in zip(values(out[0], "ma"), ema_series(closes, 21)):
        assert got == pytest.approx(want, abs=1e-5) if want is not None else got is None
    for got, want in zip(values(out[1], "rsi"), rsi_series(closes, 14)):
        assert got == pytest.approx(want, abs=1e-5) if want is not None else got is None


def test_simple_average_and_bands():
    b = bars(40)
    out = st.compute(b, [{"type": "sma", "inputs": {"length": 5}}, {"type": "bb"}])
    sma = values(out[0], "ma")
    assert sma[3] is None and sma[4] == pytest.approx(sum(x.c for x in b[:5]) / 5, abs=1e-5)
    basis, up, lo = (values(out[1], k) for k in ("basis", "upper", "lower"))
    closes = [x.c for x in b[:20]]
    mean = sum(closes) / 20
    sd = math.sqrt(sum((c - mean) ** 2 for c in closes) / 20)
    assert basis[19] == pytest.approx(mean, abs=1e-5) and up[19] == pytest.approx(mean + 2 * sd, abs=1e-5) and lo[19] == pytest.approx(mean - 2 * sd, abs=1e-5)


def test_an_indicator_on_an_indicator_shares_its_pane():
    b = bars()
    out = st.compute(b, [{"id": "r", "type": "rsi"}, {"id": "m", "type": "sma", "inputs": {"source": "r:rsi", "length": 9}},
                         {"id": "e", "type": "ema", "inputs": {"length": 9}}])
    rsi = values(out[0], "rsi")
    ma = values(out[1], "ma")
    assert ma[30] == pytest.approx(sum(rsi[22:31]) / 9, abs=1e-5)
    assert out[1]["pane"] == "r" and out[1]["name"] == "SMA 9 on RSI 14"
    assert out[0]["pane"] == "r" and out[2]["pane"] == "price"


def test_macd_histogram_is_the_gap_between_its_lines():
    out = st.compute(bars(), [{"type": "macd"}])[0]
    for m, s, h in zip(values(out, "macd"), values(out, "signal"), values(out, "hist")):
        if h is not None:
            assert h == pytest.approx(m - s, abs=1e-5)


def test_every_indicator_runs_and_lines_up_with_the_candles():
    b = bars(250)
    specs = [{"id": f"x{i}", "type": t} for i, t in enumerate(st.CATALOG)]
    out = st.compute(b, specs)
    assert len(st.CATALOG) >= 40
    for r in out:
        for o in r["outputs"]:
            assert len(o["values"]) == len(b), r["type"]
            assert any(v is not None for v in o["values"]), (r["type"], o["key"])


def test_bad_inputs_and_sources_are_refused():
    b = bars(30)
    with pytest.raises(ValueError, match="Length must be 1 to 500"):
        st.compute(b, [{"type": "rsi", "inputs": {"length": 0}}])
    with pytest.raises(ValueError, match="unknown source"):
        st.compute(b, [{"type": "sma", "inputs": {"source": "nope:rsi"}}])
    with pytest.raises(ValueError, match="unknown indicator"):
        st.compute(b, [{"type": "astrology"}])
    with pytest.raises(ValueError, match="at most 50"):
        st.compute(b, [{"type": "sma"}] * 51)


def test_formulas_follow_arithmetic_rules():
    f, syms = parse("(AAPL + MSFT) / 2 - 3 * spy")
    assert syms == ["AAPL", "MSFT", "SPY"]
    assert f({"AAPL": 10, "MSFT": 20, "SPY": 1}) == 12
    assert parse("-QQQ/2")[0]({"QQQ": 4}) == -2
    assert is_formula("SPY/QQQ") and not is_formula("SPY") and not is_formula("BRK.B")
    for bad in ("SPY +", "(SPY", "2*3", "SPY $ QQQ", ""):
        with pytest.raises(ValueError):
            parse(bad)


def test_formula_candles_on_shared_times():
    t = datetime(2026, 11, 25, 10, tzinfo=TZ)
    a = [Bar(t, 10, 12, 9, 11, 5), Bar(t + timedelta(minutes=1), 11, 11, 10, 10, 5)]
    b = [Bar(t + timedelta(minutes=1), 5, 5, 4, 4, 7)]
    out = combine("A/B", {"A": a, "B": b})
    assert len(out) == 1 and out[0].o == pytest.approx(2.2) and out[0].c == pytest.approx(2.5)
    assert out[0].h == pytest.approx(2.5) and out[0].l == pytest.approx(2.2) and out[0].v == 0  # high: the largest of the four
