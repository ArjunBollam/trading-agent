"""AI fallbacks (llm.FallbackLLM): the next model answers once one is out of budget or rate limited."""

from __future__ import annotations

from datetime import datetime, timedelta

from fakes import ET, FakeLLM, decision
from paperbot.config import AnthropicSettings, Config
from paperbot.graph import run_cycle
from paperbot.llm import AnthropicLLM, Choice, FallbackLLM, OpenAICompatLLM, Turn, make_llm
from conftest import ROOT

NOW = datetime(2026, 10, 26, 10, 40, tzinfo=ET)


class Scripted:
    """A model that answers with a fixed list of turns."""

    def __init__(self, name: str, *turns: Turn):
        self.name, self.turns, self.calls = name, list(turns), 0

    def user_message(self, text):
        return {"role": "user", "content": text}

    def turn(self, system, messages, tools, allow_tools):
        self.calls += 1
        return self.turns.pop(0)

    def tool_results(self, results):
        return []

    def reformat(self, text):
        return None


OK = Turn("end_turn", {"role": "assistant", "content": "{}"}, "{}")
DAILY = Turn("api_error", error="Rate limit reached for model on tokens per day (TPD): Limit 200000", rate_limited=True)
MINUTE = Turn("api_error", error="Rate limit reached for model on tokens per minute (TPM)", rate_limited=True)


def chain(*models, budgets=(1000, None, None)):
    return FallbackLLM([Choice(m, f"groq:{m.name}", m.name, b) for m, b in zip(models, budgets)])


def used(key: str, tokens: int) -> dict:
    return {"event": "llm_turn", "key": key, "usage": {"prompt_tokens": tokens, "completion_tokens": 0}}


def test_select_skips_models_whose_budget_is_used():
    a, b = Scripted("a"), Scripted("b")
    llm = chain(a, b)
    assert llm.select([used("groq:a", 999)], NOW).llm is a
    assert llm.select([used("groq:a", 600), used("groq:a", 400)], NOW).llm is b
    # turns journaled before fallbacks existed have no key: they count by model
    assert llm.select([{"event": "llm_turn", "model": "a", "usage": {"input_tokens": 2000}}], NOW).llm is b
    assert chain(a, b, budgets=(1000, 1000)).select([used("groq:a", 1000), used("groq:b", 1000)], NOW) is None


def test_a_rate_limited_first_call_is_answered_by_the_next_model():
    a, b = Scripted("a", DAILY), Scripted("b", OK)
    llm = chain(a, b)
    llm.select([], NOW)
    t = llm.turn("sys", [llm.user_message("prompt")], [], True)
    assert t is OK and llm.name == "b" and "tokens per day" in llm.switched
    assert llm.resting["groq:a"] == datetime(2026, 10, 27, tzinfo=ET)  # out until tomorrow
    assert llm.select([], NOW + timedelta(hours=1)).llm is b


def test_a_per_minute_limit_rests_only_briefly():
    a, b = Scripted("a", MINUTE), Scripted("b", OK)
    llm = chain(a, b)
    llm.select([], NOW)
    llm.turn("sys", [llm.user_message("p")], [], True)
    assert llm.select([], NOW + timedelta(minutes=1)).llm is b
    assert llm.select([], NOW + timedelta(minutes=3)).llm is a


def test_mid_cycle_rate_limit_holds_instead_of_switching():
    a, b = Scripted("a", MINUTE), Scripted("b", OK)
    llm = chain(a, b)
    llm.select([], NOW)
    t = llm.turn("sys", [{"role": "user", "content": "p"}, {"role": "assistant", "content": "x"}], [], True)
    assert t.rate_limited and b.calls == 0 and llm.switched is None


def test_make_llm_chains_groq_models_and_skips_fallbacks_without_a_key(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    cfg = Config.load(ROOT / "config.aggressive.yaml")
    cfg.llm.fallbacks = cfg.llm.fallbacks + [cfg.llm.fallbacks[0].model_copy(update={"provider": "gemini", "model": "gemini-x"})]
    llm = make_llm(cfg)
    assert isinstance(llm, FallbackLLM)
    names = [c.llm.name for c in llm.choices]
    assert names[0] == "Groq" and "Groq gpt-oss-20b" in names and not any("Gemini" in n for n in names)
    qwen = next(c.llm for c in llm.choices if "qwen" in c.model)
    assert isinstance(qwen, OpenAICompatLLM) and qwen.s.extra_body
    cfg.llm.fallbacks = []
    assert not isinstance(make_llm(cfg), FallbackLLM)


def test_cycle_runs_on_the_fallback_and_says_so_once(make_app, channel):
    settings = AnthropicSettings()
    first = AnthropicLLM(FakeLLM(), settings)
    first.name = "Main"
    second = AnthropicLLM(FakeLLM(decision(), decision()), settings)
    second.name = "Backup"
    app = make_app(FallbackLLM([Choice(first, "anthropic:main", "main", 1000), Choice(second, "anthropic:backup", "backup", None)]))
    app.journal.log("llm_turn", key="anthropic:main", usage={"input_tokens": 1500})
    state = run_cycle(app)
    assert state.get("decision") is not None and second.client.calls and not first.client.calls
    turns = [e for e in app.journal.events(app.today(), "llm_turn") if e.get("provider") == "Backup"]
    assert turns and turns[0]["key"] == "anthropic:backup"
    assert [t for t, *_ in channel.sent].count("AI fallback") == 1
    run_cycle(app)
    assert [t for t, *_ in channel.sent].count("AI fallback") == 1  # alerted once per model per day


def test_every_model_used_up_holds_the_cycle(make_app, channel):
    first = AnthropicLLM(FakeLLM(), AnthropicSettings())
    app = make_app(FallbackLLM([Choice(first, "anthropic:main", "main", 1000)]))
    app.journal.log("llm_turn", key="anthropic:main", usage={"input_tokens": 1500})
    state = run_cycle(app)
    assert "every AI model" in state["halt"] and not first.client.calls
    assert any(t == "AI token budget used" for t, *_ in channel.sent)
