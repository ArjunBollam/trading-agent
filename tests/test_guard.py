"""The position guard (paperbot/guard.py): your positions against the lines on their symbol."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from paperbot.features import Bar
from paperbot.guard import Guard, check, positions
from paperbot.trendlines import Line

TZ = ZoneInfo("America/New_York")
T0 = datetime(2026, 11, 27, 9, 50, tzinfo=TZ)
STATE = {"holdings": [], "options": [{"symbol": "SPY", "right": "call", "strike": 729.0, "expiration": "2026-11-27", "qty": 6.0,
                                      "mark": 0.3, "pnl": -40.0}]}


def bars(closes, lows=None, highs=None):
    out = []
    for i, c in enumerate(closes):
        lo = lows[i] if lows else c - 0.1
        hi = highs[i] if highs else c + 0.1
        out.append(Bar(T0 + timedelta(minutes=i), c, max(hi, c), min(lo, c), c, 1000))
    return out


FLOOR = Line("low", "hray", T0 - timedelta(hours=1), 720.50, T0 - timedelta(hours=1), 720.50, "24h", True, "Today's low 720.50")
DEEPER = Line("o", "hline", T0, 720.40, T0, 720.40, "24h", True, "720.40 overnight low")
TARGET = Line("t", "hline", T0, 727.80, T0, 727.80, "24h", True, "727.80")
DOWN = Line("d", "trend", T0 - timedelta(minutes=20), 726.0, T0, 722.0, "24h", True, "1m down line")  # falling 0.2 a minute


def test_calls_are_bullish_and_puts_bearish():
    p = positions(STATE | {"options": STATE["options"] + [{"symbol": "QQQ", "right": "put", "strike": 600, "expiration": "2026-11-27",
                                                            "qty": 2, "mark": 1, "pnl": 3}]})
    assert [(x.symbol, x.bullish, x.label) for x in p] == [("SPY", True, "6× SPY 729 call 11-27"), ("QQQ", False, "2× QQQ 600 put 11-27")]


def test_the_floor_breaking_is_the_exit_signal():
    pos = positions(STATE)[0]
    status, events = check(pos, bars([721.0, 720.3]), [FLOOR, DEEPER, TARGET, DOWN])
    assert [(e["kind"], e["line"]) for e in events] == [("exit", "low"), ("against", "o")]  # 720.50 was the nearest floor
    assert "the 09:51 candle closed at 720.30, below Today's low 720.50 (720.50 there)" in events[0]["text"]
    _, deeper = check(pos, bars([720.45, 720.3]), [FLOOR, DEEPER])
    assert [e["kind"] for e in deeper] == ["exit"]  # once 720.50 is gone, 720.40 is the floor


def test_a_lower_line_breaking_first_is_against_not_exit():
    pos = positions(STATE)[0]
    rising = Line("r", "trend", T0 - timedelta(minutes=10), 720.0, T0, 721.0, "24h", True, "up line")  # 721.1 at 09:51
    _, events = check(pos, bars([722.0, 721.05]), [FLOOR, rising])
    assert [(e["kind"], e["line"]) for e in events] == [("exit", "r")]  # the rising line was the nearest floor
    _, events = check(pos, bars([722.0, 720.3]), [FLOOR, rising])
    assert sorted((e["kind"], e["line"]) for e in events) == [("against", "low"), ("exit", "r")]


def test_a_falling_line_is_never_the_floor_under_calls():
    pos = positions(STATE)[0]
    status, events = check(pos, bars([722.0, 721.6]), [FLOOR, DOWN])  # DOWN is at 721.8 at 09:51
    assert [e["kind"] for e in events] == ["failed"] and "back below 1m down line" in events[0]["text"]
    assert status["against"]["id"] == "low" and status["against"]["distance"] == 1.1


def test_target_zone_says_whether_it_reached():
    pos = positions(STATE)[0]
    status, events = check(pos, bars([727.5, 727.6], highs=[727.6, 727.75]), [FLOOR, TARGET])
    assert events[-1]["kind"] == "target" and "came within 0.05 of 727.80" in events[-1]["text"]
    _, events = check(pos, bars([727.5, 727.6], highs=[727.6, 727.9]), [FLOOR, TARGET])
    assert "reached 727.80" in events[-1]["text"]
    assert status["ahead"]["id"] == "t"


def test_puts_are_mirrored():
    put = positions({"options": [{"symbol": "SPY", "right": "put", "strike": 715, "expiration": "2026-11-27", "qty": 1, "mark": 1, "pnl": 0}]})[0]
    ceiling = Line("c", "hline", T0, 722.0, T0, 722.0, "24h", True, "722.00")
    _, events = check(put, bars([721.8, 722.2]), [ceiling])
    assert [e["kind"] for e in events] == ["exit"] and "above 722.00" in events[0]["text"]


def test_a_pass_logs_once_and_pushes_the_warnings(tmp_path):
    sent = []
    g = Guard(tmp_path)
    candles = bars([721.0, 720.3, 720.2])
    now = T0 + timedelta(minutes=2, seconds=25)  # the 09:52 candle is still forming: only 09:50 and 09:51 (settled) count
    new = g.run(STATE, lambda s: candles, lambda s: [{"id": "low", "kind": "hray", "t1": (T0 - timedelta(hours=1)).timestamp(),
                                                       "p1": 720.5, "clock": "24h", "name": "Today's low 720.50"}],
                TZ, now, lambda title, msg, pri: sent.append((title, pri)))
    assert [e["kind"] for e in new] == ["exit"] and sent == [("EXIT signal: SPY", "high")]
    assert g.run(STATE, lambda s: candles, lambda s: [], TZ, now) == []  # no lines: nothing new, nothing repeated
    saved = g.load()
    assert saved["positions"][0]["position"] == "6× SPY 729 call 11-27" and saved["events"][0]["kind"] == "exit"


def test_a_floor_that_just_broke_is_not_a_target():
    pos = positions(STATE)[0]
    candles = bars([721.0, 720.3, 720.35], highs=[721.1, 720.4, 720.47])  # 720.50 broke, then the next high came back near it
    status, events = check(pos, candles, [FLOOR, TARGET])
    assert [e["kind"] for e in events] == [] and status["ahead"]["id"] == "t"  # still 727.80, not the broken 720.50


FLOOR_JSON = {"id": "low", "kind": "hray", "t1": (T0 - timedelta(hours=1)).timestamp(), "p1": 720.5, "clock": "24h",
              "name": "Today's low 720.50"}
TARGET_JSON = {"id": "t", "kind": "hline", "t1": T0.timestamp(), "p1": 727.8, "clock": "24h", "name": "727.80"}
WINNING = {"holdings": [], "options": [{"symbol": "SPY", "right": "call", "strike": 729.0, "expiration": "2026-11-27", "qty": 12.0,
                                        "mark": 0.245, "pnl": 29.92, "pnl_pct": 11.3}]}
HALF_DAY_CLOSE = datetime(2026, 11, 27, 13, 0, tzinfo=TZ)


def run(g, closes, now, sent, lines=(FLOOR_JSON, TARGET_JSON), state=WINNING, close_at=HALF_DAY_CLOSE):
    candles = bars(closes)
    return g.run(state, lambda s: candles, lambda s: list(lines), TZ, now, lambda title, msg, pri: sent.append((title, msg, pri)),
                 close_at)


def test_hold_says_why_and_what_is_left_on_the_last_day(tmp_path):
    g, sent = Guard(tmp_path), []
    now = T0 + timedelta(minutes=3, seconds=30)  # 09:53:30: the 09:50 to 09:52 candles are settled
    new = run(g, [722.0, 722.6, 723.03, 723.1], now, sent)
    assert [e["kind"] for e in new] == ["hold"]
    v = g.load()["positions"][0]["verdict"]
    assert v["kind"] == "hold" and not v["near"]
    assert v["text"].startswith("HOLD by the video's rule: the 09:52 candle closed at 723.03, 2.53 above Today's low 720.50, "
                                "the line under your 12× SPY 729 call 11-27.")
    assert "Next level: 727.80, 4.77 away" in v["text"]
    assert "Up $29.92 (+11.3%) at Robinhood's last mark." in v["text"]
    assert v["text"].endswith("These expire today: the market closes at 13:00 ET (3h 06m left), and SPY is 5.97 below the 729 "
                              "strike (out of the money).")
    assert sent == [("HOLD: 12× SPY 729 call 11-27", new[0]["text"], "default")]
    saved = g.load()["positions"][0]
    assert saved["expires_today"] and saved["close"] == "13:00"


def test_hold_checks_in_every_quarter_hour_of_regular_hours_only(tmp_path):
    g, sent = Guard(tmp_path), []
    closes = [722.0 + 0.01 * i for i in range(40)]
    at = lambda m, s=30: T0 + timedelta(minutes=m, seconds=s)  # noqa: E731
    assert [e["kind"] for e in run(g, closes, at(10), sent)] == ["hold"]  # 10:00:30, the first pass
    assert run(g, closes, at(14), sent) == []  # same quarter hour: quiet
    assert [e["kind"] for e in run(g, closes, at(25), sent)] == ["hold"]  # 10:15:30, the next quarter hour
    assert [t for t, _, _ in sent] == ["HOLD: 12× SPY 729 call 11-27"] * 2
    # on a half day nothing after the 13:00 close, and nothing before 9:30
    late = datetime(2026, 11, 27, 13, 5, tzinfo=TZ)
    candles = [Bar(late - timedelta(minutes=4 - i), 723, 723.1, 722.9, 723, 1000) for i in range(3)]
    assert g.run(WINNING, lambda s: candles, lambda s: [FLOOR_JSON], TZ, late, None, HALF_DAY_CLOSE) == []
    early = datetime(2026, 11, 27, 9, 20, tzinfo=TZ)
    candles = [Bar(early - timedelta(minutes=4 - i), 723, 723.1, 722.9, 723, 1000) for i in range(3)]
    assert g.run(WINNING, lambda s: candles, lambda s: [FLOOR_JSON], TZ, early, None, HALF_DAY_CLOSE) == []


def test_no_check_in_on_stale_candles_or_without_a_floor(tmp_path):
    g, sent = Guard(tmp_path), []
    assert run(g, [722.0, 722.5, 723.0], T0 + timedelta(minutes=8), sent) == []  # the last candle is 5 minutes old
    assert run(g, [722.0, 722.5, 723.0], T0 + timedelta(minutes=3, seconds=30), sent, lines=[TARGET_JSON]) == []
    v = g.load()["positions"][0]["verdict"]
    assert v["kind"] == "no_floor" and v["text"].startswith("No line under your 12× SPY 729 call 11-27")


def test_an_exit_stands_until_a_close_back_over_the_line(tmp_path):
    g, sent = Guard(tmp_path), []
    at = lambda m: T0 + timedelta(minutes=m, seconds=30)  # noqa: E731
    deeper = {"id": "o", "kind": "hline", "t1": T0.timestamp(), "p1": 719.0, "clock": "24h", "name": "719.00"}
    lines = (FLOOR_JSON, TARGET_JSON, deeper)
    new = run(g, [721.0, 720.3, 720.2], at(2), sent, lines)  # 09:51 closes under 720.50
    assert [e["kind"] for e in new] == ["exit"]
    v = g.load()["positions"][0]["verdict"]
    assert v["kind"] == "exit" and v["text"].startswith("EXIT stands for your 12× SPY 729 call 11-27: the 09:51 candle closed below "
                                                        "Today's low 720.50 and no 1-minute candle has closed back above it since")
    # still under it a candle later: no HOLD against the deeper 719.00 line, no check-in
    assert run(g, [721.0, 720.3, 720.2, 720.1], at(3), sent, lines) == []
    assert g.load()["positions"][0]["verdict"]["kind"] == "exit"
    # back over 720.50: the EXIT no longer stands, one "with you" push, and HOLD again
    new = run(g, [721.0, 720.3, 720.2, 720.1, 720.9, 721.0], at(5), sent, lines)
    kinds = [e["kind"] for e in new]
    assert kinds == ["with", "hold"]
    assert new[0]["text"].endswith("The EXIT from 09:51 no longer stands by the rule.")
    assert g.load()["positions"][0]["verdict"]["kind"] == "hold"
    assert [t for t, _, _ in sent] == ["EXIT signal: SPY", "With you: SPY", "HOLD: 12× SPY 729 call 11-27"]


def test_with_you_pushes_once_for_a_candle_that_crossed_several_lines(tmp_path):
    g, sent = Guard(tmp_path), []
    a = {"id": "a", "kind": "hline", "t1": T0.timestamp(), "p1": 721.5, "clock": "24h", "name": "721.50"}
    b = {"id": "b", "kind": "hline", "t1": T0.timestamp(), "p1": 721.8, "clock": "24h", "name": "721.80"}
    new = run(g, [721.0, 722.0, 722.1], T0 + timedelta(minutes=2, seconds=30), sent, (FLOOR_JSON, a, b))
    assert [e["kind"] for e in new].count("with") == 2
    withs = [m for t, m, _ in sent if t == "With you: SPY"]
    assert len(withs) == 1 and withs[0].endswith("(and 1 more line)")


def test_hold_close_to_the_floor_says_so(tmp_path):
    g, sent = Guard(tmp_path), []
    run(g, [720.9, 720.7, 720.6, 720.6], T0 + timedelta(minutes=3, seconds=30), sent)  # 09:52 closes 0.10 over 720.50; candles 0.20 tall
    v = g.load()["positions"][0]["verdict"]
    assert v["kind"] == "hold" and v["near"] and "Close to it: that's less than one average 1-minute candle" in v["text"]
    assert sent[0][0] == "HOLD: 12× SPY 729 call 11-27 (close to the floor)"


def test_phone_titles_are_ascii_for_ntfy():
    from paperbot.notify import ascii_title

    assert ascii_title("HOLD: 5× SPY 729 call 11-27") == "HOLD: 5x SPY 729 call 11-27"
    assert ascii_title("Target → 727.80 ✓").isascii()
