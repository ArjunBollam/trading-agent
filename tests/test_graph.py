"""Full decision cycles through the LangGraph graph, with a scripted Claude."""

from __future__ import annotations

from datetime import date, datetime

import anthropic
import httpx2
from fakes import CALL, ET, Block, FakeLLM, Resp, decision, tool_call

from paperbot.graph import run_cycle
from paperbot.llm import FALLBACK_BETA

BUY = {"action": "buy_to_open", "symbol": CALL, "qty": 1, "reason": "SPY +1% on the day, holding highs"}


def test_research_then_buy(make_app, broker):
    llm = FakeLLM(tool_call("get_stock_bars", {"symbol": "SPY", "timeframe": "5Min", "limit": 12}), decision(BUY))
    app = make_app(llm)
    state = run_cycle(app)

    assert [r.status for r in state["results"]] == ["submitted"]
    assert broker.submitted[-1].type == "limit" and broker.submitted[-1].symbol == CALL

    first, second = llm.calls
    assert first["model"] == "claude-opus-5-5"
    assert first["fallbacks"] == "default" and FALLBACK_BETA in first["betas"]
    assert first["output_config"]["format"]["type"] == "json_schema"
    assert first["output_config"]["effort"] == "high"
    assert "temperature" not in first and "tool_choice" not in first
    assert all(t["strict"] for t in first["tools"])
    # The tool result went back in one user message, after the unchanged assistant turn.
    assert second["messages"][-2]["role"] == "assistant"
    result = second["messages"][-1]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "tu_1" and not result["is_error"]


def test_empty_proposals_means_hold(make_app, broker):
    state = run_cycle(make_app(FakeLLM(decision())))
    assert state["results"] == [] and broker.submitted == []


def test_malformed_answer_holds(make_app, broker):
    state = run_cycle(make_app(FakeLLM(Resp([Block("text", text='[{"symbol": "SPY"]')], "end_turn"))))
    assert "did not match the schema" in state["hold_reason"]
    assert broker.submitted == []


def test_refusal_holds(make_app, broker):
    state = run_cycle(make_app(FakeLLM(Resp([], "refusal"))))
    assert "refusal" in state["hold_reason"] and broker.submitted == []


def test_api_error_holds_and_alerts(make_app, broker, channel):
    err = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    state = run_cycle(make_app(FakeLLM(err)))
    assert "unreachable" in state["hold_reason"]
    assert any(t == "Claude call failed" for t, _, _ in channel.sent)
    assert broker.submitted == []


def test_rejected_proposal_is_not_sent(make_app, broker):
    bad = {**BUY, "symbol": "TSLA261030C00200000"}
    state = run_cycle(make_app(FakeLLM(decision(bad, {**BUY, "qty": 50}))))
    assert [r.status for r in state["results"]] == ["rejected", "rejected"]
    assert broker.submitted == []


def test_tool_budget_then_forced_answer(make_app, cfg):
    cfg.llm.max_tool_calls = 1
    llm = FakeLLM(
        tool_call("get_news", {"symbols": ["SPY"], "limit": 3}, id="tu_1"),
        tool_call("get_news", {"symbols": ["QQQ"], "limit": 3}, id="tu_2"),
        decision(),
    )
    app = make_app(llm)
    run_cycle(app)
    assert "tool_choice" not in llm.calls[0]
    assert llm.calls[1]["tool_choice"] == {"type": "none"}
    over_budget = llm.calls[2]["messages"][-1]["content"][0]
    assert over_budget["is_error"] and "budget" in over_budget["content"]
    assert len(app.journal.events(date(2026, 10, 26), "tool_call")) == 1


def test_bad_tool_input_goes_back_to_claude(make_app):
    llm = FakeLLM(tool_call("get_news", {"symbols": ["TSLA"], "limit": 3}), decision())
    run_cycle(make_app(llm))
    result = llm.calls[1]["messages"][-1]["content"][0]
    assert result["is_error"] and "must be from" in result["content"]


def test_kill_switch_skips_claude(make_app):
    llm = FakeLLM()
    app = make_app(llm)
    app.killswitch.halt("testing", scope="manual")
    state = run_cycle(app)
    assert state["halt"].startswith("halted") and llm.calls == []


def test_market_closed_and_account_mismatch(make_app, broker, cfg, channel):
    broker.market_open = False
    assert run_cycle(make_app()).get("halt") == "market closed"
    broker.market_open = True
    cfg.account_number = "PA999"
    assert "does not match" in run_cycle(make_app()).get("halt")
    assert any(t == "Account mismatch" for t, _, _ in channel.sent)


def test_halt_during_claude_call_blocks_orders(make_app, broker):
    holder = {}
    llm = FakeLLM(decision(BUY), on_call=lambda: holder["app"].killswitch.halt("phone", scope="manual"))
    holder["app"] = app = make_app(llm)
    state = run_cycle(app)
    assert "halted during the cycle" in state["hold_reason"] and broker.submitted == []


def test_code_exits_run_first_and_claude_waits(make_app, broker):
    broker.add_position(CALL, 1, 2.0)
    broker.set_quote(CALL, 3.20, 3.30)  # +62%: take profit
    llm = FakeLLM()
    state = run_cycle(make_app(llm))
    assert "code exits ran" in state["halt"] and llm.calls == []
    assert CALL not in broker.pos


def test_nothing_to_decide_after_entry_cutoff(make_app, clock):
    clock.dt = datetime(2026, 10, 26, 15, 20, tzinfo=ET)
    llm = FakeLLM()
    state = run_cycle(make_app(llm))
    assert "entry window closed" in state["halt"] and llm.calls == []


def test_claude_cannot_sell_inside_min_hold(make_app, broker):
    broker.add_position(CALL, 1, 2.0, opened_minutes_ago=5)
    sell = {"action": "sell_to_close", "symbol": CALL, "qty": 1, "reason": "changed my mind"}
    state = run_cycle(make_app(FakeLLM(decision(sell))))
    assert state["results"][0].status == "rejected" and "min_hold" in state["results"][0].detail
    assert CALL in broker.pos


def test_prompt_shows_positions_budget_and_reasons(make_app, broker):
    llm = FakeLLM(decision(BUY), decision())
    app = make_app(llm)
    run_cycle(app)
    broker.fill_at_limit(next(o.id for o in broker.orders.values() if o.is_open and o.side == "buy"))
    run_cycle(app)
    prompt = llm.calls[1]["messages"][0]["content"]
    assert CALL in prompt and "SPY +1% on the day" in prompt and "remaining risk budget" in prompt
    assert "stop_at" in prompt
