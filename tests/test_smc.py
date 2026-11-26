"""Smart money concepts (smc.py) and how the scanner hands them to the dashboard."""

from __future__ import annotations

import json
import random
from datetime import date, datetime, timedelta

from test_minutes import MinuteSource, minute_bars, setup
from test_qqe import five_minute_bars, walk
from test_scan import PREV, TODAY, TZ, FakeSource, at, breakout_bars, make_scanner

from paperbot.features import Bar
from paperbot.smc import smart_money


def bar(i: int, o: float, c: float, up: float = 0.05, down: float = 0.05, day: int = 26) -> Bar:
    t = datetime(2026, 10, day, 9, 30, tzinfo=TZ) + timedelta(minutes=5 * i)
    return Bar(t, o, max(o, c) + up, min(o, c) - down, c, 1000)


def path(*turns: tuple[int, float]) -> list[Bar]:
    """Straight moves through (candles, price) turning points, starting at 100."""
    out, price = [], 100.0
    for n, target in turns:
        for k in range(1, n + 1):
            c = price + (target - price) * k / n
            out.append(bar(len(out), out[-1].c if out else 100.0, c))
        price = target
    return out


def random_bars(seed: int, n: int = 500) -> list[Bar]:
    r, c, out = random.Random(seed), 100.0, []
    for i in range(n):
        o, c = c, c * (1 + r.gauss(0, 0.002 if (i // 70) % 2 else 0.0008))
        out.append(bar(i, o, c, abs(r.gauss(0, 0.08)), abs(r.gauss(0, 0.08))))
    return out


def test_breaks_go_with_the_trend_or_change_its_character():
    bars = path((8, 104), (8, 101), (10, 107), (8, 104), (14, 95))
    inner = [s for s in smart_money(bars).structures if s.internal]
    up = next(s for s in inner if s.side == "long")
    down = next(s for s in inner if s.side == "short")
    assert up.kind == "BOS" and up.level == bars[up.pivot].h and bars[up.index].c > up.level >= bars[up.index - 1].c
    assert down.kind == "CHoCH" and down.index > up.index and down.level == bars[down.pivot].l  # it turned the trend
    again = [s for s in inner if s.side == "short"]
    assert all(s.kind == "BOS" for s in again[1:])  # and from there goes with it
    assert smart_money(bars).internal_bias == -1


def test_an_order_block_stands_until_price_trades_through_it():
    bars = path((8, 104), (8, 101), (10, 107))
    m = smart_money(bars)
    block = next(b for b in m.blocks if b.internal and b.side == "long")
    assert block.bottom == min(b.l for b in bars[8:26]) and block.top >= block.bottom
    through = [*bars, bar(len(bars), 107, block.bottom - 1)]
    assert block not in smart_money(through).blocks


def test_equal_highs_need_two_pivots_almost_level():
    warm = [bar(i, 100, 100 + 0.2 * (-1) ** i, 0.3, 0.3) for i in range(210)]  # for the average range

    def tail(second: float) -> list[Bar]:
        out = list(warm)
        for target in (103, 101, second, 101):
            start = out[-1].c
            for k in range(1, 7):
                out.append(bar(len(out), out[-1].c, start + (target - start) * k / 6))
        return out

    assert any(e.side == "high" and e.first < e.second < e.known for e in smart_money(tail(103.005)).equals)
    assert not any(e.side == "high" and e.second > 215 for e in smart_money(tail(104)).equals)


def test_a_fair_value_gap_closes_when_price_comes_back():
    bars = [bar(i, 100, 100 + 0.05 * (-1) ** i) for i in range(30)]
    bars += [bar(30, 100, 103), bar(31, 103, 103.2)]
    gap = smart_money(bars).gaps[0]
    assert (gap.index, gap.side) == (30, "long") and gap.top == bars[31].l and gap.bottom == bars[29].h
    assert smart_money([*bars, bar(32, 103, gap.bottom - 0.1)]).gaps == []


def test_signals_never_change_as_candles_are_added():
    for seed in range(8):
        bars = random_bars(seed)
        whole = smart_money(bars)
        for cut in (180, 330, 499):
            part = smart_money(bars[:cut])
            assert part.structures == [s for s in whole.structures if s.index < cut]
            assert part.equals == [e for e in whole.equals if e.known < cut]


def test_zones_split_the_swing_range():
    m = next(m for m in map(smart_money, (random_bars(s) for s in range(20))) if m.top and m.bottom)
    z = m.zones()
    assert z["premium"][0] == m.top and z["discount"][1] == m.bottom
    assert z["premium"][1] > z["equilibrium"][0] > (m.top + m.bottom) / 2 > z["equilibrium"][1] > z["discount"][0]
    assert smart_money([]).zones() == {} and smart_money(path((3, 101))).structures == []


def test_a_break_by_the_overnight_gap_is_marked():
    bars = path((8, 104), (8, 101), (6, 103.5))
    nxt = [bar(0, 106, 106.5, day=27)]
    broken = [s for s in smart_money(bars + nxt).structures if s.index == len(bars)]
    assert broken and all(s.gap for s in broken)
    inside = [s for s in smart_money([*bars, bar(len(bars), 103.5, 106.5)]).structures if s.index == len(bars)]
    assert inside and not any(s.gap for s in inside)


def test_the_scanner_shows_todays_smart_money_and_it_can_be_switched_off(cfg):
    bars = five_minute_bars(walk(3, 156))
    cfg.scan.chart_sessions = 0
    clock = [at(TODAY, 78) + timedelta(seconds=10)]
    scanner, rec = make_scanner(cfg, FakeSource({"SPY": bars}, []), lambda: clock[0])
    r = scanner.scan()
    m, whole = scanner.view["SPY"]["smc"], smart_money(bars)
    today = {int(at(TODAY, k).timestamp()) for k in range(78)}
    assert m["structure"] and all(x["t"] in today for x in m["structure"] + [{"t": e["known"]} for e in m["equal"]])
    assert [x["t"] for x in m["structure"]] == [int(bars[s.index].t.timestamp()) for s in whole.structures if bars[s.index].t.date() == TODAY]
    assert len(m["blocks"]) <= cfg.scan.smc_blocks and (m["swing"], m["internal"]) == (whole.swing_bias, whole.internal_bias)
    assert not [f for f in r.found if f.signal.kind.lower() in ("bos", "choch", "smc")]  # shown only
    cfg.scan.smc = False
    scanner, rec = make_scanner(cfg, FakeSource({"SPY": bars}, []), lambda: clock[0])
    scanner.scan()
    assert "smc" not in scanner.view["SPY"]


def test_the_charts_show_the_sessions_before_with_their_overlays(cfg):
    bars = five_minute_bars(walk(3, 156))
    clock = [at(TODAY, 78) + timedelta(seconds=10)]
    scanner, rec = make_scanner(cfg, FakeSource({"SPY": bars}, []), lambda: clock[0])
    scanner.scan()
    view = scanner.view["SPY"]
    stamp = [int(b.t.timestamp()) for b in bars]
    assert [b[0] for b in view["earlier"]] == stamp[:78] and [b[0] for b in view["bars"]] == stamp[78:]
    whole = smart_money(bars)
    assert [x["t"] for x in view["smc"]["structure"]] == [stamp[s.index] for s in whole.structures]  # both days'
    assert view["sr"]["levels"][0][0] == stamp[0]
    cfg.scan.chart_sessions = 0
    scanner, rec = make_scanner(cfg, FakeSource({"SPY": bars}, []), lambda: clock[0])
    scanner.scan()
    view = scanner.view["SPY"]
    assert view["earlier"] == [] and all(x["t"] >= stamp[78] for x in view["smc"]["structure"] + view["qqe"])


def test_the_overlays_reach_back_further_than_the_scan(cfg, tmp_path):
    minutes = minute_bars(PREV, 390, walk(1, 390)) + minute_bars(TODAY, 60, walk(2, 60))
    source = MinuteSource({"SPY": breakout_bars()}, minutes)
    old = date(2026, 10, 20)
    source.history = {"SPY": [Bar(datetime(2026, 10, 20, 9, 30, tzinfo=TZ) + timedelta(minutes=5 * k), c, c + 0.2, c - 0.2, c, 1000)
                              for k, c in enumerate(walk(5, 78))]}
    scanner, clock, chart = setup(cfg, tmp_path, source, at(TODAY, 11) + timedelta(seconds=5))
    scanner.scan()
    (syms, minutes_, start, extended), = source.history_calls
    assert (syms, minutes_, extended) == (("SPY",), cfg.scan.bar_minutes, False) and start.date() < old
    joined = scanner._with_history("SPY", breakout_bars())
    assert joined[0].t.date() == old and [b.t for b in joined] == sorted(b.t for b in joined)
    scanner.scan()
    assert len(source.history_calls) == 1  # once a day
    assert "smc" in json.loads(chart.read_text())["symbols"]["SPY"]

    failing = MinuteSource({"SPY": breakout_bars()}, [], fail=True)
    scanner, clock, _ = setup(cfg, tmp_path / "b", failing, at(TODAY, 11) + timedelta(seconds=5))
    assert "breakout" in {f.signal.kind for f in scanner.scan().found} or scanner.view["SPY"]["smc"] is not None
    assert list(scanner.journal.events(TODAY, "history_failed"))
