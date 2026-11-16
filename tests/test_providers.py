"""The xAI (Grok) and Groq paths, through the same graph, with a scripted OpenAI-style client."""

from __future__ import annotations

import httpx2
import openai
import pytest
from fakes import CALL, FakeOpenAI, oa_decision, oa_text, oa_tool_call

from paperbot.graph import run_cycle
from paperbot.llm import AnthropicLLM, OpenAICompatLLM, make_llm

BUY = {"action": "buy_to_open", "symbol": CALL, "qty": 1, "reason": "SPY +1% on the day"}


@pytest.fixture
def provider_app(make_app, cfg):
    def make(provider, *responses):
        cfg.llm.provider = provider
        fake = FakeOpenAI(*responses)
        return make_app(OpenAICompatLLM(fake, getattr(cfg.llm, provider), provider)), fake
    return make


def test_groq_research_then_buy(provider_app, broker):
    app, fake = provider_app("groq", oa_tool_call("get_news", {"symbols": ["SPY"], "limit": 3}), oa_decision(BUY))
    state = run_cycle(app)
    assert [r.status for r in state["results"]] == ["submitted"]

    first, second = fake.calls
    assert first["model"] == "openai/gpt-oss-120b"
    assert first["messages"][0]["role"] == "system"
    assert first["tool_choice"] == "auto" and first["tools"][0]["type"] == "function"
    assert "response_format" not in first  # Groq can't combine structured outputs with tools
    assert first["max_completion_tokens"] == 2000 and first["reasoning_effort"] == "low"
    # The assistant tool call and its result go back in OpenAI format.
    assert second["messages"][-2]["tool_calls"][0]["id"] == "call_1"
    assert second["messages"][-1]["role"] == "tool" and second["messages"][-1]["tool_call_id"] == "call_1"


def test_groq_free_tier_limits_shrink_the_prompt(provider_app, cfg):
    app, fake = provider_app("groq", oa_decision())
    assert (cfg.llm.tool_call_budget, cfg.llm.candidates_per_underlying) == (2, 6)
    run_cycle(app)
    assert "up to 2 times per cycle" in fake.calls[0]["messages"][0]["content"]


def test_groq_invalid_answer_gets_one_schema_reformat(provider_app, broker):
    app, fake = provider_app("groq", oa_text("I'd buy 1 SPY call, SPY261030C00580000, it's trending."), oa_decision(BUY))
    state = run_cycle(app)
    assert [r.status for r in state["results"]] == ["submitted"]
    reformat = fake.calls[1]
    assert "tools" not in reformat
    assert reformat["response_format"]["json_schema"]["strict"] is True


def test_groq_reformat_failure_holds(provider_app, broker):
    app, _ = provider_app("groq", oa_text("not json"), oa_text("still not json"))
    state = run_cycle(app)
    assert "did not match the schema" in state["hold_reason"] and broker.submitted == []


def test_groq_tool_budget(provider_app):
    app, fake = provider_app(
        "groq",
        oa_tool_call("get_news", {"symbols": ["SPY"], "limit": 3}, id="c1"),
        oa_tool_call("get_news", {"symbols": ["QQQ"], "limit": 3}, id="c2"),
        oa_tool_call("get_news", {"symbols": ["NVDA"], "limit": 3}, id="c3"),
        oa_decision(),
    )
    run_cycle(app)
    assert [c["tool_choice"] for c in fake.calls] == ["auto", "auto", "none", "none"]
    assert "budget" in fake.calls[3]["messages"][-1]["content"]


def test_xai_uses_structured_outputs_with_tools_and_never_reformats(provider_app, broker):
    app, fake = provider_app("xai", oa_text("not json"))
    state = run_cycle(app)
    assert fake.calls[0]["model"] == "grok-4.7"
    assert fake.calls[0]["response_format"]["type"] == "json_schema" and "tools" in fake.calls[0]
    assert len(fake.calls) == 1 and "did not match" in state["hold_reason"]


def test_bad_tool_arguments_go_back_as_an_error(provider_app):
    app, fake = provider_app("groq", oa_tool_call("get_news", "{not json"), oa_decision())
    run_cycle(app)
    assert fake.calls[1]["messages"][-1]["content"].startswith("Error:")


@pytest.mark.parametrize("response", [oa_text("", refusal="I can't help with that"), oa_text("", finish="content_filter")])
def test_refusals_hold(provider_app, broker, response):
    app, _ = provider_app("groq", response)
    state = run_cycle(app)
    assert "refusal" in state["hold_reason"] and broker.submitted == []


def test_rate_limit_holds_and_alerts(provider_app, broker, channel):
    req = httpx2.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    err = openai.RateLimitError("rate limited", response=httpx2.Response(429, request=req), body=None)
    app, _ = provider_app("groq", err)
    state = run_cycle(app)
    assert "rate limited" in state["hold_reason"] and broker.submitted == []
    assert any(t == "Groq call failed" for t, _, _ in channel.sent)


def test_make_llm_picks_the_provider_and_needs_its_key(cfg, monkeypatch):
    cfg.llm.fallbacks = []  # one provider on its own; the chain is in test_fallback.py
    cfg.llm.provider = "groq"
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(ValueError, match="GROQ_API_KEY"):
        make_llm(cfg)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    llm = make_llm(cfg)
    assert isinstance(llm, OpenAICompatLLM) and str(llm.client.base_url) == "https://api.groq.com/openai/v1/"
    cfg.llm.provider = "xai"
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    assert str(make_llm(cfg).client.base_url) == "https://api.x.ai/v1/"
    cfg.llm.provider = "anthropic"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert isinstance(make_llm(cfg), AnthropicLLM)


# ---- Groq's tool_use_failed: recover the answer, else retry once with tools off --------------

import json  # noqa: E402

from paperbot.llm import decision_from_failed_generation  # noqa: E402


def tool_use_failed(generation: str):
    req = httpx2.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    body = {"message": "Tool call validation failed: attempted to call tool 'JSON' which was not in request.tools",
            "type": "invalid_request_error", "code": "tool_use_failed", "failed_generation": generation}
    return openai.BadRequestError("tool_use_failed", response=httpx2.Response(400, request=req), body=body)


def test_failed_generation_answer_is_used(provider_app, broker):
    gen = json.dumps({"name": "JSON", "arguments": {"market_view": "up", "proposals": [BUY]}})
    app, fake = provider_app("groq", tool_use_failed(gen))
    state = run_cycle(app)
    assert [r.status for r in state["results"]] == ["submitted"] and len(fake.calls) == 1


def test_unusable_failed_generation_retries_once_without_tools(provider_app):
    app, fake = provider_app("groq", tool_use_failed('{"name": "JSON", "arguments": {"oops": 1}}'), oa_decision())
    state = run_cycle(app)
    assert state.get("hold_reason") is None and state["results"] == []
    assert fake.calls[0]["tool_choice"] == "auto" and fake.calls[1]["tool_choice"] == "none"


def test_two_tool_failures_hold(provider_app, broker):
    app, fake = provider_app("groq", tool_use_failed("garbage"), tool_use_failed("garbage"))
    state = run_cycle(app)
    assert "failed twice" in state["hold_reason"] and broker.submitted == []


def test_decision_from_failed_generation_shapes():
    d = {"market_view": "flat", "proposals": []}
    assert json.loads(decision_from_failed_generation(json.dumps({"name": "JSON", "arguments": d}))) == d
    assert json.loads(decision_from_failed_generation(json.dumps({"name": "x", "arguments": json.dumps(d)}))) == d
    assert json.loads(decision_from_failed_generation(json.dumps(d))) == d
    assert decision_from_failed_generation("not json") is None
    assert decision_from_failed_generation(json.dumps({"name": "JSON", "arguments": {"proposals": "x"}})) is None
