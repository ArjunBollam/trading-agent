"""Order flow (orderflow.py): signing the tape, footprint bars, the profile, each signal, and the wiring
into the AI prompt, the watcher and the scanner."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fakes import ET
from paperbot import orderflow as of
from paperbot.explain import EXPLAIN
from paperbot.models import StockSnap
from paperbot.patterns import LABELS
from paperbot.prompt import cycle_prompt
from paperbot.research import build_snapshot
from paperbot.watcher import maybe_orderflow

DAY = date(2026, 10, 26)
OPEN = datetime(2026, 10, 26, 9, 30, tzinfo=ET)


def row(t: datetime, price: float, size: float, cond=(" ",)) -> dict:
    return {"t": t.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z"), "p": price, "s": size, "c": list(cond)}


def mk(i: int, o: float, h: float, low: float, c: float, buy: float, sell: float, poc: float | None = None,
       step: float = 0.05) -> of.FlowBar:
    """A footprint bar: volume spread evenly over its prices, or half of it on `poc`."""
    b = of.FlowBar(OPEN + timedelta(minutes=5 * i), step, o, h, low, c, buy, sell)
    keys = list(range(round(low / step), round(h / step) + 1))
    share = 0.5 if poc is not None else 1.0
    for k in keys:
        b.rows[k] = [sell * share / len(keys), buy * share / len(keys)]
    if poc is not None:
        k = round(poc / step)
        b.rows[k] = [b.rows[k][0] + sell / 2, b.rows[k][1] + buy / 2]
    b.pv = (h + low + c) / 3 * (buy + sell)
    return b


def with_cvd(bars: list[of.FlowBar]) -> list[of.FlowBar]:
    total = 0.0
    for b in bars:
        total += b.delta
        b.cvd = total
    return bars


def quiet_descent(n: int = 8) -> list[of.FlowBar]:
    out = []
    for i in range(n):
        o = 100.40 - 0.08 * i
        c = o - 0.06
        out.append(mk(i, o, o + 0.10, c - 0.14, c, 10_500 if i % 2 else 9_500, 9_500 if i % 2 else 10_500))
    return out


def absorption_day() -> list[of.FlowBar]:
    """A slow drift down into the prior-day low at 99.50, heavy selling absorbed there, then buyers take over."""
    bars = quiet_descent()
    bars.append(mk(8, 99.70, 99.75, 99.50, 99.68, 5_000, 25_000, poc=99.50))  # absorption
    bars.append(mk(9, 99.68, 99.90, 99.66, 99.85, 15_000, 8_000))  # confirmation
    return with_cvd(bars)


# ---- the tape ----------------------------------------------------------------------------------


def test_tick_rule_signs_trades_and_skips_auction_and_out_of_session_prints():
    tape = of.Tape(DAY, 5, ET, step=0.05)
    t = OPEN + timedelta(minutes=1)
    tape.add([
        row(OPEN - timedelta(minutes=5), 99.00, 500),  # pre-market: not in the regular session
        row(OPEN, 100.00, 9_000, ("O",)),  # the opening auction print: nobody crossed the spread
        row(t, 100.00, 100),  # first print: side unknown
        row(t + timedelta(seconds=1), 100.05, 200),  # uptick: bought at the offer
        row(t + timedelta(seconds=2), 100.05, 300),  # same price: keeps the last side
        row(t + timedelta(seconds=3), 100.00, 400, (" ", "I")),  # downtick (odd lot still counts)
    ])
    [bar] = tape.bars
    assert (bar.other, bar.buy, bar.sell) == (100, 500, 400)
    assert bar.delta == 100 and bar.cvd == 100
    assert bar.row(100.05) == (0.0, 500.0) and bar.row(100.00) == (400.0, 0.0)
    assert (bar.o, bar.h, bar.l, bar.c) == (100.00, 100.05, 100.00, 100.00)


def test_bars_follow_the_session_clock_and_only_closed_bars_count():
    tape = of.Tape(DAY, 5, ET)
    tape.add([row(OPEN + timedelta(minutes=1), 100.0, 10), row(OPEN + timedelta(minutes=4, seconds=59), 100.1, 10),
              row(OPEN + timedelta(minutes=5), 100.2, 10)])
    assert [b.t for b in tape.bars] == [OPEN, OPEN + timedelta(minutes=5)]
    assert tape.step == 0.01  # auto row height for a $100 stock
    assert [b.t for b in tape.closed(OPEN + timedelta(minutes=9))] == [OPEN]


def test_volume_profile_finds_the_point_of_control_and_value_area():
    bars = [mk(0, 100.0, 100.5, 100.0, 100.2, 5_000, 5_000), mk(1, 100.2, 100.3, 100.2, 100.25, 20_000, 20_000)]
    p = of.volume_profile(bars, 0.25)
    assert p.poc == 100.25 and p.val <= 100.25 <= p.vah


# ---- signals -----------------------------------------------------------------------------------


def test_absorption_at_a_key_level_then_confirmation_gives_a_long_setup():
    bars = absorption_day()
    sigs = of.analyze(bars, {"prior low": 99.50})
    absorb = next(s for s in sigs if s.kind == "of_absorption_bull")
    assert absorb.bar_time == bars[8].t and not absorb.weak and absorb.level == "prior low"
    assert "Wait for buyers" in absorb.detail
    long = next(s for s in sigs if s.kind == "of_absorption_long")
    assert long.bar_time == bars[9].t and long.setup and long.entry == 99.85
    assert long.stop == 99.45 and long.target > long.entry


def test_absorption_away_from_levels_is_weak_and_a_broken_level_cancels_it():
    bars = absorption_day()
    sigs = of.analyze(bars, {})
    assert next(s for s in sigs if s.kind == "of_absorption_bull").weak
    broken = absorption_day()[:9] + [mk(9, 99.68, 99.70, 99.20, 99.25, 15_000, 8_000)]
    assert not [s for s in of.analyze(with_cvd(broken), {"prior low": 99.50}) if s.kind == "of_absorption_long"]


def test_signals_use_only_earlier_bars():
    bars = absorption_day()
    full = [(s.kind, s.bar_time) for s in of.analyze(bars, {"prior low": 99.50}) if s.bar_time <= bars[8].t]
    prefix = [(s.kind, s.bar_time) for s in of.analyze(bars[:9], {"prior low": 99.50})]
    assert full == prefix


def test_stacked_buy_imbalances():
    bars = with_cvd(quiet_descent(6))
    b = mk(6, 100.00, 100.40, 100.00, 100.35, 0, 0)
    for k in b.rows:
        b.rows[k] = [500.0, 500.0]
    for p in (100.15, 100.20, 100.25):
        b.rows[round(p / 0.05)][1] = 3_000.0
    b.buy = sum(r[1] for r in b.rows.values())
    b.sell = sum(r[0] for r in b.rows.values())
    bars = with_cvd(bars + [b])
    stack = next(s for s in of.analyze(bars) if s.kind == "of_stacked_buy")
    assert "100.15-100.25" in stack.detail and stack.side == "buy"


def test_delta_divergence_at_a_higher_high():
    bars = [mk(i, 100.0, 100.3, 99.9, 100.1, 10_000, 10_000) for i in range(6)]
    bars.append(mk(6, 100.1, 100.80, 100.1, 100.7, 20_000, 10_000))  # rally on strong buying: the CVD peak
    bars += [mk(7, 100.7, 100.7, 100.4, 100.45, 7_000, 10_000), mk(8, 100.45, 100.6, 100.4, 100.5, 7_000, 10_000)]
    bars.append(mk(9, 100.5, 100.90, 100.5, 100.6, 9_000, 10_000))  # higher high, lower CVD, negative delta
    div = next(s for s in of.analyze(with_cvd(bars)) if s.kind == "of_divergence_bear")
    assert div.bar_time == bars[9].t and "100.80" in div.detail


def test_failed_breakout_targets_the_point_of_control():
    bars = [mk(i, 100.2, 100.5, 100.0, 100.25, 10_000, 10_000) for i in range(10)]
    bars.append(mk(10, 100.45, 100.80, 100.40, 100.70, 3_000, 2_000))  # pokes above balance
    bars.append(mk(11, 100.70, 100.72, 100.25, 100.30, 4_000, 24_000))  # straight back in, heavy selling
    sig = next(s for s in of.analyze(with_cvd(bars)) if s.kind == "of_failed_breakout")
    assert sig.setup and sig.entry == 100.30 and sig.stop > 100.80 and sig.target < sig.entry


def test_trend_continuation_on_a_pullback_with_aggression():
    bars = [mk(i, 100.2, 100.5, 100.0, 100.25, 10_000, 10_000) for i in range(8)]
    bars += [mk(8, 100.5, 100.9, 100.5, 100.85, 6_000, 5_000), mk(9, 100.85, 101.2, 100.8, 101.1, 6_000, 5_000)]
    bars.append(mk(10, 101.1, 101.15, 100.55, 100.95, 25_000, 6_000))  # dips to the value-area high, buyers step in
    sig = next(s for s in of.analyze(with_cvd(bars)) if s.kind == "of_trend_long")
    assert sig.setup and sig.stop < 100.55 < sig.entry < sig.target


def test_evaluate_counts_r_and_ties_go_to_the_stop():
    sig = of.FlowSignal("of_absorption_long", "buy", OPEN, 100.0, "", entry=100.0, stop=99.0, target=102.0)
    win = [mk(1, 100, 101, 99.5, 100.5, 1, 1), mk(2, 100.5, 102.1, 100, 102, 1, 1)]
    assert of.evaluate(sig, win) == {"result": "target", "r": 2.0}
    assert of.evaluate(sig, [mk(1, 100, 102.5, 98.9, 100, 1, 1)]) == {"result": "stop", "r": -1.0}


def test_every_order_flow_signal_has_a_label_and_a_tooltip():
    assert set(of.LABELS) <= set(LABELS) and set(of.LABELS) <= set(EXPLAIN)
    assert of.SETUPS <= set(of.LABELS)


# ---- live tape and wiring ----------------------------------------------------------------------


class FakeTrades:
    def __init__(self, rows: list[dict]):
        self.rows, self.calls = rows, []

    def trades(self, symbol, start, end):
        self.calls.append((start, end))
        return [r for r in self.rows if start <= datetime.fromisoformat(r["t"].replace("Z", "+00:00")) < end]


def test_tracker_only_fetches_new_trades():
    src = FakeTrades([row(OPEN + timedelta(minutes=m), 100 + m / 100, 10) for m in range(1, 20)])
    tracker = of.FlowTracker(src, 5, ET)
    tracker.update("SPY", OPEN + timedelta(minutes=12))
    assert src.calls[0][0] == OPEN and tracker.tapes["SPY"].trades == 11
    tracker.update("SPY", OPEN + timedelta(minutes=17))
    assert src.calls[-1][0] > OPEN + timedelta(minutes=11) and tracker.tapes["SPY"].trades == 16


class FakeFlow:
    """Stands in for a FlowTracker: a fixed set of closed bars."""

    settings = of.Settings()

    def __init__(self, bars):
        self.bars = bars

    def update(self, symbol, now):
        return self

    def closed(self, now):
        return [b for b in self.bars if b.t + timedelta(minutes=5) <= now]

    def signals(self, symbol, now, prior=None):
        bars = self.closed(now)
        return bars, of.analyze(bars, prior, self.settings)


def test_watcher_alerts_a_confirmed_setup_once(app, broker, clock, channel):
    broker.stocks["SPY"] = StockSnap("SPY", 99.85, 100.40, 100.40, 100.50, 99.50, 1e6, prev_high=101.0, prev_low=99.50)
    app.flow = FakeFlow(absorption_day())
    app.cfg.schedule.trigger_cycle_on_signal = True
    clock.dt = OPEN + timedelta(minutes=50, seconds=5)  # the 10:15 confirmation bar has just closed
    maybe_orderflow(app)
    logged = app.journal.events(app.today(), "orderflow_signal")
    assert any(e["kind"] == "of_absorption_long" and e["entry"] == 99.85 for e in logged)
    assert [t for t, *_ in channel.sent if t == "Order flow: SPY"] and app.cycle_requested
    sent = len(channel.sent)
    maybe_orderflow(app)  # same bar: nothing new
    assert len(channel.sent) == sent


def test_prompt_carries_the_order_flow_summary(app, broker, clock):
    broker.stocks["SPY"] = StockSnap("SPY", 99.85, 100.40, 100.40, 100.50, 99.50, 1e6, prev_high=101.0, prev_low=99.50,
                                     as_of=OPEN + timedelta(minutes=50))
    app.flow = FakeFlow(absorption_day())
    clock.dt = OPEN + timedelta(minutes=50, seconds=5)
    snap = build_snapshot(app)
    recent = snap.orderflow["SPY"]["recent_signals"]
    assert {"at": "10:15", "signal": "Absorption confirmed: long", "side": "buy", "entry": 99.85, "stop": 99.45} \
        .items() <= next(r for r in recent if r["signal"] == "Absorption confirmed: long").items()
    assert "Order flow (code-computed from the trade tape)" in cycle_prompt(app, snap)


def test_order_flow_is_off_unless_enabled(app, broker, clock):
    from paperbot.app import flow_tracker

    app.cfg.orderflow.enabled = False  # config.yaml may turn it on; the default is off
    assert flow_tracker(app.cfg) is None and app.flow is None
    clock.dt = OPEN + timedelta(minutes=50, seconds=5)
    assert not build_snapshot(app).orderflow


def test_scanner_shows_order_flow_signals_on_its_charts(cfg):
    from test_scan import FakeSource, make_scanner

    now = OPEN + timedelta(minutes=50, seconds=5)
    scanner, _ = make_scanner(cfg, FakeSource({}, []), lambda: now)
    assert scanner._flow_signals("SPY", [], DAY, now, catch_up=True) == []  # no tracker: nothing
    scanner.flow = FakeFlow(absorption_day())
    sigs = scanner._flow_signals("SPY", [], DAY, now, catch_up=False)
    assert {s.kind for s in sigs} >= {"of_absorption_long"} and all(s.bar_time == OPEN + timedelta(minutes=45) for s in sigs)
    assert sigs[0].label in LABELS.values()
