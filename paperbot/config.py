"""Config loading and validation.

Validation is strict on purpose: a limit that is silently disabled by a bad value
(Autobelay shipped with min_hold_minutes: 0, which turned its churn guard off) is
refused at startup instead.
"""

from __future__ import annotations

import os
from datetime import date as date_
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, model_validator


class ScheduleConfig(BaseModel):
    timezone: str = "America/New_York"
    decision_interval_minutes: int = Field(10, ge=1, le=120)
    watch_interval_seconds: int = Field(30, ge=5, le=300)
    trading_start: time = time(9, 45)
    trading_end: time = time(15, 45)
    no_new_entries_after: time = time(15, 15)
    trigger_cycle_on_signal: bool = False  # run an extra AI cycle as soon as a setup alert fires

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @model_validator(mode="after")
    def _window(self) -> ScheduleConfig:
        if not self.trading_start < self.no_new_entries_after <= self.trading_end:
            raise ValueError("need trading_start < no_new_entries_after <= trading_end")
        return self


class Earnings(BaseModel):
    """A scheduled earnings report. bmo = before the open, amc = after the close."""

    symbol: str
    date: date_
    time: Literal["bmo", "amc"]

    def last_session_before(self) -> date_:
        """The last trading session before the price gap: the report day itself for amc,
        the previous weekday for bmo."""
        if self.time == "amc":
            return self.date
        d = self.date - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d


class Blackout(BaseModel):
    """A window with no new entries, e.g. around an FOMC decision. No date means every day."""

    start: time
    end: time
    date: date_ | None = None
    reason: str = ""

    def covers(self, when: datetime) -> bool:
        return (self.date is None or when.date() == self.date) and self.start <= when.time() < self.end

    @model_validator(mode="after")
    def _order(self) -> Blackout:
        if self.end <= self.start:
            raise ValueError("blackout end must be after start")
        return self


class RiskConfig(BaseModel):
    universe: list[str] = Field(min_length=1)
    max_contracts_per_order: int = Field(gt=0)
    max_position_usd: float = Field(gt=0)
    max_positions: int = Field(gt=0)
    max_entries_per_day: int = Field(gt=0)
    daily_loss_limit_pct: float = Field(gt=0, le=20)
    soft_loss_limit_pct: float | None = Field(None, gt=0, description="past this drawdown, per-trade risk halves")
    min_dte: int = Field(ge=0, description="0 allows same-day expiries (needs close_at_dte -1 and close_all_at)")
    max_dte: int = Field(ge=1)
    max_spread_pct: float = Field(gt=0, le=100)
    min_hold_minutes: int = Field(ge=1, description="0 would disable the churn guard, so it is not allowed")
    reentry_cooldown_minutes: int = Field(ge=0)
    max_risk_per_trade_pct: float = Field(0.5, gt=0, le=5, description="risk to stop, % of start-of-day equity")
    max_consecutive_losses: int = Field(3, ge=1, description="losing trades in a row before entries stop for the day")
    entry_cooldown_minutes: int = Field(0, ge=0, description="minimum wait between new entries")
    max_quote_age_seconds: int = Field(60, ge=5, description="entries are refused on quotes older than this")
    max_stock_data_age_seconds: int = Field(300, ge=30, description="underlyings with older trades are skipped")
    blackout: list[Blackout] = Field(default_factory=list, description="no new entries in these windows")
    delta_min: float | None = Field(None, ge=0, le=1, description="entries need |delta| at least this")
    delta_max: float | None = Field(None, ge=0, le=1, description="entries need |delta| at most this")
    min_open_interest: int = Field(0, ge=0, description="entries need at least this open interest")
    oi_waived_below_spread_pct: float = Field(3.0, ge=0, description="no OI floor when the live spread is this tight")
    max_theta_pct_per_day: float | None = Field(None, gt=0, description="refuse entries decaying faster than this")
    stop_slippage_factor: float = Field(1.0, ge=1, le=3, description="size as if a stop loses this x its distance")
    max_same_direction_positions: int = Field(99, ge=1, description="calls (or puts) held or pending at once")
    manage_external_positions: bool = Field(True, description="put stops and exits on positions paperbot didn't open")
    earnings: list[Earnings] = Field(default_factory=list)
    earnings_block_days: int = Field(2, ge=0, description="no new entries this many days before an earnings gap")
    earnings_exit_time: time = time(15, 30)
    max_order_actions_per_10min: int = Field(30, ge=5, description="submits + cancels; more trips HALT_ALL")
    max_order_errors_per_10min: int = Field(5, ge=1, description="broker rejections; more trips HALT_ALL")

    @model_validator(mode="after")
    def _dte(self) -> RiskConfig:
        if self.max_dte < self.min_dte:
            raise ValueError("max_dte must be >= min_dte")
        if self.delta_min is not None and self.delta_max is not None and self.delta_min > self.delta_max:
            raise ValueError("delta_min must be <= delta_max")
        self.universe = [s.upper() for s in self.universe]
        for e in self.earnings:
            e.symbol = e.symbol.upper()
        return self


class ExitConfig(BaseModel):
    stop_loss_pct: float = Field(gt=0, lt=100)
    take_profit_pct: float = Field(gt=0)
    close_at_dte: int = Field(ge=-1, description="-1 turns this off; then close_all_at must close same-day expiries")
    broker_stop: bool = True
    exit_order_ttl_seconds: int = Field(60, ge=10)
    # The broker stop only ever moves up: to breakeven once a position is up this much...
    breakeven_at_pct: float | None = Field(None, gt=0)
    # ...and, if both are set, trailing trail_pct below the mark once it's up trail_start_pct.
    trail_start_pct: float | None = Field(None, gt=0)
    trail_pct: float | None = Field(None, gt=0, lt=100)
    max_hold_minutes: int | None = Field(None, ge=1, description="time stop: close positions held this long")
    min_stop_step_pct: float = Field(3.0, ge=0, description="move the broker stop only by at least this % of entry")
    close_all_at: time | None = Field(None, description="close every position at this time of day")

    @model_validator(mode="after")
    def _ratchets(self) -> ExitConfig:
        if self.breakeven_at_pct is not None and self.breakeven_at_pct >= self.take_profit_pct:
            raise ValueError("breakeven_at_pct must be below take_profit_pct")
        if (self.trail_start_pct is None) != (self.trail_pct is None):
            raise ValueError("set both trail_start_pct and trail_pct, or neither")
        return self


class OrderConfig(BaseModel):
    entry_offset: float = Field(0.2, ge=0, le=1)
    entry_ttl_minutes: int = Field(5, ge=1)


class AnthropicSettings(BaseModel):
    model: str = "claude-opus-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    max_tokens: int = Field(16000, ge=1024)


class OpenAICompatSettings(BaseModel):
    model: str
    max_tokens: int = Field(4000, ge=256)
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    # Optional overrides of the shared llm settings, e.g. to fit a free tier's token limits.
    daily_token_budget: int | None = Field(None, ge=1000, description="skip AI cycles once today's tokens reach this")
    max_tool_calls: int | None = Field(None, ge=0, le=20)
    contracts_per_underlying: int | None = Field(None, ge=2, le=40)
    # Provider-specific request fields, e.g. {"reasoning_format": "hidden"} for Qwen on Groq.
    extra_body: dict[str, Any] = Field(default_factory=dict)


class FallbackModel(BaseModel):
    """A model the bot switches to when every model before it is out for the day: its daily token budget
    is used, or the provider rate-limited it. Each Groq model has its own free daily quota, so another
    model on the same key is the simplest fallback. gemini and openrouter need GEMINI_API_KEY or
    OPENROUTER_API_KEY in .env; a fallback whose key isn't set is skipped."""

    provider: Literal["anthropic", "xai", "groq", "gemini", "openrouter"]
    model: str
    max_tokens: int = Field(2000, ge=256)
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    daily_token_budget: int | None = Field(None, ge=1000, description="stop using this model once today's tokens on it reach this")
    extra_body: dict[str, Any] = Field(default_factory=dict)


class LLMConfig(BaseModel):
    provider: Literal["anthropic", "xai", "groq"] = "anthropic"
    max_tool_calls: int = Field(6, ge=0, le=20)
    contracts_per_underlying: int = Field(12, ge=2, le=40)
    max_proposals: int = Field(3, ge=1, le=10)
    anthropic: AnthropicSettings = AnthropicSettings()
    xai: OpenAICompatSettings = OpenAICompatSettings(model="grok-4.7", max_tokens=16000)
    groq: OpenAICompatSettings = OpenAICompatSettings(
        model="openai/gpt-oss-120b", max_tokens=2000, reasoning_effort="low",
        max_tool_calls=2, contracts_per_underlying=6, daily_token_budget=180_000,
    )
    fallbacks: list[FallbackModel] = Field(default_factory=list)  # tried in order once the provider above is out

    @property
    def active(self) -> AnthropicSettings | OpenAICompatSettings:
        return getattr(self, self.provider)

    @property
    def model(self) -> str:
        return self.active.model

    @property
    def daily_token_budget(self) -> int | None:
        return getattr(self.active, "daily_token_budget", None)

    @property
    def tool_call_budget(self) -> int:
        override = getattr(self.active, "max_tool_calls", None)
        return self.max_tool_calls if override is None else override

    @property
    def candidates_per_underlying(self) -> int:
        override = getattr(self.active, "contracts_per_underlying", None)
        return self.contracts_per_underlying if override is None else override


class AlertConfig(BaseModel):
    provider: Literal["ntfy", "telegram", "console"] = "ntfy"
    ntfy_server: str = "https://ntfy.sh"
    notify_rejections: bool = False
    dedupe_seconds: int = Field(600, ge=0)
    signals: bool = True  # push intraday setups (opening-range breaks, strong first half-hour)
    signal_interval_minutes: int = Field(5, ge=1)
    signal_first_half_hour_pct: float = Field(0.5, gt=0)


class ScanConfig(BaseModel):
    """`paperbot scan`: chart signals on your Robinhood positions. Information only, never orders.
    The account and server come from ROBINHOOD_ACCOUNT_NUMBER and ROBINHOOD_MCP_URL in .env.
    Each field's description is what the web page's settings form shows."""

    watchlist: list[str] = Field([], description="Also scanned, held or not, e.g. SPY, QQQ")
    bar_minutes: Literal[1, 5, 10, 30] = Field(5, description="Candle size for every signal, in minutes")
    cooldown_minutes: int = Field(30, ge=0, description="The same signal on the same symbol and level, at most this often")
    rsi_period: int = Field(14, ge=2, description="RSI length, in candles")
    rsi_oversold: float = Field(30, gt=0, lt=50, description="RSI under this is oversold")
    rsi_overbought: float = Field(70, gt=50, lt=100, description="RSI over this is overbought")
    level_tolerance_pct: float = Field(0.15, gt=0, le=2, description="How close (%) a low must come to a support to test it")
    min_cross_atr: float = Field(0.2, ge=0, description="A VWAP, EMA or MACD cross must clear this many ATRs, so chop is ignored")
    vwap_touch_atr: float = Field(0.1, ge=0, description=(
        "A VWAP bounce or rejection needs the candle's low or high within this many ATRs of VWAP"))
    alert_weak: bool = Field(False, description="Also alert weak signals (breakouts on light volume); they're always logged")
    one_alert_per_symbol: bool = Field(True, description="One phone alert per symbol per scan, listing every new signal")
    volume_spike_ratio: float = Field(3.0, gt=1, description="A volume spike is this many times the 20-candle average")
    breakout_volume_ratio: float = Field(1.5, gt=0, description="Below this volume ratio, a breakout is weak")
    # Live scanning (`scan --live`). live_symbols is the starting list; `paperbot live-symbols` (or anything
    # that calls paperbot.livelist, like an AI tool or the web page) changes it while the scan runs, through
    # live_symbols_file (in log_dir), which then wins over this list.
    live_symbols: list[str] = Field([], description="What `scan --live` scans; empty: everything you hold plus the watchlist")
    live_symbols_file: str = Field("live_symbols.json", description="File in log_dir holding the list as changed while running")
    tick_seconds: int = Field(0, ge=0, le=300, description="Between candle closes, check the live price this often (0 = off)")
    tick_cooldown_minutes: int = Field(10, ge=0, description="The same live level cross, at most this often")
    live_positions_seconds: int = Field(60, ge=0, description="Live checks re-read your positions at most this often")
    sessions: list[Literal["pre", "regular", "post"]] = Field(
        ["pre", "regular", "post"], min_length=1, description=(
            "When the scanner runs, weekdays ET: pre-market 4:00-9:30 AM, regular 9:30 AM-4:00 PM, after hours 4:00-8:00 PM"))
    # Chart patterns (formations.py), in thinkorswim's groups. Names for the two lists are the pattern kinds
    # (`paperbot patterns` lists them), e.g. doji, double_top, williams_fractal_high.
    pattern_groups: list[Literal["classic", "candlestick", "fibonacci"]] = Field(
        ["classic", "candlestick", "fibonacci"], description="Pattern groups that are on, like thinkorswim's tabs")
    disabled_patterns: list[str] = Field([], description="Patterns never reported")
    log_only_patterns: list[str] = Field(
        ["doji", "long_legged_doji", "spinning_top", "williams_fractal_high", "williams_fractal_low"],
        description="Patterns logged but not alerted")
    candle_trend_atr: float = Field(0.75, gt=0, description="A reversal candle needs a move into it of this many ATRs")
    pattern_swing_atr: float = Field(1.0, gt=0, description="Chart patterns are drawn from swings of this many ATRs")
    pattern_tolerance_atr: float = Field(0.5, gt=0, description="Equal tops and bottoms are within this many ATRs")
    pattern_lookback: int = Field(120, ge=20, description="Candles a chart pattern can span")
    flag_pole_atr: float = Field(3.0, gt=0, description="A flag's or pennant's pole moves at least this many ATRs")
    fib_swing_atr: float = Field(3.0, gt=0, description="Fibonacci retracements of swings at least this many ATRs")
    forming_near_atr: float = Field(0.5, gt=0, description=(
        "Warn of a top or bottom still forming once price closes within this many ATRs of its neckline"))
    # The bigger picture (context.py): what the last few hours and a slower candle say, checked against
    # every signal from the faster candles. A signal that goes against it is flagged, or held back.
    context_mode: Literal["warn", "suppress", "off"] = Field("warn", description=(
        "Signals against the bigger picture: warn (flag the alert, high priority), suppress (log only), off"))
    context_minutes: int = Field(180, ge=30, le=390, description="How far back the bigger picture looks, in minutes")
    context_bar_minutes: Literal[15, 30, 60] = Field(15, description="Slower candle for the bigger picture's patterns and trend")
    context_threshold: int = Field(3, ge=1, description="Bigger-picture score at which it counts as bullish or bearish")
    oauth_callback_port: int = Field(33418, ge=1024, le=65535, description="Local port for the Robinhood sign-in redirect")


class OrderFlowConfig(BaseModel):
    """Order flow from the trade tape (orderflow.py): absorption, imbalances, exhaustion, divergence, and
    Fabio Valentini's trend and failed-breakout models. Off by default: on the free IEX feed the signals
    don't match the market's real order flow (docs/ORDERFLOW.md). Turn on with feed: sip, which needs
    Alpaca's paid market data plan for live use. `paperbot orderflow` studies any past day either way."""

    enabled: bool = False
    feed: Literal["iex", "sip"] = "iex"
    bar_minutes: Literal[1, 5, 15] = 5
    delta_multiple: float = Field(1.5, gt=0, description="Heavy aggression: |delta| this many times the recent average")
    imbalance_ratio: float = Field(2.5, gt=1, description="Diagonal imbalance ratio for a stacked imbalance")
    stacked_levels: int = Field(3, ge=2, description="Consecutive imbalanced prices that make a stack")
    confirm_bars: int = Field(3, ge=1, description="Bars an absorption has to be confirmed in")
    alert_setups: bool = True  # phone alert, and an AI cycle if schedule.trigger_cycle_on_signal, on each setup
    in_prompt: bool = True  # give the AI a summary of today's order flow each cycle
    in_scan: bool = True  # show order-flow signals on the scanner's charts and alerts


class Config(BaseModel):
    account_number: str | None = None
    options_feed: Literal["indicative", "opra"] = "indicative"
    stock_feed: Literal["iex", "sip", "delayed_sip"] = "iex"
    log_dir: Path = Path("logs")
    schedule: ScheduleConfig = ScheduleConfig()
    risk: RiskConfig
    exits: ExitConfig
    orders: OrderConfig = OrderConfig()
    llm: LLMConfig = LLMConfig()
    alerts: AlertConfig = AlertConfig()
    scan: ScanConfig = ScanConfig()
    orderflow: OrderFlowConfig = OrderFlowConfig()
    strategy: str

    @model_validator(mode="after")
    def _cross_checks(self) -> Config:
        if self.exits.close_at_dte >= self.risk.min_dte:
            raise ValueError(
                "exits.close_at_dte must be below risk.min_dte, or new entries would be closed immediately"
            )
        if self.exits.close_at_dte < 0 and self.exits.close_all_at is None:
            raise ValueError("exits.close_at_dte -1 needs exits.close_all_at, or expiring options could be held to expiry")
        if self.risk.min_dte == 0:
            # Alpaca stops accepting orders that open positions from 15:30 ET on expiration day,
            # and starts handling expiring positions itself; be flat and done well before then.
            if self.exits.close_all_at > time(15, 30):
                raise ValueError("with same-day expiries (min_dte 0), exits.close_all_at must be 15:30 or earlier")
            if self.schedule.no_new_entries_after > time(15, 25):
                raise ValueError("with same-day expiries (min_dte 0), schedule.no_new_entries_after must be 15:25 or earlier")
        return self

    @classmethod
    def load(cls, path: str | Path) -> Config:
        path = Path(path)
        cfg = cls.model_validate(yaml.safe_load(path.read_text()))
        if not cfg.log_dir.is_absolute():
            cfg.log_dir = path.parent / cfg.log_dir
        return cfg


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader: KEY=VALUE lines, existing environment variables win."""
    path = Path(path)
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)
