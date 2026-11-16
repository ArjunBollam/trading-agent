"""`paperbot check-config`: every effective parameter, what the combination implies, and
warnings where settings interact badly. Pure arithmetic on the config, plus optional
read-only account checks.

The implied numbers ignore fees and assume stops fill at their price, so treat them as
optimistic floors, not forecasts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from .config import Config

Level = Literal["ok", "info", "warn"]

# Measured: a full-size Groq prompt was 4,391 tokens (2026-10-25). Budget ~4.5K per call.
TOKENS_PER_CALL = 4500
GROQ_FREE_DAILY_TOKENS = 200_000  # reported for openai/gpt-oss-120b; check console.groq.com


@dataclass(frozen=True)
class Finding:
    level: Level
    area: str
    message: str


def implied(cfg: Config, equity: float = 100_000) -> dict[str, float]:
    r, x, o = cfg.risk, cfg.exits, cfg.orders
    stop, tp = x.stop_loss_pct / 100, x.take_profit_pct / 100
    padded = stop * r.stop_slippage_factor  # what sizing assumes a stopped-out trade loses
    per_trade_risk = equity * r.max_risk_per_trade_pct / 100
    position_risk = r.max_position_usd * padded
    # Round trip at the widest allowed spread: pay mid + offset * half-spread, sell at the bid.
    half = r.max_spread_pct / 100 / 2
    round_trip_cost = half * o.entry_offset + half
    breakeven = stop / (stop + tp)
    breakeven_with_costs = (stop + round_trip_cost) / (stop + tp)
    window_min = (r_minutes(cfg.schedule.no_new_entries_after) - r_minutes(cfg.schedule.trading_start))
    cycles = window_min / cfg.schedule.decision_interval_minutes
    return {
        "per_trade_risk_usd": per_trade_risk,
        "max_position_risk_usd": position_risk,
        "binding_risk_usd": min(per_trade_risk, position_risk),
        "max_premium_per_trade_usd": min(r.max_position_usd, per_trade_risk / padded),
        "daily_loss_limit_usd": equity * r.daily_loss_limit_pct / 100,
        "streak_loss_usd": min(per_trade_risk, position_risk) * r.max_consecutive_losses,
        "max_exposure_usd": r.max_positions * r.max_position_usd,
        "reward_to_risk": tp / stop,
        "breakeven_win_rate_pct": breakeven * 100,
        "round_trip_cost_pct_of_premium": round_trip_cost * 100,
        "breakeven_win_rate_with_costs_pct": breakeven_with_costs * 100,
        "decision_cycles_per_day": cycles,
        "ai_tokens_per_day_estimate": cycles * TOKENS_PER_CALL * (1 + cfg.llm.tool_call_budget / 3),
    }


def r_minutes(t) -> int:
    return t.hour * 60 + t.minute


def audit(cfg: Config, today: date | None = None, equity: float = 100_000) -> list[Finding]:
    today = today or date.today()
    r, x, s = cfg.risk, cfg.exits, cfg.schedule
    m = implied(cfg, equity)
    out: list[Finding] = []

    def add(level: Level, area: str, msg: str) -> None:
        out.append(Finding(level, area, msg))

    # Sizing
    if m["per_trade_risk_usd"] < m["max_position_risk_usd"]:
        add("info", "sizing", f"max_risk_per_trade_pct binds before max_position_usd: at most "
            f"${m['max_premium_per_trade_usd']:,.0f} of premium per entry (${m['per_trade_risk_usd']:,.0f} to its stop).")
    else:
        add("info", "sizing", f"max_position_usd binds: ${r.max_position_usd:,.0f} premium, "
            f"${m['max_position_risk_usd']:,.0f} to its stop.")
    if m["binding_risk_usd"] >= m["daily_loss_limit_usd"]:
        add("warn", "sizing", "one stopped-out trade can hit the whole daily loss limit; lower the per-trade risk.")
    if m["streak_loss_usd"] < m["daily_loss_limit_usd"]:
        add("ok", "sizing", f"the losing-streak breaker ({r.max_consecutive_losses} losses, about "
            f"${m['streak_loss_usd']:,.0f}) trips before the ${m['daily_loss_limit_usd']:,.0f} daily limit.")
    exposure_pct = m["max_exposure_usd"] / equity * 100
    if exposure_pct > 10:
        add("warn", "sizing", f"max premium at risk across positions is {exposure_pct:.0f}% of equity.")

    # Edge arithmetic
    add("info", "edge", f"reward:risk {m['reward_to_risk']:.2f}. Breakeven win rate {m['breakeven_win_rate_pct']:.0f}% "
        f"before costs, about {m['breakeven_win_rate_with_costs_pct']:.0f}% at the widest allowed spread "
        f"(round trip ≈ {m['round_trip_cost_pct_of_premium']:.1f}% of premium).")
    if r.max_spread_pct > 10:
        add("info", "edge", f"max_spread_pct {r.max_spread_pct:g}% is above the common 10% rule of thumb.")

    # Exits
    if not x.broker_stop:
        add("warn", "exits", "broker_stop is off: between watch ticks a position has no stop at the broker.")
    if x.breakeven_at_pct is None and x.trail_pct is None:
        add("info", "exits", "no breakeven or trailing stop: winners can round-trip to the full stop-loss.")
    if x.max_hold_minutes is not None and x.max_hold_minutes <= r.min_hold_minutes:
        add("info", "exits", "max_hold_minutes ≤ min_hold_minutes: the AI can never sell before the time stop.")

    # Entry filters
    if r.delta_min is None and r.delta_max is None:
        add("info", "entries", "no delta band: the AI can buy far out-of-the-money lottery tickets.")
    if not r.min_open_interest:
        add("info", "entries", "min_open_interest is 0: illiquid contracts are allowed.")
    if r.entry_cooldown_minutes == 0:
        add("info", "entries", "entry_cooldown_minutes is 0: several entries can go out in one cycle.")
    if s.trading_start.hour * 60 + s.trading_start.minute < 9 * 60 + 45:
        add("info", "timing", "trading starts inside the opening 15 minutes, the most volatile part of the day.")

    # Calendar
    for b in r.blackout:
        if b.date is not None and b.date < today:
            add("info", "calendar", f"blackout on {b.date} ({b.reason}) is in the past and can be removed.")
    for e in r.earnings:
        if e.symbol not in r.universe:
            add("warn", "calendar", f"earnings entry for {e.symbol}, which is not in the universe.")
        elif e.date < today:
            add("info", "calendar", f"{e.symbol} earnings on {e.date} have passed; add the next date.")
    single_names = [u for u in r.universe if u not in ("SPY", "QQQ", "IWM", "DIA")]
    covered = {e.symbol for e in r.earnings if e.date >= today}
    for u in single_names:
        if u not in covered:
            add("warn", "calendar", f"no upcoming earnings date for {u}: positions could be held through a report.")

    # AI budget
    if cfg.llm.provider == "groq":
        est = m["ai_tokens_per_day_estimate"]
        budget = cfg.llm.daily_token_budget
        chain = [f for f in cfg.llm.fallbacks if f.daily_token_budget]
        if budget is not None and chain:  # each fallback model adds its own daily budget
            budget += sum(f.daily_token_budget for f in chain)
        free = GROQ_FREE_DAILY_TOKENS * (1 + sum(1 for f in cfg.llm.fallbacks if f.provider == "groq"))
        level: Level = "warn" if est > free else "ok"
        msg = (f"about {m['decision_cycles_per_day']:.0f} cycles a day, roughly {est / 1000:,.0f}K Groq tokens "
               f"(free tier reported at ~{GROQ_FREE_DAILY_TOKENS // 1000}K/day per model"
               + (f", {1 + len(cfg.llm.fallbacks)} models in the fallback chain" if cfg.llm.fallbacks else "") + ").")
        if budget is not None and est > budget:
            per_cycle = est / max(m["decision_cycles_per_day"], 1)
            minutes = budget / per_cycle * s.decision_interval_minutes
            start = s.trading_start.hour * 60 + s.trading_start.minute + minutes
            msg += (f" The {budget / 1000:,.0f}K daily budget{' across the chain' if chain else ''} would stop AI cycles around {int(start // 60):02d}:"
                    f"{int(start % 60):02d} ET (exits keep running); measured cycles have used less, so it may last longer.")
        add(level, "ai", msg)
    return out


def format_audit(cfg: Config, findings: list[Finding], equity: float) -> str:
    m = implied(cfg, equity)
    lines = [f"Effective parameters (equity ${equity:,.0f}):"]
    for section in ("schedule", "risk", "exits", "orders", "llm", "alerts"):
        data = getattr(cfg, section).model_dump(mode="json")
        if section == "llm":
            data = {k: v for k, v in data.items() if k in ("provider", "max_tool_calls", "contracts_per_underlying",
                                                          "max_proposals")} | {cfg.llm.provider: data[cfg.llm.provider]}
        for k, v in data.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                v = "; ".join(", ".join(f"{kk}={vv}" for kk, vv in item.items() if vv not in (None, "")) for item in v)
            lines.append(f"  {section}.{k}: {v}")
    lines.append("")
    lines.append("Implied by these settings:")
    for k, v in m.items():
        lines.append(f"  {k}: {v:,.2f}")
    lines.append("")
    marks = {"ok": "ok  ", "info": "info", "warn": "WARN"}
    lines.append("Checks:")
    lines += [f"  [{marks[f.level]}] {f.area}: {f.message}" for f in findings]
    return "\n".join(lines)


def account_findings(broker) -> list[Finding]:
    """Read-only checks on the Alpaca account itself. Changing them is up to you."""
    out = []
    acct = broker.account()
    level = acct.options_level
    out.append(Finding("ok" if (level or 0) >= 2 else "warn", "account",
                       f"options trading level {level} (2 is needed to buy calls and puts)."))
    if acct.trading_blocked:
        out.append(Finding("warn", "account", "account is blocked from trading."))
    cfg_fn = getattr(broker, "account_configuration", None)
    if cfg_fn is not None:
        conf = cfg_fn()
        if not conf.get("no_shorting"):
            out.append(Finding("info", "account", "Alpaca's no_shorting setting is off. paperbot never opens shorts "
                                                  "(every sell is sell_to_close), but turning it on in the dashboard adds "
                                                  "a broker-side guard."))
    return out
