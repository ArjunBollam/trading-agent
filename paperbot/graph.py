"""One decision cycle as a LangGraph state graph.

    preflight -> protect -> snapshot -> research <-> tools
                                           |
                                         decide -> execute -> report

Any node can end the cycle early by setting `halt`; it then goes straight to report.
The AI (Claude, Grok or Groq, see llm.py) is called only in `research` (and, for Groq,
`decide`), and only returns JSON proposals. `execute` hands each proposal to the order
funnel, which checks it against the risk gate.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from .app import App
from .llm import ToolCall
from .models import Decision, Snapshot
from .orders import ExecResult
from .performance import tokens_used
from .prompt import cycle_prompt, cycle_slot, system_prompt
from .protect import ProtectReport, protect
from .research import ResearchTools, build_snapshot


class CycleState(TypedDict, total=False):
    slot: str
    halt: str  # set when the cycle ends early, with the reason
    protect: ProtectReport
    snapshot: Snapshot
    messages: Annotated[list, operator.add]  # append-only, in the provider's own message format
    turns: int
    tool_calls: int
    pending_tools: list[ToolCall]
    stop_reason: str
    final_text: str
    hold_reason: str
    decision: Decision | None
    results: list[ExecResult]


def _parse(text: str) -> Decision | None:
    try:
        return Decision.model_validate_json(text)
    except ValidationError:
        return None


def build_graph(app: App):
    cfg = app.cfg
    llm = app.llm
    budget = cfg.llm.tool_call_budget
    tools = ResearchTools(app)
    tool_defs = tools.definitions
    system = system_prompt(app)

    def preflight(state: CycleState) -> dict:
        today = app.today()
        if reason := app.killswitch.entries_halted(today):
            return {"halt": f"halted: {reason}"}
        if app.rehearsal:
            assert app.funnel.dry_run, "rehearsals must be dry runs"
        elif not app.broker.clock().is_open:
            return {"halt": "market closed"}
        app.gate.ensure_session(app.broker, today)
        if not app.rehearsal and not app.gate.in_trading_window(app.now()):
            return {"halt": "outside trading window"}
        if hasattr(llm, "select"):  # a FallbackLLM: the first model with budget left answers this cycle
            choice = llm.select(app.journal.events(today, "llm_turn"), app.now())
            if choice is None:
                if not app.journal.events(today, "token_budget_reached"):
                    app.journal.log("token_budget_reached", models=llm.usage())
                    app.alerts.send("AI token budget used", f"Every AI model is used up for today ({llm.usage()}). "
                                    "No more AI cycles until tomorrow; exits and stops keep running.", "default", ["hourglass"])
                return {"halt": f"every AI model's daily budget is used ({llm.usage()})"}
            if choice is not llm.choices[0]:
                note_fallback(choice.key, f"{llm.choices[0].llm.name} is out for now ({llm.usage()}); "
                                          f"using {choice.llm.name} ({choice.model}).")
            return account_check()
        budget = cfg.llm.daily_token_budget
        if budget is not None:
            used = tokens_used(app.journal.events(today, "llm_turn"))
            if used >= budget:
                if not app.journal.events(today, "token_budget_reached"):
                    app.journal.log("token_budget_reached", used=used, budget=budget)
                    app.alerts.send("AI token budget used", f"{used:,} of {budget:,} tokens today. No more AI cycles "
                                    "until tomorrow; exits and stops keep running.", "default", ["hourglass"])
                return {"halt": f"daily AI token budget used ({used:,}/{budget:,})"}
        return account_check()

    def note_fallback(key: str, text: str) -> None:
        """Journal every switch; alert once per model per day."""
        today = app.today()
        alerted = any(e.get("key") == key for e in app.journal.events(today, "llm_fallback"))
        app.journal.log("llm_fallback", key=key, text=text)
        if not alerted:
            app.alerts.send("AI fallback", text, "low", ["arrows_counterclockwise"])

    def account_check() -> dict:
        acct = app.broker.account()
        if cfg.account_number and acct.account_number != cfg.account_number:
            app.alerts.send("Account mismatch", f"Broker account {acct.account_number} is not {cfg.account_number}.",
                            "high", ["warning"])
            return {"halt": f"account {acct.account_number} does not match config"}
        return {}

    def protect_node(state: CycleState) -> dict:
        rep = protect(app, state["slot"])
        if rep.flattened:
            return {"protect": rep, "halt": "daily loss limit: flattened"}
        if rep.changed_positions:
            # Positions just changed, so the snapshot the AI would see is already stale.
            return {"protect": rep, "halt": "code exits ran this cycle; the AI waits for the next one"}
        return {"protect": rep}

    def snapshot_node(state: CycleState) -> dict:
        snap = build_snapshot(app)
        if app.rehearsal:
            pass  # exercise the whole pipeline on whatever data there is
        elif not app.gate.entries_allowed(app.now()) and not any(p.option for p in snap.positions):
            return {"snapshot": snap, "halt": "entry window closed and nothing held"}
        if snap.stale and len(snap.stale) == len(cfg.risk.universe) and not app.rehearsal:
            app.alerts.send("Market data stale", f"No recent trades for {', '.join(snap.stale)}; skipping this cycle.",
                            "high", ["warning"])
            return {"snapshot": snap, "halt": "market data stale"}
        prompt = cycle_prompt(app, snap)
        if app.rehearsal:
            prompt += ("\n\nREHEARSAL: the market is closed and this data is old. Answer as you would during "
                       "market hours; nothing will be traded.")
        app.journal.log("prompt", slot=state["slot"], text=prompt)
        return {"snapshot": snap, "messages": [llm.user_message(prompt)]}

    def research(state: CycleState) -> dict:
        turns = state.get("turns", 0) + 1
        allow_tools = state.get("tool_calls", 0) < budget
        turn = llm.turn(system, state["messages"], tool_defs, allow_tools)
        if getattr(llm, "switched", None):
            note_fallback(llm.key, llm.switched)
            llm.switched = None
        if turn.stop == "api_error":
            return {"turns": turns, "stop_reason": "api_error", "hold_reason": turn.error}
        app.journal.log(
            "llm_turn", slot=state["slot"], provider=llm.name, key=getattr(llm, "key", None), turn=turns,
            stop_reason=turn.raw_stop,
            model=turn.model, text=turn.text, tool_uses=[vars(c) for c in turn.tool_calls], usage=turn.usage,
        )
        return {"messages": [turn.message], "turns": turns, "stop_reason": turn.stop,
                "final_text": turn.text, "pending_tools": turn.tool_calls}

    def route_research(state: CycleState) -> str:
        if state.get("stop_reason") == "tool_use" and state.get("turns", 0) <= budget + 1:
            return "tools"
        return "decide"

    def tools_node(state: CycleState) -> dict:
        used = state.get("tool_calls", 0)
        results: list[tuple[str, str, bool]] = []
        for call in state.get("pending_tools", []):
            if used >= budget:
                results.append((call.id, "Research budget used up. Give your final decision now.", True))
                continue
            used += 1
            try:
                output, is_error = tools.run(call.name, dict(call.input)), False
            except Exception as e:  # tool errors go back to the model, they don't end the cycle
                output, is_error = f"Error: {e}", True
            app.journal.log("tool_call", slot=state["slot"], name=call.name, input=dict(call.input),
                            output=output[:4000], is_error=is_error)
            results.append((call.id, output, is_error))
        return {"messages": llm.tool_results(results), "tool_calls": used, "pending_tools": []}

    def decide(state: CycleState) -> dict:
        if state.get("hold_reason"):
            return {"decision": None}
        stop = state.get("stop_reason")
        if stop == "refusal":
            return {"decision": None, "hold_reason": f"{llm.name} declined the request (refusal); holding"}
        if stop != "end_turn":
            return {"decision": None, "hold_reason": f"no final answer (stop_reason={stop}); holding"}
        text = state.get("final_text", "")
        decision = _parse(text)
        if decision is None:
            # Providers without structured outputs alongside tools (Groq) get one schema-enforced reformat.
            fixed = llm.reformat(text)
            if fixed is not None:
                app.journal.log("reformat", slot=state["slot"], stop_reason=fixed.raw_stop, text=fixed.text,
                                error=fixed.error)
                if fixed.stop == "end_turn":
                    decision = _parse(fixed.text)
        if decision is None:
            return {"decision": None, "hold_reason": "answer did not match the schema; holding"}
        if len(decision.proposals) > cfg.llm.max_proposals:
            decision.proposals = decision.proposals[: cfg.llm.max_proposals]
        app.journal.log("decision", slot=state["slot"], market_view=decision.market_view,
                        proposals=[p.model_dump() for p in decision.proposals])
        return {"decision": decision}

    def execute(state: CycleState) -> dict:
        decision = state.get("decision")
        if decision is None or not decision.proposals:
            return {"results": []}
        if reason := app.killswitch.entries_halted(app.today()):
            # Someone halted while the AI was thinking.
            return {"results": [], "hold_reason": f"halted during the cycle: {reason}"}
        results = []
        # Sells first: they free up position slots, and they start any re-entry cooldown before a
        # buy on the same underlying is checked, whatever order the AI listed them in.
        for p in sorted(decision.proposals, key=lambda p: p.action != "sell_to_close"):
            if p.action == "buy_to_open":
                results.append(app.funnel.enter(p.symbol, p.qty, p.reason, state["slot"]))
            else:
                results.append(app.funnel.exit(p.symbol.strip().upper(), p.qty, p.reason, state["slot"], discretionary=True))
        return {"results": results}

    def report(state: CycleState) -> dict:
        results = state.get("results", [])
        app.journal.log(
            "cycle_end", slot=state["slot"], halt=state.get("halt"), hold_reason=state.get("hold_reason"),
            turns=state.get("turns", 0), tool_calls=state.get("tool_calls", 0),
            results=[{"status": r.status, "kind": r.kind, "symbol": r.symbol, "qty": r.qty, "detail": r.detail}
                     for r in results],
        )
        if state.get("stop_reason") == "api_error":
            app.ai_failures += 1
            if app.ai_failures in (1, 4, 16):  # don't page every cycle during an outage
                app.alerts.send(f"{llm.name} call failed",
                                f"{state.get('hold_reason', '')} ({app.ai_failures} in a row; holding until it recovers)",
                                "high", ["warning"])
        elif state.get("turns"):
            if app.ai_failures >= 1:
                app.alerts.send(f"{llm.name} is answering again", f"After {app.ai_failures} failed calls.", "low")
            app.ai_failures = 0
        return {}

    def go_on(next_node: str):
        return lambda state: "report" if state.get("halt") else next_node

    g = StateGraph(CycleState)
    g.add_node("preflight", preflight)
    g.add_node("protect", protect_node)
    g.add_node("snapshot", snapshot_node)
    g.add_node("research", research)
    g.add_node("tools", tools_node)
    g.add_node("decide", decide)
    g.add_node("execute", execute)
    g.add_node("report", report)
    g.add_edge(START, "preflight")
    g.add_conditional_edges("preflight", go_on("protect"), ["protect", "report"])
    g.add_conditional_edges("protect", go_on("snapshot"), ["snapshot", "report"])
    g.add_conditional_edges("snapshot", go_on("research"), ["research", "report"])
    g.add_conditional_edges("research", route_research, ["tools", "decide"])
    g.add_edge("tools", "research")
    g.add_edge("decide", "execute")
    g.add_edge("execute", "report")
    g.add_edge("report", END)
    return g.compile()


def run_cycle(app: App, graph=None, triggered: bool = False) -> CycleState:
    """triggered: an extra cycle started by a setup alert. It gets its own slot, so its client
    order ids don't collide with the scheduled cycle's."""
    graph = graph or build_graph(app)
    now = app.now().astimezone(app.cfg.schedule.tz)
    slot = cycle_slot(now, app.cfg.schedule.decision_interval_minutes) + (f"s{now:%H%M}" if triggered else "")
    return graph.invoke({"slot": slot, "messages": [], "turns": 0, "tool_calls": 0},
                        config={"recursion_limit": 100})
