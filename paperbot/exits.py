"""Exit rules decided by code, never by the AI."""

from __future__ import annotations

from datetime import date, datetime, time

from .config import ExitConfig
from .models import Position, Quote, round_to_tick

STOP_LOSS = "stop_loss"
TAKE_PROFIT = "take_profit"
EXPIRY = "expiry"
TIME_STOP = "time_stop"
END_OF_DAY = "end_of_day"


def mark(position: Position, quote: Quote | None) -> float:
    """Mid when the quote is usable, otherwise the broker's last price."""
    return quote.mid if quote is not None and quote.valid else position.current_price


def pnl_pct(position: Position, quote: Quote | None) -> float:
    if position.avg_entry_price <= 0:
        return 0.0
    return (mark(position, quote) - position.avg_entry_price) / position.avg_entry_price * 100


def exit_reason(position: Position, quote: Quote | None, today: date, cfg: ExitConfig,
                now: datetime | None = None, opened_at: datetime | None = None,
                close_all_at: time | None = None) -> str | None:
    """`now` must be in market time (ET). close_all_at overrides cfg.close_all_at (early closes)."""
    close_all_at = close_all_at if close_all_at is not None else cfg.close_all_at
    opt = position.option
    if opt is not None and cfg.close_at_dte >= 0 and opt.dte(today) <= cfg.close_at_dte:
        return EXPIRY  # never hold into expiration
    if close_all_at is not None and now is not None and now.time() >= close_all_at:
        return END_OF_DAY
    move = pnl_pct(position, quote)
    if move <= -cfg.stop_loss_pct:
        return STOP_LOSS
    if move >= cfg.take_profit_pct:
        return TAKE_PROFIT
    if cfg.max_hold_minutes is not None and now is not None and opened_at is not None:
        if (now - opened_at).total_seconds() / 60 >= cfg.max_hold_minutes:
            return TIME_STOP
    return None


def protective_stop_price(position: Position, cfg: ExitConfig, quote: Quote | None = None,
                          current_stop: float | None = None, min_step: bool = True) -> float:
    """Where the broker stop belongs. It starts at the stop-loss level, moves up to breakeven
    and then trails as the position gains, and never moves down."""
    entry = position.avg_entry_price
    levels = [entry * (1 - cfg.stop_loss_pct / 100)]
    if quote is not None and quote.valid:
        m = quote.mid
        if cfg.breakeven_at_pct is not None and m >= entry * (1 + cfg.breakeven_at_pct / 100):
            levels.append(entry)
        if cfg.trail_pct is not None and m >= entry * (1 + cfg.trail_start_pct / 100):
            levels.append(m * (1 - cfg.trail_pct / 100))
    want = max(0.01, round_to_tick(max(levels), position.underlying, "down"))
    if current_stop is None:
        return want
    # Only ratchet in meaningful steps: replacing the stop on every tick would churn orders (and
    # trip the runaway breaker) while a trailing stop follows a rising price.
    if want <= current_stop or (min_step and want - current_stop < entry * cfg.min_stop_step_pct / 100):
        return current_stop
    return want
