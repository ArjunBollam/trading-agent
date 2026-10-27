"""`paperbot eval-prompts`: replay saved cycle prompts to the configured AI, offline.

Two questions it answers:
- Consistency: asked the same thing twice, does the AI give the same decision? Published LLM
  trading results are often non-reproducible (TradingAgents notes sampling non-determinism). If
  the answer flips on identical data, its decisions are partly noise.
- Agreement: after you change the prompt, model or provider, how often does it still reach the
  original decision?

Tools are turned off for replays (the market data they'd fetch is gone), and nothing is sent to
the broker. Every replay is a real AI call, so it costs tokens; --limit keeps that small.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from pydantic import ValidationError

from .models import Decision


def decision_key(d: Decision | None) -> tuple:
    """What counts as 'the same decision': the same set of (action, symbol) pairs. Quantities and
    wording can differ."""
    if d is None:
        return ("invalid",)
    return tuple(sorted((p.action, p.symbol.upper()) for p in d.proposals)) or ("hold",)


@dataclass
class EvalResult:
    prompts: int
    consistent: int  # repeats that matched the first answer
    repeats: int
    agreed: int  # first answers that matched the original decision
    compared: int
    invalid: int
    examples: list[str]

    def summary(self) -> str:
        lines = [f"Replayed {self.prompts} saved prompts."]
        if self.repeats:
            lines.append(f"Consistency: {self.consistent}/{self.repeats} repeats gave the same decision "
                         f"({self.consistent / self.repeats * 100:.0f}%).")
        if self.compared:
            lines.append(f"Agreement with the original run: {self.agreed}/{self.compared} "
                         f"({self.agreed / self.compared * 100:.0f}%).")
        if self.invalid:
            lines.append(f"{self.invalid} answers didn't match the schema.")
        if self.examples:
            lines.append("Differences:")
            lines += [f"  {e}" for e in self.examples[:8]]
        return "\n".join(lines)


def _ask(llm, system: str, prompt: str) -> Decision | None:
    turn = llm.turn(system, [llm.user_message(prompt)], [], allow_tools=False)
    if turn.stop != "end_turn":
        return None
    try:
        return Decision.model_validate_json(turn.text)
    except ValidationError:
        fixed = llm.reformat(turn.text)
        if fixed is None or fixed.stop != "end_turn":
            return None
        try:
            return Decision.model_validate_json(fixed.text)
        except ValidationError:
            return None


def evaluate(llm, system: str, journal, days: list[date], limit: int, repeat: int) -> EvalResult:
    prompts, originals = [], {}
    for d in days:
        for e in journal.events(d, "prompt"):
            prompts.append((e["slot"], e["text"]))
        for e in journal.events(d, "decision"):
            try:
                originals[e["slot"]] = Decision(market_view=e.get("market_view", ""), proposals=e.get("proposals", []))
            except ValidationError:
                pass
    prompts = prompts[-limit:]
    res = EvalResult(len(prompts), 0, 0, 0, 0, 0, [])
    for slot, text in prompts:
        answers = [_ask(llm, system, text) for _ in range(max(1, repeat))]
        keys = [decision_key(a) for a in answers]
        res.invalid += sum(k == ("invalid",) for k in keys)
        for k in keys[1:]:
            res.repeats += 1
            res.consistent += k == keys[0]
            if k != keys[0]:
                res.examples.append(f"{slot}: {keys[0]} vs {k} on the same prompt")
        if slot in originals:
            res.compared += 1
            same = keys[0] == decision_key(originals[slot])
            res.agreed += same
            if not same:
                res.examples.append(f"{slot}: originally {decision_key(originals[slot])}, now {keys[0]}")
    return res
