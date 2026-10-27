"""The bigger picture (context.py): patterns and trend over the last few hours, checked against each signal."""

from __future__ import annotations

from datetime import timedelta

from test_formations import T0, hits, kinds, path
from test_scan import FakeSource, make_scanner

from paperbot.config import ScanConfig
from paperbot.context import Context, Factor, assess, resample
from paperbot.features import Bar
from paperbot.formations import FORMING, LABELS, catalog, chart_patterns
from paperbot.patterns import ChartSignal, analyze
from paperbot.scanner import Found

SC = ScanConfig()
DAY = T0.date()


def head_and_shoulders() -> list[Bar]:
    """Shoulders at 103 and 103.1, head at 106, neckline about 101.1; price eases toward the neckline,
    breaks it near 13:00, and bounces a little."""
    return path(99, 103, 101, 106, 101.1, 103.1, 101.4, 101.3, 98.5, 99.6, steps=6)


def test_forming_kinds_are_labelled_and_listed_as_classic():
    assert all(k in LABELS for k in FORMING)
    assert LABELS["head_and_shoulders_forming"] == "Head and shoulders forming"
    assert {k for g, k, _ in catalog() if g == "classic"} >= FORMING


def test_head_and_shoulders_warns_before_its_neckline_breaks():
    found = hits(head_and_shoulders(), chart_patterns)
    forming = [i for k, s, i in found if (k, s) == ("head_and_shoulders_forming", "sell")]
    broke = [i for k, s, i in found if (k, s) == ("head_and_shoulders", "sell")]
    assert forming and broke and forming[0] < broke[0]
    mirrored = [Bar(b.t, 200 - b.o, 200 - b.l, 200 - b.h, 200 - b.c, b.v) for b in head_and_shoulders()]
    assert ("inverse_head_and_shoulders_forming", "buy") in kinds(hits(mirrored, chart_patterns))


def test_a_forming_pattern_counts_until_the_neckline_breaks():
    bars = head_and_shoulders()[:40]  # right shoulder in, price 0.3 ATR above the neckline
    ctx = assess(bars, DAY, SC)
    f = next(f for f in ctx.factors if "head and shoulders forming" in f.text)
    assert f.side == -1 and f.weight == 2 and "neckline 101.1" in f.text


def test_a_broken_pattern_makes_the_picture_bearish_and_flags_buy_signals():
    ctx = assess(head_and_shoulders()[:54], DAY, SC)
    texts = [f.text for f in ctx.factors]
    assert ctx.bias == "bearish" and ctx.score <= -SC.context_threshold
    assert any(t.startswith("head and shoulders on the 5-minute chart broke down") for t in texts)
    assert any(t.startswith("lower highs") for t in texts)
    assert ctx.conflict("sell") is None
    why = ctx.conflict("buy")
    assert why and "is bearish" in why and "head and shoulders" in why and "This buy signal goes against it" in why


def test_bounce_signals_after_the_break_are_conflicts():
    bars = head_and_shoulders()
    flagged = set()
    for end in range(44, len(bars) + 1):
        ctx = assess(bars[:end], DAY, SC)
        for s in analyze("T", bars[:end], DAY, SC)[0]:
            if s.side == "buy":
                assert ctx.conflict("buy"), s.kind  # every buy after the break goes against it
                flagged.add(s.kind)
            else:
                assert ctx.conflict("sell") is None
    assert flagged  # the bounce does produce buy signals


def test_a_pattern_that_failed_stops_counting():
    bars = path(99, 103, 101, 106, 101.1, 103.1, 101.4, 101.3, 98.5, 104, steps=6)  # back far above the neckline
    assert not any("head and shoulders on" in f.text for f in assess(bars, DAY, SC).factors)


def test_one_major_factor_vetoes_even_when_the_score_is_mixed():
    ctx = Context((Factor(-1, 2, "head and shoulders forming"), Factor(1, 1, "price above VWAP")), 3, 180)
    assert ctx.bias == "mixed" and ctx.conflict("buy") and ctx.conflict("sell") is None
    minor = Context((Factor(-1, 1, "MACD below its signal"),), 3, 180)
    assert minor.conflict("buy") is None


def test_off_and_short_charts_say_nothing():
    assert assess(head_and_shoulders(), DAY, SC.model_copy(update={"context_mode": "off"})).factors == ()
    assert assess(head_and_shoulders()[:20], DAY, SC).factors == ()


def test_resample_builds_slower_candles_and_drops_the_one_still_filling():
    bars = [Bar(T0 + timedelta(minutes=5 * i), 100 + i, 100.5 + i, 99.5 + i, 100.2 + i, 10) for i in range(13)]
    slow = resample(bars, 15, 5)
    assert [b.t for b in slow] == [T0 + timedelta(minutes=15 * k) for k in range(4)]  # 10:30 isn't complete
    first = slow[0]
    assert (first.o, first.h, first.l, first.c, first.v) == (100, 102.5, 99.5, 102.2, 30)


def test_found_text_carries_the_warning():
    ctx = assess(head_and_shoulders()[:54], DAY, SC)
    sig = ChartSignal("T", "support_test", "buy", 99.6, T0, "tested the low of day")
    assert "WARNING: Bigger picture" in Found(sig, None, (), ctx).text()
    assert "WARNING" not in Found(sig, None, (), Context((), 3, 180)).text()


def _scan(cfg, mode):
    cfg.scan = cfg.scan.model_copy(update={"context_mode": mode, "watchlist": ["SPY"]})
    bars = head_and_shoulders()[:47]  # the last bar has a support test (buy) and three crows (sell)
    clock = bars[-1].t + timedelta(minutes=5, seconds=10)
    scanner, rec = make_scanner(cfg, FakeSource({"SPY": bars}, []), lambda: clock)
    return scanner.scan(), rec


def test_scanner_warns_on_conflicts(cfg):
    r, rec = _scan(cfg, "warn")
    assert r.contexts["SPY"].bias == "bearish"
    assert any(f.conflict for f in r.found) and any(not f.conflict for f in r.found)
    title, message, priority = rec.sent[0]
    assert "against the bigger picture: SPY" in title and priority == "high"
    assert "WARNING: Bigger picture" in message
    events = [e for e in r.found if e.signal.side == "buy"]
    assert events and all(e.conflict for e in events)


def test_scanner_suppress_holds_conflicting_signals_back(cfg):
    r, rec = _scan(cfg, "suppress")
    assert any(f.conflict for f in r.found)  # still found and logged
    assert rec.sent and all("WARNING" not in m for _, m, _ in rec.sent)
