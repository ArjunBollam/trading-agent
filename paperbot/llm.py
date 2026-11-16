"""The AI providers paperbot can ask for proposals, behind one small interface.

The graph only ever calls turn(), tool_results() and reformat(). Each adapter turns that
into its provider's request shape, and turns the reply into a provider-neutral Turn.

    anthropic  Claude through the Anthropic SDK (structured outputs + refusal fallback)
    xai        Grok through xAI's OpenAI-compatible API (structured outputs work with tools)
    groq       Open models through Groq's OpenAI-compatible API (no structured outputs with
               tools, so an answer that doesn't validate gets one tools-free reformat call)
    gemini, openrouter
               Fallbacks only, through their OpenAI-compatible APIs, handled like Groq

FallbackLLM chains them (llm.fallbacks): when one model's daily budget is used or it's rate limited,
the next one answers.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

from .config import AnthropicSettings, Config, FallbackModel, OpenAICompatSettings
from .prompt import DECISION_SCHEMA

Stop = Literal["tool_use", "end_turn", "refusal", "max_tokens", "api_error", "other"]

FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class Turn:
    stop: Stop
    message: Any = None  # provider-native assistant message, appended to the conversation as-is
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw_stop: str | None = None
    model: str | None = None
    usage: Any = None
    error: str | None = None
    rate_limited: bool = False  # the provider said slow down or out of quota (HTTP 429)


class LLM(Protocol):
    name: str  # shown in alerts, e.g. "Claude"

    def user_message(self, text: str) -> Any: ...
    def turn(self, system: str, messages: list, tools: list[dict], allow_tools: bool) -> Turn: ...
    def tool_results(self, results: list[tuple[str, str, bool]]) -> list[Any]: ...
    def reformat(self, text: str) -> Turn | None: ...


def _dump(obj: Any) -> Any:
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json", exclude_none=True)
    return dict(vars(obj)) if hasattr(obj, "__dict__") else obj


# ---- Anthropic ------------------------------------------------------------------------------


class AnthropicLLM:
    name = "Claude"

    def __init__(self, client: Any, settings: AnthropicSettings):
        self.client = client
        self.s = settings

    def user_message(self, text: str) -> dict:
        return {"role": "user", "content": text}

    def turn(self, system: str, messages: list, tools: list[dict], allow_tools: bool) -> Turn:
        import anthropic

        kwargs: dict[str, Any] = dict(
            model=self.s.model,
            max_tokens=self.s.max_tokens,
            system=system,
            messages=messages,
            tools=tools,
            output_config={"effort": self.s.effort,
                           "format": {"type": "json_schema", "schema": DECISION_SCHEMA}},
            cache_control={"type": "ephemeral"},
            # If a safety classifier declines, the API retries on a fallback model instead of failing.
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )
        if not allow_tools:
            kwargs["tool_choice"] = {"type": "none"}
        try:
            resp = self.client.beta.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            return Turn("api_error", error=f"Claude rate limited: {e.message}", rate_limited=True)
        except anthropic.APIStatusError as e:
            return Turn("api_error", error=f"Claude API error {e.status_code}: {e.message}")
        except anthropic.APIConnectionError as e:
            return Turn("api_error", error=f"Claude unreachable: {e}")

        blocks = resp.content
        text = "".join(b.text for b in blocks if getattr(b, "type", None) == "text")
        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in blocks if getattr(b, "type", None) == "tool_use"]
        stop = resp.stop_reason if resp.stop_reason in ("tool_use", "end_turn", "refusal", "max_tokens") else "other"
        # Content goes back unchanged (thinking blocks included): the conversation is append-only.
        return Turn(stop, {"role": "assistant", "content": blocks}, text, calls, resp.stop_reason,
                    getattr(resp, "model", None), _dump(getattr(resp, "usage", None)))

    def tool_results(self, results: list[tuple[str, str, bool]]) -> list[dict]:
        # All results for one assistant turn go back in a single user message.
        return [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": out, "is_error": err} for tid, out, err in results
        ]}]

    def reformat(self, text: str) -> Turn | None:
        return None  # structured outputs already guarantee the schema


# ---- OpenAI-compatible: xAI and Groq ---------------------------------------------------------


@dataclass(frozen=True)
class ProviderInfo:
    label: str
    base_url: str
    key_env: str
    structured_with_tools: bool


OPENAI_COMPATIBLE = {
    "xai": ProviderInfo("Grok", "https://api.x.ai/v1", "XAI_API_KEY", structured_with_tools=True),
    "groq": ProviderInfo("Groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY", structured_with_tools=False),
    "gemini": ProviderInfo("Gemini", "https://generativelanguage.googleapis.com/v1beta/openai/", "GEMINI_API_KEY",
                           structured_with_tools=False),
    "openrouter": ProviderInfo("OpenRouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY",
                               structured_with_tools=False),
}

_REFORMAT_PROMPT = (
    "Convert the trading decision below into JSON matching the schema. Keep exactly the proposals it "
    "states; do not add, change or invent any. If it states no clear proposal, return an empty proposals list."
)


@dataclass(frozen=True)
class ToolUseFailed:
    generation: str  # what the model wrote before the provider rejected it
    error: str


def decision_from_failed_generation(generation: str) -> str | None:
    """The decision JSON inside a rejected tool call such as {"name": "JSON", "arguments": {...}},
    or a bare decision object, if it validates against the schema. Never guesses beyond that."""
    from pydantic import ValidationError

    from .models import Decision

    try:
        data = json.loads(generation)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(data, dict) and "arguments" in data:
        data = data["arguments"]
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                return None
    try:
        return Decision.model_validate(data).model_dump_json()
    except ValidationError:
        return None


class OpenAICompatLLM:
    def __init__(self, client: Any, settings: OpenAICompatSettings, provider: str, name: str | None = None):
        self.client = client
        self.s = settings
        self.info = OPENAI_COMPATIBLE[provider]
        self.name = name or self.info.label

    def user_message(self, text: str) -> dict:
        return {"role": "user", "content": text}

    @staticmethod
    def _functions(tools: list[dict]) -> list[dict]:
        return [{"type": "function", "function": {
            "name": t["name"], "description": t["description"], "parameters": t["input_schema"],
        }} for t in tools]

    @staticmethod
    def _schema_format() -> dict:
        return {"type": "json_schema", "json_schema": {"name": "decision", "strict": True, "schema": DECISION_SCHEMA}}

    def _create(self, **kwargs: Any) -> Any:
        import openai

        kwargs.setdefault("max_completion_tokens", self.s.max_tokens)
        if self.s.reasoning_effort:
            kwargs["reasoning_effort"] = self.s.reasoning_effort
        if self.s.extra_body:
            kwargs["extra_body"] = self.s.extra_body
        try:
            return self.client.chat.completions.create(model=self.s.model, **kwargs)
        except openai.BadRequestError as e:
            body = e.body if isinstance(e.body, dict) else {}
            if e.code == "tool_use_failed":  # Groq refused a malformed tool call; keep what the model wrote
                return ToolUseFailed(str(body.get("failed_generation") or ""), e.message)
            return Turn("api_error", error=f"{self.name} API error {e.status_code}: {e.message}")
        except openai.RateLimitError as e:
            return Turn("api_error", error=f"{self.name} rate limited: {e.message}", rate_limited=True)
        except openai.APIStatusError as e:
            return Turn("api_error", error=f"{self.name} API error {e.status_code}: {e.message}")
        except openai.APIConnectionError as e:
            return Turn("api_error", error=f"{self.name} unreachable: {e}")

    def turn(self, system: str, messages: list, tools: list[dict], allow_tools: bool) -> Turn:
        kwargs: dict[str, Any] = dict(
            messages=[{"role": "system", "content": system}, *messages],
            tools=self._functions(tools),
            tool_choice="auto" if allow_tools else "none",
        )
        if self.info.structured_with_tools:
            kwargs["response_format"] = self._schema_format()
        resp = self._create(**kwargs)
        if isinstance(resp, ToolUseFailed):
            # gpt-oss on Groq sometimes "calls" a made-up tool (e.g. "JSON") to deliver its answer,
            # and Groq rejects the whole reply. Use that answer if it's a valid decision; otherwise
            # ask once more with tools off.
            text = decision_from_failed_generation(resp.generation)
            if text is not None:
                return Turn("end_turn", {"role": "assistant", "content": text}, text, raw_stop="tool_use_failed:recovered")
            resp = self._create(**{**kwargs, "tool_choice": "none"})
            if isinstance(resp, ToolUseFailed):
                return Turn("api_error", error=f"{self.name} tool call failed twice: {resp.error}")
        return resp if isinstance(resp, Turn) else self._to_turn(resp)

    def _to_turn(self, resp: Any) -> Turn:
        choice = resp.choices[0]
        msg = choice.message
        usage = _dump(getattr(resp, "usage", None))
        if getattr(msg, "refusal", None):
            return Turn("refusal", None, msg.refusal, raw_stop="refusal", model=resp.model, usage=usage)

        calls = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_unparseable_arguments": tc.function.arguments}
            calls.append(ToolCall(tc.id, tc.function.name, args if isinstance(args, dict) else {"_arguments": args}))

        stop: Stop = {"tool_calls": "tool_use", "stop": "end_turn", "length": "max_tokens",
                      "content_filter": "refusal"}.get(choice.finish_reason, "other")
        if calls:
            stop = "tool_use"  # some providers report finish_reason "stop" alongside tool calls
        assistant: dict[str, Any] = {"role": "assistant", "content": msg.content or ("" if not calls else None)}
        if calls:
            assistant["tool_calls"] = [
                {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ]
        return Turn(stop, assistant, msg.content or "", calls, choice.finish_reason, resp.model, usage)

    def tool_results(self, results: list[tuple[str, str, bool]]) -> list[dict]:
        return [{"role": "tool", "tool_call_id": tid, "content": out} for tid, out, _ in results]

    def reformat(self, text: str) -> Turn | None:
        if self.info.structured_with_tools or not text.strip():
            return None
        resp = self._create(
            messages=[{"role": "system", "content": _REFORMAT_PROMPT}, {"role": "user", "content": text}],
            response_format=self._schema_format(),
        )
        if isinstance(resp, ToolUseFailed):
            return Turn("api_error", error=f"{self.name} reformat failed: {resp.error}")
        return resp if isinstance(resp, Turn) else self._to_turn(resp)


@dataclass
class Choice:
    llm: Any
    key: str  # "provider:model": today's tokens are counted per model
    model: str
    budget: int | None


def tokens_for(choice: Choice, llm_turns: list[dict]) -> int:
    """Today's tokens on one model. Turns journaled before fallbacks existed carry no key: match the model."""
    from .performance import tokens_used

    return tokens_used([e for e in llm_turns
                        if e.get("key") == choice.key or (e.get("key") is None and e.get("model") == choice.model)])


class FallbackLLM:
    """The configured model, then each of llm.fallbacks in order. At the start of a cycle, select() picks
    the first model with daily budget left that isn't resting after a rate limit. If the cycle's first
    call is rate limited, the next model answers it at once; later in a cycle the conversation already
    holds one model's replies, so the cycle holds instead and the next one starts on the next model."""

    def __init__(self, choices: list[Choice]):
        self.choices = choices
        self.current = 0
        self.resting: dict[str, datetime] = {}  # key -> usable again from
        self.switched: str | None = None  # set when turn() moved to another model; the graph journals it
        self._used: dict[str, int] = {}
        self._now: datetime | None = None

    @property
    def active(self) -> Choice:
        return self.choices[self.current]

    @property
    def name(self) -> str:
        return self.active.llm.name

    @property
    def key(self) -> str:
        return self.active.key

    def _now_(self) -> datetime:
        return self._now or datetime.now(timezone.utc)

    def _usable(self, i: int) -> bool:
        c = self.choices[i]
        if c.budget is not None and self._used.get(c.key, 0) >= c.budget:
            return False
        until = self.resting.get(c.key)
        return until is None or until <= self._now_()

    def select(self, llm_turns: list[dict], now: datetime) -> Choice | None:
        self._used = {c.key: tokens_for(c, llm_turns) for c in self.choices}
        self._now = now
        for i in range(len(self.choices)):
            if self._usable(i):
                self.current = i
                return self.choices[i]
        return None

    def usage(self) -> str:
        return ", ".join(f"{c.llm.name} {self._used.get(c.key, 0):,}" + (f"/{c.budget:,}" if c.budget else "")
                         for c in self.choices)

    def _rest(self, c: Choice, error: str) -> None:
        """Out for the day if the provider says a daily quota is spent; otherwise rest two minutes."""
        now = self._now_()
        daily = any(s in error.lower() for s in ("per day", "(tpd)", "(rpd)", "daily", "quota"))
        self.resting[c.key] = ((now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                               if daily else now + timedelta(minutes=2))

    def user_message(self, text: str) -> Any:
        return self.active.llm.user_message(text)

    def turn(self, system: str, messages: list, tools: list[dict], allow_tools: bool) -> Turn:
        t = self.active.llm.turn(system, messages, tools, allow_tools)
        while t.rate_limited:
            self._rest(self.active, t.error or "")
            if len(messages) != 1:  # mid-cycle: another model can't continue this one's conversation
                return t
            nxt = next((i for i in range(len(self.choices)) if i != self.current and self._usable(i)), None)
            if nxt is None:
                return t
            self.switched = f"{self.name} was rate limited ({t.error}); switched to {self.choices[nxt].llm.name}"
            self.current = nxt
            t = self.active.llm.turn(system, messages, tools, allow_tools)
        return t

    def tool_results(self, results: list[tuple[str, str, bool]]) -> list[Any]:
        return self.active.llm.tool_results(results)

    def reformat(self, text: str) -> Turn | None:
        return self.active.llm.reformat(text)


def _single(provider: str, settings: Any, name: str | None = None) -> LLM:
    """One provider's client. Raises ValueError if its API key is missing."""
    if provider == "anthropic":
        import anthropic

        # The client only fails on its first request without credentials, so check up front.
        # (An `ant auth login` profile under ~/.config/anthropic counts too.)
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                or (Path.home() / ".config" / "anthropic").exists()):
            raise ValueError("ANTHROPIC_API_KEY is not set (llm.provider is anthropic)")
        return AnthropicLLM(anthropic.Anthropic(), settings)

    import openai

    info = OPENAI_COMPATIBLE[provider]
    key = os.environ.get(info.key_env, "")
    if not key:
        raise ValueError(f"{info.key_env} is not set (llm.provider is {provider})")
    return OpenAICompatLLM(openai.OpenAI(api_key=key, base_url=info.base_url), settings, provider, name)


def _fallback_settings(fb: FallbackModel) -> AnthropicSettings | OpenAICompatSettings:
    if fb.provider == "anthropic":
        return AnthropicSettings(model=fb.model, max_tokens=max(fb.max_tokens, 1024))
    return OpenAICompatSettings(model=fb.model, max_tokens=fb.max_tokens, reasoning_effort=fb.reasoning_effort,
                                daily_token_budget=fb.daily_token_budget, extra_body=fb.extra_body)


def make_llm(cfg: Config) -> LLM:
    """The configured provider, chained to llm.fallbacks if any. Raises ValueError if the main
    provider's API key is missing; a fallback without its key is skipped with a warning."""
    import sys

    primary = _single(cfg.llm.provider, cfg.llm.active)
    if not cfg.llm.fallbacks:
        return primary
    choices = [Choice(primary, f"{cfg.llm.provider}:{cfg.llm.model}", cfg.llm.model, cfg.llm.daily_token_budget)]
    for fb in cfg.llm.fallbacks:
        label = "Claude" if fb.provider == "anthropic" else OPENAI_COMPATIBLE[fb.provider].label
        try:
            llm = _single(fb.provider, _fallback_settings(fb), f"{label} {fb.model.split('/')[-1]}")
        except ValueError as e:
            print(f"paperbot: fallback {fb.provider} {fb.model} skipped: {e}", file=sys.stderr)
            continue
        choices.append(Choice(llm, f"{fb.provider}:{fb.model}", fb.model, fb.daily_token_budget))
    return FallbackLLM(choices) if len(choices) > 1 else primary
