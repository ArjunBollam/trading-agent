"""eval-prompts: consistency and agreement, with a scripted AI."""

from __future__ import annotations

from fakes import CALL, FakeLLM, decision

from paperbot.evaluate import decision_key, evaluate
from paperbot.llm import AnthropicLLM
from paperbot.models import Decision

BUY = {"action": "buy_to_open", "symbol": CALL, "qty": 1, "reason": "x"}


def test_decision_key_ignores_qty_and_wording():
    a = Decision(market_view="a", proposals=[{**BUY, "qty": 1}])
    b = Decision(market_view="b", proposals=[{**BUY, "qty": 3, "reason": "y"}])
    assert decision_key(a) == decision_key(b) and decision_key(Decision(market_view="", proposals=[])) == ("hold",)


def test_consistency_and_agreement(app, cfg):
    for slot in ("s1", "s2"):
        app.journal.log("prompt", slot=slot, text=f"prompt {slot}")
    app.journal.log("decision", slot="s1", market_view="v", proposals=[BUY])
    app.journal.log("decision", slot="s2", market_view="v", proposals=[])
    # s1: buy then hold (inconsistent, agrees first time); s2: hold twice (consistent, agrees)
    llm = AnthropicLLM(FakeLLM(decision(BUY), decision(), decision(), decision()), cfg.llm.anthropic)
    res = evaluate(llm, "system", app.journal, [app.today()], limit=10, repeat=2)
    assert (res.prompts, res.repeats, res.consistent, res.compared, res.agreed) == (2, 2, 1, 2, 2)
    assert "Consistency: 1/2" in res.summary() and "Agreement with the original run: 2/2" in res.summary()
    assert all("tool_choice" in c and c["tools"] == [] for c in llm.client.calls)  # tools off
