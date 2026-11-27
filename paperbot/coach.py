"""Claude's read on your positions. Every few minutes of regular hours, and soon after the position guard flags
something, each symbol you hold goes to Claude with what the chart shows: the last hour of 1-minute candles and two
hours of 5-minute ones, the lines on the chart with their latest break or retest, today's QQE, squeeze, S/R and
structure signals, order flow, the scanner's named levels and bigger-picture reasons, the phone alerts and guard events
of the last half hour, and each position with its P&L, strike and time left.

Claude answers, for each position, whether the chart favors it or goes against it, each point tied to the candle that
shows it; the patterns it sees; and the prices that would change the read. The answer is checked before it's shown: a
point citing a candle that isn't in the data, a close that isn't that candle's close, "above" or "below" that its own
numbers contradict, a pattern between candles that aren't there, a price outside the day's range, or a position you
don't hold is dropped, and the number dropped is shown. Information only: nothing here places
an order, and Claude is told to describe the chart, not to tell you to buy or sell."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .features import Bar
from .trendlines import line_from_json, track

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# $ per million tokens (input, output, cache read, cache write) for working out what each read cost
PRICES = {"claude-opus-5-5": (4.0, 20.0, 0.20, 5.0), "claude-sonnet-5-5": (2.0, 10.0, 0.10, 2.5),
          "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125)}
TRIGGERS = ("exit", "against", "with", "target")  # guard events that bring the next read forward
SOON = 90  # seconds at least between two reads of one symbol

SYSTEM = """You read stock charts for one trader's own position monitor, a tool they run on their computer. Every few \
minutes you get what the chart of a symbol they hold shows right now, and their positions on it. Say, for each \
position, what in the chart is going for it and what is going against it.

How to answer:
- Calls and long shares gain when price rises; puts and short shares gain when it falls. "favors" means the chart, on \
balance, is moving or set up in the position's direction; "against" means the opposite; "mixed" when neither side \
clearly leads. Set confidence by how clearly the data agrees.
- Each point names one thing the data shows: a candle (a rejection wick, an engulfing candle, a close through a line \
or level, a volume spike), a sequence (higher lows, lower highs), a signal (QQE, squeeze, structure break, S/R break), \
order flow, a line or level holding or breaking, VWAP, the time left and distance to the strike for options that \
expire today. Put the candle that shows it in "candle" as HH:MM exactly as it appears in the data (the time a candle \
starts, Eastern time). Use "" only for a point about a level or the session as a whole.
- Use only prices and times that appear in the data. Never round a level into a different one or invent a level.
- 2 to 5 points per position, most important first, each under 160 characters. Headline under 120 characters.
- patterns: chart patterns you can see clearly in these candles (double bottom, flag, wedge, triangle, head and \
shoulders, range...), with the first and last candle (HH:MM from the data) and what it means for the positions. None \
is fine.
- watch: 1 to 3 prices from the data that would change the read, and why.
- Describe what the chart shows. Do not tell the trader to buy, sell, hold or close anything; a separate rule-based \
guard does that, and they decide. Never suggest placing orders."""

SCHEMA: dict = {
    "type": "object", "additionalProperties": False, "required": ["positions", "patterns", "watch"],
    "properties": {
        "positions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["position", "lean", "confidence", "headline", "points"],
            "properties": {
                "position": {"type": "string"},
                "lean": {"type": "string", "enum": ["favors", "against", "mixed"]},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                "headline": {"type": "string"},
                "points": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["side", "text", "candle"],
                    "properties": {"side": {"type": "string", "enum": ["for", "against"]}, "text": {"type": "string"},
                                   "candle": {"type": "string"}}}}}}},
        "patterns": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["name", "from", "to", "meaning"],
            "properties": {"name": {"type": "string"}, "from": {"type": "string"}, "to": {"type": "string"},
                           "meaning": {"type": "string"}}}},
        "watch": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["price", "why"],
            "properties": {"price": {"type": "number"}, "why": {"type": "string"}}}},
    },
}


class CoachError(Exception):
    """A read that didn't happen, how long to wait before trying again, and the model whose daily allowance ran out
    (if that's why)."""

    def __init__(self, text: str, wait: int = 60, spent: str | None = None):
        super().__init__(text)
        self.wait, self.spent = wait, spent


NO_CREDIT = ("Your Anthropic account has no API credit, so Claude can't read the charts. Add some under Plans & Billing "
             "at platform.claude.com; reads start again on their own within 5 minutes.")


def _hm(t: float | datetime, tz) -> str:
    t = t if isinstance(t, datetime) else datetime.fromtimestamp(t, tz)
    return t.astimezone(tz).strftime("%H:%M")


def _left(span: timedelta) -> str:
    m = max(0, int(span.total_seconds() // 60))
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def five_minutes(bars: list[Bar]) -> list[Bar]:
    """1-minute candles into 5-minute ones, on the :00, :05, ... marks."""
    out: list[Bar] = []
    for b in bars:
        start = b.t.replace(minute=b.t.minute - b.t.minute % 5, second=0, microsecond=0)
        if out and out[-1].t == start:
            x = out[-1]
            out[-1] = Bar(start, x.o, max(x.h, b.h), min(x.l, b.l), b.c, x.v + b.v)
        else:
            out.append(Bar(start, b.o, b.h, b.l, b.c, b.v))
    return out


def _num(x: float) -> str:
    return f"{x:.2f}"


def context(symbol: str, positions: list[dict], bars: list[Bar], scan: dict | None, chart: dict | None,
            drawings: list[dict], events: list[dict], alerts: list[dict], now: datetime, close_at: datetime | None,
            tz) -> tuple[str, dict[str, set[float]], tuple[float, float]]:
    """What the AI is shown for one symbol, the candle times it may cite (each with the closes of the 1- and 5-minute
    candles that start then, for checking what it says about them), and the day's price range."""
    ones = bars[-60:]
    fives = five_minutes(bars[-120:])[-24:]
    allowed: dict[str, set[float]] = {}
    for b in ones + fives:
        allowed.setdefault(_hm(b.t, tz), set()).add(round(b.c, 2))
    today = [b for b in bars if b.t.astimezone(tz).date() == now.date()]
    lo, hi = min(b.l for b in today or ones), max(b.h for b in today or ones)
    last = ones[-1]
    out = [f"Time now: {now:%H:%M} ET, {now:%A %Y-%m-%d}. The last closed 1-minute candle is {_hm(last.t, tz)}, close "
           f"{_num(last.c)}."]
    if close_at:
        out.append(f"Today the regular session closes at {close_at:%H:%M} ET ({_left(close_at - now)} left).")
    out.append(f"Symbol: {symbol}. Today's range so far (since midnight ET, overnight trading included): {_num(lo)} to "
               f"{_num(hi)}.")
    out.append("")
    out.append(f"The trader's positions on {symbol}:")
    for p in positions:
        side = "gains if price rises" if p.get("bullish") else "gains if price falls"
        line = f'- "{p["position"]}" ({side})'
        if p.get("pnl") is not None:
            line += f": {'up' if p['pnl'] >= 0 else 'down'} ${abs(p['pnl']):,.2f} at Robinhood's last mark"
            if p.get("mark") is not None:
                line += f" ({p['mark']})"
        line += "."
        v = p.get("verdict") or {}
        if v.get("text"):
            line += f" The rule-based guard says: {v['text']}"
        out.append(line)
    if scan:
        out.append("")
        out.append(f"paperbot's scanner (5-minute chart): trend {scan.get('trend')}, RSI {scan.get('rsi')}, VWAP "
                   f"{scan.get('vwap')}.")
        ctx = scan.get("context") or {}
        if ctx.get("factors"):
            out.append(f"Bigger picture: {ctx.get('bias')} (score {ctx.get('score')}):")
            out += [f"  {'+' if f.get('side', 0) > 0 else '-'} {f.get('text')}" for f in ctx["factors"][:8]]
        levels = sorted(scan.get("levels") or [], key=lambda x: -x["price"])
        if levels:
            out.append("Named levels: " + "; ".join(f"{x['name']} {_num(x['price'])}" for x in levels))
    near = []
    for d in drawings:
        try:
            ln = line_from_json(d, tz)
        except ValueError:
            continue
        p = ln.price_at(last.t)
        if p is not None:
            near.append((abs(last.c - p), ln, p))
    if near:
        out.append("")
        out.append("The 10 nearest lines on the chart (price at the last candle, where price is, and the line's latest "
                   "break or retest today):")
        for _, ln, p in sorted(near, key=lambda x: x[0])[:10]:
            stages = [x for x in track(ln, bars) if bars[x.index].t.astimezone(tz).date() == now.date()]
            latest = stages[-1].text if stages else "nothing today"
            out.append(f"- {ln.label}: {_num(p)}, price {_num(abs(last.c - p))} {'above' if last.c > p else 'below'} it. "
                       f"{latest}")
    if chart:
        day0 = datetime.combine(now.date(), datetime.min.time(), tzinfo=tz).timestamp()
        sigs = []
        for q in chart.get("qqe") or []:
            if q["t"] >= day0:
                sigs.append((q["t"], f"QQE {q['side']}"))
        for s in (chart.get("sqz") or {}).get("signals") or []:
            if s["t"] >= day0:
                sigs.append((s["t"], f"squeeze released {s['side']} (momentum {s.get('mom')})"))
        for s in (chart.get("sr") or {}).get("breaks") or []:
            if s["t"] >= day0:
                sigs.append((s["t"], f"S/R {s.get('kind')} {s['side']} at {s.get('level')}"))
        for s in ((chart.get("smc") or {}).get("structure") or []):
            if s["t"] >= day0:
                sigs.append((s["t"], f"{s.get('kind')} {s['side']} at {s.get('level')}"))
        if sigs:
            out.append("")
            out.append("Today's 1-minute chart signals (regular-hours candles), oldest first:")
            out.append("; ".join(f"{_hm(t, tz)} {s}" for t, s in sorted(sigs)[-24:]))
        flow = (chart.get("flow") or {}).get("bars") or []
        recent = [f for f in flow if f[0] >= last.t.timestamp() - 1800]
        if recent:
            cums = [f[2] for f in recent]
            out.append(f"Order flow, last 30 minutes ({(chart.get('flow') or {}).get('feed')} share only, cumulative "
                       f"delta of today): {cums[0]} at {_hm(recent[0][0], tz)}, low {min(cums)}, high {max(cums)}, "
                       f"{cums[-1]} at {_hm(recent[-1][0], tz)}.")
    if alerts:
        out.append("")
        out.append("Phone alerts on this symbol in the last 30 minutes: " +
                   "; ".join(f"{_hm(datetime.fromisoformat(a['ts']), tz)} {a['title']}" for a in alerts[-10:]))
    if events:
        out.append("Guard events in the last 30 minutes: " +
                   "; ".join(f"{_hm(e['t'], tz)} {e['kind']}: {e['text']}" for e in events[:8]))
    out.append("")
    out.append("5-minute candles, last 2 hours (start time, open, high, low, close, volume):")
    out += [f"{_hm(b.t, tz)} {_num(b.o)} {_num(b.h)} {_num(b.l)} {_num(b.c)} {int(b.v)}" for b in fives]
    out.append("")
    out.append("1-minute candles, last hour (start time, open, high, low, close, volume):")
    out += [f"{_hm(b.t, tz)} {_num(b.o)} {_num(b.h)} {_num(b.l)} {_num(b.c)} {int(b.v)}" for b in ones]
    return "\n".join(out), allowed, (lo, hi)


LEANS, CONFIDENCE, SIDES = ("favors", "against", "mixed"), ("low", "medium", "high"), ("for", "against")
FREE = ("groq", "gemini", "openrouter")  # providers whose free tiers the reads run on
PROVIDER_LABELS = {"anthropic": "Claude", "groq": "Groq", "gemini": "Gemini", "openrouter": "OpenRouter", "xai": "Grok"}


CLOSED = re.compile(r"\bclos(?:ed|es|ing|e)\s+(?:at\s+)?(\d+\.\d+)(?:\s*,?\s*(below|above|under|over)\b[^\n]{0,60}?(\d+\.\d+))?",
                    re.IGNORECASE)


def contradicts(text: str, closes: set[float] | None) -> bool:
    """Whether a point's own words are false: "closed 726.57 below 726.31" (726.57 isn't below 726.31), or a close for
    its candle that isn't the candle's close."""
    for m in CLOSED.finditer(text):
        x = float(m.group(1))
        if closes and all(abs(x - k) > 0.02 for k in closes):
            return True
        if m.group(2):
            y = float(m.group(3))
            if (m.group(2).lower() in ("below", "under") and not x < y) or (m.group(2).lower() in ("above", "over") and not x > y):
                return True
    return False


def validate(answer: dict, labels: list[str], allowed: dict[str, set[float]] | set[str],
             span: tuple[float, float]) -> tuple[dict, int]:
    """Keep only what checks out against the data: candles that were sent, closes that are those candles' closes,
    "above" and "below" that agree with the numbers given, positions that are held, prices within the day's range (2%
    either side), and answers in the expected shape (some models stray from it). Returns the cleaned answer and how
    many items were dropped."""
    dropped = 0
    lo, hi = span[0] * 0.98, span[1] * 1.02
    positions = []
    for p in answer.get("positions") or []:
        if not isinstance(p, dict) or p.get("position") not in labels or p.get("lean") not in LEANS:
            dropped += 1
            continue
        points = []
        for x in p.get("points") or []:
            c = str(x.get("candle") or "") if isinstance(x, dict) else ""
            if not isinstance(x, dict) or x.get("side") not in SIDES or not x.get("text") or (c and c not in allowed):
                dropped += 1
                continue
            if contradicts(str(x["text"]), allowed.get(c) if isinstance(allowed, dict) and c else None):
                dropped += 1
                continue
            points.append({"side": x["side"], "text": str(x["text"])[:240], "candle": c})
        positions.append({"position": p["position"], "lean": p["lean"],
                          "confidence": p.get("confidence") if p.get("confidence") in CONFIDENCE else "low",
                          "headline": str(p.get("headline") or "")[:160], "points": points[:6]})
    patterns = []
    for x in answer.get("patterns") or []:
        if isinstance(x, dict) and x.get("name") and x.get("from") in allowed and x.get("to") in allowed:
            patterns.append({k: str(x.get(k) or "")[:200] for k in ("name", "from", "to", "meaning")})
        else:
            dropped += 1
    watch = []
    for x in answer.get("watch") or []:
        try:
            price = float(x["price"])
        except (KeyError, TypeError, ValueError):
            dropped += 1
            continue
        if lo <= price <= hi:
            watch.append({"price": round(price, 2), "why": str(x.get("why", ""))[:200]})
        else:
            dropped += 1
    return {"positions": positions, "patterns": patterns[:4], "watch": watch[:4]}, dropped


def parse_json(raw: str, label: str) -> dict:
    """The answer's JSON object, even when a model wraps it in a code fence or a sentence."""
    for text in (raw, raw[raw.find("{"):raw.rfind("}") + 1] if "{" in raw else ""):
        try:
            out = json.loads(text)
        except ValueError:
            continue
        if isinstance(out, dict):
            return out
    raise CoachError(f"{label}'s answer wasn't valid JSON")


class ClaudeReader:
    """Claude through the Anthropic SDK: structured outputs, prompt caching, the refusal fallback. Billed per token."""

    provider, label, free = "anthropic", "Claude", False

    def __init__(self, client: Any, model: str, effort: str):
        self.client, self.model, self.effort = client, model, effort

    def read(self, text: str) -> tuple[dict, dict, str, float]:
        """The answer, its token usage, the model that answered and what it cost in dollars."""
        import anthropic

        try:
            resp = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": text}],
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": SCHEMA}},
                # if a safety classifier declines by mistake, the API retries on a fallback model instead of failing
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except anthropic.AuthenticationError:
            raise CoachError("Anthropic refused the API key (ANTHROPIC_API_KEY in .env): it may have expired or been "
                             "deleted. Make a new one at platform.claude.com.", 600) from None
        except anthropic.BadRequestError as e:
            if "credit balance" in str(e.message).lower():
                raise CoachError(NO_CREDIT, 300) from None
            raise CoachError(f"Claude API error 400: {e.message}", 300) from None
        except anthropic.RateLimitError as e:
            raise CoachError(f"Claude rate limited: {e.message}", 120) from None
        except anthropic.APIStatusError as e:
            raise CoachError(f"Claude API error {e.status_code}: {e.message}", 120) from None
        except anthropic.APIConnectionError:
            raise CoachError("Claude unreachable (network)", 60) from None
        usage = resp.usage
        used = {k: int(getattr(usage, k, 0) or 0) for k in
                ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}
        if resp.stop_reason != "end_turn":
            raise CoachError(f"Claude stopped early ({resp.stop_reason})")
        raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        return parse_json(raw, "Claude"), used, str(getattr(resp, "model", self.model)), cost(self.model, used)


class OpenAIReader:
    """Groq, Gemini, OpenRouter or Grok through their OpenAI-compatible APIs (paperbot/llm.py OPENAI_COMPATIBLE).
    Groq's free tier allows roughly 8,000 tokens a minute and 200,000 a day per model; a read is about 4,500."""

    def __init__(self, client: Any, provider: str, model: str, effort: str):
        self.client, self.provider, self.model, self.effort = client, provider, model, effort
        self.label, self.free = PROVIDER_LABELS.get(provider, provider), provider in FREE

    def read(self, text: str) -> tuple[dict, dict, str, float]:
        import openai

        kwargs: dict[str, Any] = dict(
            model=self.model,
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}],
            max_completion_tokens=4500,  # Groq's free 8,000 tokens a minute count this too; the prompt is ~3,000
            # strict schema where the model supports it (gpt-oss on Groq); best effort elsewhere, then checked here
            response_format={"type": "json_schema", "json_schema": {"name": "chart_read", "schema": SCHEMA,
                                                                     "strict": "gpt-oss" in self.model}},
        )
        if "gpt-oss" in self.model:
            kwargs["reasoning_effort"] = self.effort if self.effort in ("low", "medium", "high") else "medium"
        if self.model.startswith("qwen"):
            kwargs["extra_body"] = {"reasoning_format": "hidden"}  # keeps Qwen's thinking out of its answer
        who = f"{self.label} {self.model}"
        try:
            resp = self.client.chat.completions.create(**kwargs)
        except openai.AuthenticationError:
            raise CoachError(f"{self.label} refused the API key: check it in .env.", 600) from None
        except openai.RateLimitError as e:
            text_ = str(e.message).lower()
            if "per day" in text_ or "(tpd)" in text_ or "(rpd)" in text_ or "daily" in text_:
                raise CoachError(f"{who}: today's free allowance is used up.", 0, spent=self.model) from None
            raise CoachError(f"{who} is busy (rate limit); trying again in a minute.", 60) from None
        except openai.APIStatusError as e:
            if "max completion tokens" in str(e.message).lower():  # its thinking left no room for the whole answer
                raise CoachError(f"{who} ran out of room before finishing its answer; trying again shortly.", 60) from None
            raise CoachError(f"{who} API error {e.status_code}: {e.message}", 120) from None
        except openai.APIConnectionError:
            raise CoachError(f"{self.label} unreachable (network)", 60) from None
        choice = resp.choices[0]
        if choice.finish_reason == "length":
            raise CoachError(f"{who} ran out of room before finishing its answer", 60)
        usage = resp.usage
        used = {"input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0)}
        return parse_json(choice.message.content or "", self.label), used, str(getattr(resp, "model", self.model)), 0.0


def cost(model: str, used: dict) -> float:
    p_in, p_out, p_read, p_write = PRICES.get(model, PRICES["claude-opus-5-5"])
    return (used.get("input_tokens", 0) * p_in + used.get("output_tokens", 0) * p_out
            + used.get("cache_read_input_tokens", 0) * p_read + used.get("cache_creation_input_tokens", 0) * p_write) / 1e6


class Coach:
    """logs/coach.json: the latest read per symbol, today's reads, tokens and spend, and any problem;
    logs/coach_log.jsonl: every read."""

    def __init__(self, folder: Path):
        self.path = Path(folder) / "coach.json"
        self.log_path = Path(folder) / "coach_log.jsonl"

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {"symbols": {}, "spend": {}, "status": None}

    def save(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, self.path)

    def status(self, text: str | None, wait: int = 0, now: datetime | None = None) -> None:
        """What's stopping the reads, if anything, and when to try again."""
        data = self.load()
        retry = (now + timedelta(seconds=wait)).isoformat() if wait and now else None
        if data.get("status") != text or data.get("retry_at") != retry:
            data["status"], data["retry_at"] = text, retry
            self.save(data)

    def waiting(self, now: datetime) -> bool:
        """Whether a recent error says to hold off."""
        at = self.load().get("retry_at")
        return bool(at) and now < datetime.fromisoformat(at)

    def _today(self, now: datetime) -> dict:
        spend = self.load().get("spend") or {}
        return spend if spend.get("day") == now.date().isoformat() else {}

    def usable(self, models: list[str], now: datetime, daily_tokens: int) -> str | None:
        """The first model with free allowance left today: under `daily_tokens`, and not turned away for the day."""
        today = self._today(now)
        for m in models:
            if m not in (today.get("spent") or []) and (today.get("tokens") or {}).get(m, 0) < daily_tokens:
                return m
        return None

    def mark_spent(self, model: str, now: datetime) -> None:
        """The provider said this model's daily allowance is used up: skip it until tomorrow."""
        data = self.load()
        spend = data.get("spend") or {}
        if spend.get("day") != now.date().isoformat():
            spend = {"day": now.date().isoformat(), "reads": 0, "usd": 0.0}
        spend["spent"] = sorted(set(spend.get("spent") or []) | {model})
        data["spend"] = spend
        self.save(data)

    def due(self, symbol: str, now: datetime, every_minutes: int, guard_events: list[dict]) -> str | None:
        """Why a read is due for this symbol now ("every 10 minutes", "after exit"), or None."""
        last = (self.load().get("symbols") or {}).get(symbol)
        if not last:
            return "first read"
        at = datetime.fromisoformat(last["at"]).timestamp()
        ago = now.timestamp() - at
        if ago < SOON:
            return None
        news = [e for e in guard_events if e.get("symbol") == symbol and e.get("kind") in TRIGGERS
                and datetime.fromisoformat(e["at"]).timestamp() > at]
        if news:
            return f"after {news[0]['kind']}"
        return f"every {every_minutes} minutes" if ago >= every_minutes * 60 - 5 else None

    def run(self, symbol: str, positions: list[dict], text: str, allowed: set[str], span: tuple[float, float],
            reader: Any, now: datetime, why: str, send: Callable[[str, str, str], None] | None = None) -> dict:
        """Ask, check, save; push to the phone when a position's lean changes. Returns the saved read."""
        answer, used, served, spent = reader.read(text)
        clean, dropped = validate(answer, [p["position"] for p in positions], allowed, span)
        read = clean | {"at": now.isoformat(), "why": why, "by": reader.label, "provider": reader.provider,
                        "free": reader.free, "model": served, "usage": used, "usd": round(spent, 4), "dropped": dropped}
        data = self.load()
        before = (data.get("symbols") or {}).get(symbol) or {}
        data.setdefault("symbols", {})[symbol] = read
        day = now.date().isoformat()
        spend = data.get("spend") or {}
        if spend.get("day") != day:
            spend = {"day": day, "reads": 0, "usd": 0.0}
        spend["reads"] = spend.get("reads", 0) + 1
        spend["usd"] = round(spend.get("usd", 0.0) + spent, 4)
        tokens = spend.setdefault("tokens", {})
        tokens[reader.model] = tokens.get(reader.model, 0) + sum(used.values())
        data["spend"], data["status"], data["retry_at"] = spend, None, None
        self.save(data)
        with self.log_path.open("a") as f:
            f.write(json.dumps({"symbol": symbol} | read) + "\n")
        if send:
            was = {p["position"]: p["lean"] for p in before.get("positions") or []}
            for p in clean["positions"]:
                if was.get(p["position"]) not in (None, p["lean"]):
                    word = {"favors": "now favors", "against": "now goes against", "mixed": "is now mixed for"}[p["lean"]]
                    top = "; ".join(f"{'+' if x['side'] == 'for' else '-'} {x['text']}" for x in p["points"][:3])
                    send(f"{reader.label}: {symbol} chart {word} {p['position']}".replace("×", "x"),
                         f"{p['headline']} ({p['confidence']} confidence, {now:%H:%M} ET). {top}",
                         "high" if p["lean"] == "against" else "default")
        return read


def note_for(folder: Path, symbol: str, label: str, now: datetime, fresh_minutes: int = 12) -> str:
    """The AI's latest headline on a position, for the guard's HOLD check-in, if it's recent."""
    read = (Coach(folder).load().get("symbols") or {}).get(symbol)
    if not read or now.timestamp() - datetime.fromisoformat(read["at"]).timestamp() > fresh_minutes * 60:
        return ""
    p = next((x for x in read.get("positions") or [] if x["position"] == label), None)
    if not p:
        return ""
    word = {"favors": "favors it", "against": "goes against it", "mixed": "mixed"}[p["lean"]]
    return (f" {read.get('by', 'Claude')}'s read at {datetime.fromisoformat(read['at']):%H:%M}: {word} "
            f"({p['confidence']} confidence): {p['headline']}")
