"""Claude's read on your positions (paperbot/coach.py), with a fake client: what it's shown, how its answer is checked,
when it's asked, what it costs and when the phone hears about it."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from paperbot.coach import (FALLBACK_BETA, SCHEMA, ClaudeReader, Coach, CoachError, OpenAIReader, context, cost,
                            five_minutes, note_for, parse_json, validate)
from paperbot.features import Bar
from paperbot.guard import Guard

from test_guard import FLOOR_JSON, HALF_DAY_CLOSE, WINNING, bars as guard_bars

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 27, 11, 0, 30, tzinfo=TZ)
POS = {"position": "2× SPY 729 call 11-27", "symbol": "SPY", "bullish": True, "pnl": 15.0, "mark": 0.18,
       "verdict": {"kind": "hold", "text": "HOLD by the video's rule: ..."}}


def candles(n=90, start=725.0):
    t0 = NOW.replace(second=0) - timedelta(minutes=n)
    return [Bar(t0 + timedelta(minutes=i), start + i * 0.01, start + i * 0.01 + 0.05, start + i * 0.01 - 0.05,
                start + i * 0.01 + 0.02, 1000 + i) for i in range(n)]


class FakeClient:
    """client.beta.messages.create(**kw): records the request, answers with the given JSON."""

    def __init__(self, *answers, stop="end_turn"):
        self.answers, self.stop, self.calls = list(answers), stop, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        text = self.answers.pop(0)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text if isinstance(text, str) else json.dumps(text))],
                               stop_reason=self.stop, model=kw["model"],
                               usage=SimpleNamespace(input_tokens=3000, output_tokens=1500, cache_read_input_tokens=1000,
                                                     cache_creation_input_tokens=0))


def answer(lean="favors", candle="10:58", pattern=("10:40", "10:55"), watch=726.0, position=POS["position"]):
    return {"positions": [{"position": position, "lean": lean, "confidence": "medium", "headline": f"Chart {lean}",
                           "points": [{"side": "for", "text": "higher lows", "candle": candle},
                                      {"side": "against", "text": "under 727.80", "candle": ""}]}],
            "patterns": [{"name": "rising channel", "from": pattern[0], "to": pattern[1], "meaning": "trend up"}],
            "watch": [{"price": watch, "why": "the up line"}]}


def shown():
    return context("SPY", [POS], candles(), {"trend": "up", "rsi": 60.0, "vwap": 724.0, "levels": [{"name": "VWAP", "price": 724.0}],
                                             "context": {"bias": "bullish", "score": 3, "factors": [{"side": 1, "text": "above VWAP"}]}},
                   {"qqe": [{"t": int((NOW - timedelta(minutes=20)).timestamp()), "side": "long"}]},
                   [FLOOR_JSON], [], [], NOW, HALF_DAY_CLOSE, TZ)


def test_claude_is_shown_the_candles_positions_lines_and_signals():
    text, allowed, span = shown()
    assert '"2× SPY 729 call 11-27" (gains if price rises): up $15.00' in text
    assert "Today the regular session closes at 13:00 ET (1h 59m left)." in text
    assert "1-minute candles, last hour" in text and "10:59 " in text and "Today's low 720.50: 720.50" in text
    assert "10:40 QQE long" in text and "Bigger picture: bullish (score 3):" in text
    assert "10:59" in allowed and "09:30" in allowed and "09:31" not in allowed  # the last hour of 1-minute candles, 5-minute starts
    assert span == (pytest.approx(724.95), pytest.approx(725.94))  # the lowest low and highest high today


def test_five_minute_candles_from_one_minute_ones():
    five = five_minutes(candles(12))
    assert [b.t.minute % 5 for b in five] == [0] * len(five)
    assert five[0].v == sum(b.v for b in candles(12) if b.t < five[1].t)


def test_what_cant_be_checked_against_the_data_is_dropped():
    _, allowed, span = shown()
    clean, dropped = validate(answer(), [POS["position"]], allowed, span)
    assert dropped == 0 and clean["positions"][0]["points"][0]["candle"] == "10:58"
    bad = answer(candle="09:12", pattern=("08:00", "10:55"), watch=650.0)
    bad["positions"].append(answer(position="5× QQQ 600 put")["positions"][0])
    clean, dropped = validate(bad, [POS["position"]], allowed, span)
    assert dropped == 4  # a candle not sent, a pattern from one, a price far outside the day, a position not held
    assert [x["text"] for x in clean["positions"][0]["points"]] == ["under 727.80"] and not clean["patterns"]
    assert not clean["watch"] and len(clean["positions"]) == 1


def test_a_read_asks_claude_with_the_schema_and_the_fallback(tmp_path):
    text, allowed, span = shown()
    client, sent = FakeClient(answer()), []
    read = Coach(tmp_path).run("SPY", [POS], text, allowed, span, ClaudeReader(client, "claude-opus-5-5", "medium"), NOW, "first read",
                               lambda *a: sent.append(a))
    kw = client.calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["output_config"]["effort"] == "medium"
    assert kw["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}
    assert kw["betas"] == [FALLBACK_BETA] and kw["fallbacks"] == "default"
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"} and kw["messages"][0]["content"] == text
    assert read["usd"] == pytest.approx((3000 * 4 + 1500 * 20 + 1000 * 0.2) / 1e6)
    saved = Coach(tmp_path).load()
    assert saved["spend"] == {"day": "2026-11-27", "reads": 1, "usd": round(read["usd"], 4), "tokens": {"claude-opus-5-5": 5500}}
    assert saved["status"] is None
    assert sent == []  # the first read has nothing to change from


def test_the_phone_hears_when_a_lean_changes(tmp_path):
    text, allowed, span = shown()
    coach, sent = Coach(tmp_path), []
    client = FakeClient(answer("favors"), answer("favors"), answer("against"))
    for i in range(3):
        coach.run("SPY", [POS], text, allowed, span, ClaudeReader(client, "claude-opus-5-5", "medium"), NOW + timedelta(minutes=5 * i),
                  "every 5 minutes", lambda *a: sent.append(a))
    assert len(sent) == 1
    title, msg, pri = sent[0]
    assert title == "Claude: SPY chart now goes against 2x SPY 729 call 11-27" and pri == "high"
    assert msg.startswith("Chart against (medium confidence, 11:10 ET).") and "+ higher lows" in msg
    assert coach.load()["spend"]["reads"] == 3


def test_a_bad_answer_is_an_error_not_a_read(tmp_path):
    text, allowed, span = shown()
    with pytest.raises(CoachError, match="stopped early"):
        Coach(tmp_path).run("SPY", [POS], text, allowed, span,
                            ClaudeReader(FakeClient(answer(), stop="max_tokens"), "claude-opus-5-5", "medium"), NOW, "x")
    with pytest.raises(CoachError, match="valid JSON"):
        Coach(tmp_path).run("SPY", [POS], text, allowed, span, ClaudeReader(FakeClient("not json"), "claude-opus-5-5", "medium"), NOW, "x")
    assert Coach(tmp_path).load()["symbols"] == {}


def test_when_a_read_is_due(tmp_path):
    coach = Coach(tmp_path)
    assert coach.due("SPY", NOW, 5, []) == "first read"
    text, allowed, span = shown()
    coach.run("SPY", [POS], text, allowed, span, ClaudeReader(FakeClient(answer()), "claude-opus-5-5", "medium"), NOW, "first read")
    exit_at = (NOW + timedelta(minutes=2)).isoformat()
    event = {"symbol": "SPY", "kind": "exit", "at": exit_at, "t": 0}
    assert coach.due("SPY", NOW + timedelta(seconds=60), 5, [event]) is None  # never twice within 90 seconds
    assert coach.due("SPY", NOW + timedelta(minutes=3), 5, []) is None
    assert coach.due("SPY", NOW + timedelta(minutes=3), 5, [event]) == "after exit"  # the guard flagged something
    assert coach.due("SPY", NOW + timedelta(minutes=3), 5, [event | {"kind": "hold"}]) is None
    assert coach.due("SPY", NOW + timedelta(minutes=5), 5, []) == "every 5 minutes"


def test_hold_check_ins_carry_claudes_latest_read(tmp_path):
    text, allowed, span = shown()
    Coach(tmp_path).run("SPY", [POS], text, allowed, span, ClaudeReader(FakeClient(answer()), "claude-opus-5-5", "medium"), NOW, "x")
    note = note_for(tmp_path, "SPY", POS["position"], NOW + timedelta(minutes=4))
    assert note == " Claude's read at 11:00: favors it (medium confidence): Chart favors"
    assert note_for(tmp_path, "SPY", POS["position"], NOW + timedelta(minutes=20)) == ""  # too old to pass on
    assert note_for(tmp_path, "SPY", "1× SPY 730 call 11-27", NOW) == ""
    # the guard adds it to its HOLD check-in
    sent = []
    t0 = datetime(2026, 11, 27, 9, 50, tzinfo=TZ)
    g = Guard(tmp_path / "g")
    (tmp_path / "g").mkdir()
    new = g.run(WINNING, lambda s: guard_bars([722.0, 722.6, 723.03, 723.1]), lambda s: [FLOOR_JSON], TZ,
                t0 + timedelta(minutes=3, seconds=30), lambda *a: sent.append(a), HALF_DAY_CLOSE,
                lambda sym, label: " Claude's read: favors it.")
    assert new[0]["kind"] == "hold" and new[0]["text"].endswith("Claude's read: favors it.")


def test_cost_by_model():
    used = {"input_tokens": 1_000_000, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    assert cost("claude-opus-5-5", used) == 4.0 and cost("claude-sonnet-5-5", used) == 2.0


def test_the_coach_endpoint(web, tmp_path):  # noqa: F811
    r = web.get("/api/coach")
    assert r.status_code == 200 and r.json()["symbols"] == {}


from test_chartdata import web  # noqa: E402,F401 - the web fixture


def _api_error(cls, status, message):
    import httpx2

    import anthropic

    resp = httpx2.Response(status, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    return getattr(anthropic, cls)(message, response=resp, body=None)


class FailingClient(FakeClient):
    def __init__(self, error):
        super().__init__()
        self.error = error

    def create(self, **kw):
        self.calls.append(kw)
        raise self.error


def test_no_credit_says_so_plainly_and_waits(tmp_path):
    from paperbot.coach import NO_CREDIT

    text, allowed, span = shown()
    coach = Coach(tmp_path)
    err = _api_error("BadRequestError", 400, "Your credit balance is too low to access the Anthropic API.")
    with pytest.raises(CoachError) as e:
        coach.run("SPY", [POS], text, allowed, span, ClaudeReader(FailingClient(err), "claude-opus-5-5", "medium"), NOW, "x")
    assert str(e.value) == NO_CREDIT and e.value.wait == 300
    coach.status(str(e.value), e.value.wait, NOW)
    assert coach.waiting(NOW + timedelta(minutes=4)) and not coach.waiting(NOW + timedelta(minutes=5, seconds=1))
    # a read that works clears it
    coach.run("SPY", [POS], text, allowed, span, ClaudeReader(FakeClient(answer()), "claude-opus-5-5", "medium"), NOW + timedelta(minutes=6), "x")
    saved = coach.load()
    assert saved["status"] is None and saved["retry_at"] is None


def test_a_refused_key_waits_longer(tmp_path):
    text, allowed, span = shown()
    err = _api_error("AuthenticationError", 401, "invalid x-api-key")
    with pytest.raises(CoachError, match="refused the API key") as e:
        Coach(tmp_path).run("SPY", [POS], text, allowed, span, ClaudeReader(FailingClient(err), "claude-opus-5-5", "medium"), NOW, "x")
    assert e.value.wait == 600


def test_env_values_are_read_as_they_are_now(tmp_path):
    from paperbot.web.server import _env_value

    env = tmp_path / ".env"
    env.write_text("ALPACA_API_KEY=a\nANTHROPIC_API_KEY=sk-ant-new\n")
    assert _env_value(env, "ANTHROPIC_API_KEY") == "sk-ant-new" and _env_value(env, "MISSING") == ""


class FakeOpenAI:
    """client.chat.completions.create(**kw), as Groq answers it."""

    def __init__(self, *answers, finish="stop", error=None):
        self.answers, self.finish, self.error, self.calls = list(answers), finish, error, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        text = self.answers.pop(0)
        return SimpleNamespace(model=kw["model"], usage=SimpleNamespace(prompt_tokens=3100, completion_tokens=900),
                               choices=[SimpleNamespace(finish_reason=self.finish, message=SimpleNamespace(
                                   content=text if isinstance(text, str) else json.dumps(text)))])


def _openai_error(cls, status, message):
    import httpx2
    import openai

    resp = httpx2.Response(status, request=httpx2.Request("POST", "https://api.groq.com/openai/v1/chat/completions"))
    return getattr(openai, cls)(message, response=resp, body=None)


def test_a_free_groq_read(tmp_path):
    text, allowed, span = shown()
    client, sent = FakeOpenAI(answer("favors"), answer("against")), []
    reader = OpenAIReader(client, "groq", "openai/gpt-oss-20b", "medium")
    coach = Coach(tmp_path)
    read = coach.run("SPY", [POS], text, allowed, span, reader, NOW, "first read", lambda *a: sent.append(a))
    kw = client.calls[0]
    assert kw["model"] == "openai/gpt-oss-20b" and kw["reasoning_effort"] == "medium"
    assert kw["response_format"]["json_schema"]["strict"] is True and kw["response_format"]["json_schema"]["schema"] == SCHEMA
    assert kw["messages"][0]["role"] == "system" and kw["messages"][1]["content"] == text
    assert read["by"] == "Groq" and read["free"] and read["usd"] == 0 and read["positions"][0]["lean"] == "favors"
    coach.run("SPY", [POS], text, allowed, span, reader, NOW + timedelta(minutes=10), "every 10 minutes", lambda *a: sent.append(a))
    assert sent[0][0] == "Groq: SPY chart now goes against 2x SPY 729 call 11-27"
    assert coach.load()["spend"]["tokens"] == {"openai/gpt-oss-20b": 8000}
    note = note_for(tmp_path, "SPY", POS["position"], NOW + timedelta(minutes=12))
    assert note.startswith(" Groq's read at 11:10: goes against it")


def test_qwen_keeps_its_thinking_out_and_isnt_held_to_a_strict_schema():
    text, *_ = shown()
    client = FakeOpenAI("Here it is:\n```json\n" + json.dumps(answer()) + "\n```")
    got, used, model, usd = OpenAIReader(client, "groq", "qwen/qwen3.8-27b", "medium").read(text)
    kw = client.calls[0]
    assert kw["extra_body"] == {"reasoning_format": "hidden"} and "reasoning_effort" not in kw
    assert kw["response_format"]["json_schema"]["strict"] is False
    assert got["positions"][0]["lean"] == "favors" and used == {"input_tokens": 3100, "output_tokens": 900}


def test_a_model_out_of_free_allowance_hands_over_to_the_next(tmp_path):
    text, allowed, span = shown()
    err = _openai_error("RateLimitError", 429, "Rate limit reached for model openai/gpt-oss-20b on tokens per day (TPD)")
    with pytest.raises(CoachError) as e:
        Coach(tmp_path).run("SPY", [POS], text, allowed, span,
                            OpenAIReader(FakeOpenAI(error=err), "groq", "openai/gpt-oss-20b", "medium"), NOW, "x")
    assert e.value.spent == "openai/gpt-oss-20b"
    coach = Coach(tmp_path)
    models = ["openai/gpt-oss-20b", "qwen/qwen3.8-27b"]
    assert coach.usable(models, NOW, 120000) == "openai/gpt-oss-20b"
    coach.mark_spent("openai/gpt-oss-20b", NOW)
    assert coach.usable(models, NOW, 120000) == "qwen/qwen3.8-27b"
    assert coach.usable(models, NOW + timedelta(days=1), 120000) == "openai/gpt-oss-20b"  # a new day
    # our own cap counts too: past 120K tokens today, the next model
    coach.run("SPY", [POS], text, allowed, span, OpenAIReader(FakeOpenAI(answer()), "groq", "qwen/qwen3.8-27b", "medium"),
              NOW, "x")
    assert coach.usable(models, NOW, 4000) is None
    # a per-minute limit just waits a minute
    busy = _openai_error("RateLimitError", 429, "Rate limit reached on tokens per minute (TPM)")
    with pytest.raises(CoachError) as e:
        OpenAIReader(FakeOpenAI(error=busy), "groq", "qwen/qwen3.8-27b", "medium").read(text)
    assert e.value.spent is None and e.value.wait == 60


def test_answers_out_of_shape_are_dropped_not_crashed_on():
    _, allowed, span = shown()
    odd = {"positions": [{"position": POS["position"], "lean": "bullish", "points": []},
                         {"position": POS["position"], "lean": "favors", "confidence": "very",
                          "points": [{"side": "maybe", "text": "x"}, "not a point", {"side": "for", "text": "ok", "candle": "10:59"}]}],
           "patterns": ["nope"], "watch": [{"price": "n/a"}]}
    clean, dropped = validate(odd, [POS["position"]], allowed, span)
    assert dropped == 5 and clean["positions"][0]["confidence"] == "low"
    assert clean["positions"][0]["points"] == [{"side": "for", "text": "ok", "candle": "10:59"}]
    with pytest.raises(CoachError, match="valid JSON"):
        parse_json("no json here", "Groq")


def test_a_point_its_own_numbers_contradict_is_dropped():
    from paperbot.coach import contradicts

    # Groq's first live read said "Closed 726.57 below 5m down line 726.31": 726.57 is above 726.31
    assert contradicts("Closed 726.57 below 5m down line 726.31 - failed breakout", {726.57})
    assert not contradicts("Closed 726.18 below 1m up line 11:27 (726.49) - exit trigger", {726.18})
    assert contradicts("Closed 726.30 above Today's low 720.50", {726.18})  # not that candle's close
    assert not contradicts("closes at 727.05, above VWAP 723.45", {727.05})
    assert not contradicts("higher lows since 10:05", {726.18})
    _, allowed, span = shown()
    close = sorted(allowed["10:59"])[0]
    odd = answer()
    odd["positions"][0]["points"] = [{"side": "against", "text": f"closed {close:.2f} below {close - 1:.2f}", "candle": "10:59"},
                                     {"side": "for", "text": f"closed {close:.2f} above {close - 1:.2f}", "candle": "10:59"}]
    clean, dropped = validate(odd, [POS["position"]], allowed, span)
    assert dropped == 1 and [x["side"] for x in clean["positions"][0]["points"]] == ["for"]
