# Every trading parameter

Run `.venv/bin/python -m paperbot check-config --broker` to see the effective values, the
numbers they imply, and warnings. This page explains each setting: what it does, its default,
and which direction is safer. Research references are in [RESEARCH.md](RESEARCH.md).

"Safer" means less risk per day. It doesn't mean more profit: tighter settings also mean fewer
trades and smaller winners.

## schedule

| Parameter | Default | What it does | Safer |
|---|---|---|---|
| `timezone` | America/New_York | Market time for every time of day below | – |
| `decision_interval_minutes` | 15 | How often the AI is consulted | Higher (fewer trades, fewer AI tokens) |
| `watch_interval_seconds` | 30 | How often code checks exits, stops, the loss limit and phone commands | Lower |
| `trading_start` | 09:45 | No AI cycles before this; skips the most volatile 15 minutes | Later |
| `trading_end` | 15:45 | No AI cycles after this | Earlier |
| `no_new_entries_after` | 15:15 | Entry cutoff | Earlier |
| `trigger_cycle_on_signal` | false | Run an extra AI cycle as soon as a setup alert fires (on in the aggressive profile) | false |

On early-close days (Nov 27 and Dec 24, 2026 close at 13:00) every time of day shifts earlier
by the same amount, using Alpaca's exchange calendar.

## risk: limits on every entry

| Parameter | Default | What it does | Safer |
|---|---|---|---|
| `universe` | SPY QQQ AAPL MSFT NVDA | Only options on these | Fewer, more liquid names |
| `max_contracts_per_order` | 5 | Contract cap per order | Lower |
| `max_position_usd` | 2000 | Premium cap per contract symbol, pending buys included | Lower |
| `max_positions` | 3 | Distinct contracts held or pending | Lower |
| `max_same_direction_positions` | 2 | Calls (or puts) at once; the universe is ~0.98 correlated | Lower |
| `manage_external_positions` | true | Put stops and exits on positions paperbot didn't open (you get one alert either way) | – |
| `soft_loss_limit_pct` | 1.0 | Past this drawdown, per-trade risk halves for the day | Lower |
| `max_entries_per_day` | 6 | Entry orders per day | Lower (overtrading is the most common discipline failure) |
| `daily_loss_limit_pct` | 2.0 | Down this much from start-of-day equity: sell everything, stop for the day | Lower (0.5–2% is typical) |
| `min_dte` / `max_dte` | 2 / 14 | Days to expiration allowed. 0 allows same-day expiries, but only with `close_at_dte: -1`, a `close_all_at` of 15:30 or earlier, and an entry cutoff of 15:25 or earlier, because Alpaca stops accepting opening orders at 15:30 on expiration day | Longer-dated (slower theta and gamma) |
| `max_spread_pct` | 10 | (ask − bid) / mid on a fresh quote; at 10%, a round trip costs ~6% of premium | Lower (10% is the common rule of thumb) |
| `min_hold_minutes` | 20 | The AI can't sell younger positions; code exits can | Higher (curbs churn); must be ≥ 1 |
| `reentry_cooldown_minutes` | 30 | No new buy on an underlying after selling it | Higher |
| `max_risk_per_trade_pct` | 0.5 | An entry's loss at its stop, as % of equity | Lower (0.25–0.5% is typical) |
| `stop_slippage_factor` | 1.5 | Sizing assumes a stop-out loses this × the stop distance; the worst simulated fills hit ~2× | Higher |
| `max_theta_pct_per_day` | 10 | Refuse entries losing more than this % of price per day to decay (2 DTE ATM SPY: ~25%) | Lower |
| `max_consecutive_losses` | 3 | Losing trades in a row before entries stop for the day | Lower |
| `entry_cooldown_minutes` | 5 | Minimum gap between entries | Higher |
| `max_quote_age_seconds` | 60 | Entries refused on older quotes | Lower |
| `max_stock_data_age_seconds` | 300 | Underlyings with an older last trade are left out of the prompt | Lower |
| `max_order_actions_per_10min` | 30 | More order submits plus cancels than this trips HALT_ALL (runaway protection) | Lower |
| `max_order_errors_per_10min` | 5 | More broker rejections than this trips HALT_ALL | Lower |
| `delta_min` / `delta_max` | 0.30 / 0.70 | Allowed \|delta\| for entries | Narrower around 0.5 |
| `min_open_interest` | 100 | Prior-day open interest floor | Higher |
| `oi_waived_below_spread_pct` | 3 | No OI floor when the live spread is this tight (liquid SPY/QQQ weeklies often show OI < 100) | Lower |
| `blackout` | FOMC 2026-10-28 13:45–15:00 | No entries in these windows; no date means every day | Add CPI/FOMC/jobs days |
| `earnings` | MSFT 10-28, AAPL 11-02, NVDA 11-18 | Earnings reports (bmo/amc) | Keep up to date: stale dates are the main risk |
| `earnings_block_days` | 2 | Trading days before the earnings gap with no new entries | Higher |
| `earnings_exit_time` | 15:30 | Positions in that stock are closed from this time on the last session before the gap | Earlier |

The daily loss budget also applies: an entry is refused if its loss at the stop wouldn't fit in
what's left of today's limit.

## exits: all decided by code

| Parameter | Default | What it does | Safer |
|---|---|---|---|
| `stop_loss_pct` | 35 | Code stop and broker stop level, from entry | Lower |
| `take_profit_pct` | 60 | Code take-profit | – (lower takes profit sooner, but needs a higher win rate) |
| `close_at_dte` | 1 | Close anything this close to expiration; must be below `min_dte`. -1 turns it off, which requires `close_all_at` | Higher |
| `broker_stop` | true | Keep a GTC stop order at Alpaca for every position | true |
| `exit_order_ttl_seconds` | 60 | An unfilled exit limit is replaced by a market order | Lower |
| `breakeven_at_pct` | 30 | Once up this much, the broker stop moves to entry | Lower |
| `trail_start_pct` / `trail_pct` | off | Trailing stop once up `trail_start_pct` | On |
| `max_hold_minutes` | off | Time stop | On for scalps |
| `min_stop_step_pct` | 3 | The broker stop moves up only in steps of this % of entry. Without it, a trailing stop would be replaced every tick and trip runaway protection. | – |
| `close_all_at` | off | Close everything at this time each day | On if you never want overnight risk |

With the defaults, reward:risk is 1.71, so the strategy breaks even at a 37% win rate before
costs and about 43% after spread costs at the widest allowed spread. `check-config` recomputes
this for your settings.

`paperbot simulate-exits` replays these exit settings, and a grid around them, on real
historical option bars. On 60 days of SPY and of QQQ, breakeven stops helped in nearly every
pair and the current settings ranked in the top group. Finer differences weren't stable, so
don't tune to the single best row. Details are in [RESEARCH.md](RESEARCH.md), finding 16.

## orders

| Parameter | Default | What it does | Safer |
|---|---|---|---|
| `entry_offset` | 0.2 | Entry limit = mid + offset × (ask − mid); 0 = mid, 1 = ask | Lower (cheaper fills, fewer fills) |
| `entry_ttl_minutes` | 5 | Cancel unfilled entries after this | – |

Alpaca's paper engine fills buys at the ask and sells at the bid, with no price improvement. So
in paper, a limit below the ask fills only if the ask comes down to it. Expect a lower fill rate
than live. `paperbot report` shows the fill rate.

## llm

| Parameter | Default | What it does |
|---|---|---|
| `provider` | groq | anthropic, xai or groq. Each has its own block for model and output size. |
| `max_tool_calls` | 6 (Groq: 2) | Research tool calls per cycle |
| `contracts_per_underlying` | 12 (Groq: 6) | Candidates shown in the prompt |
| `max_proposals` | 3 | Proposals considered per cycle; extras are dropped |
| `anthropic.effort` | high | Claude's thinking effort |
| `groq.reasoning_effort` | low | gpt-oss reasoning effort |
| `groq.daily_token_budget` | 180000 | Once today's AI tokens reach this, AI cycles stop for the day (exits keep running) |
| `*.max_tokens` | 16000 (Groq: 2000) | Answer length cap; on Groq it counts toward the per-minute token limit |

## alerts

| Parameter | Default | What it does |
|---|---|---|
| `provider` | ntfy | ntfy, telegram or console |
| `notify_rejections` | false | Also push rejected proposals |
| `dedupe_seconds` | 600 | Identical alerts within this window are sent once |
| `signals` / `signal_interval_minutes` / `signal_first_half_hour_pct` | on / 5 / 0.5 | Setup alerts |
| `HEALTHCHECK_URL` (in .env) | unset | Dead man's switch ping after each healthy watch tick |

## Alpaca order parameters: what paperbot uses

From Alpaca's order API reference, for single-leg options:

| Alpaca parameter | Options support | paperbot |
|---|---|---|
| `symbol`, `qty`, `side` | yes | Always set; whole-contract quantities only |
| `type` | market, limit, stop, stop_limit | limit for entries, limit at bid then market for exits, stop for broker stops, market for flatten |
| `time_in_force` | day, gtc | day for entries and exits, gtc for broker stops |
| `limit_price` / `stop_price` | yes | Rounded to the option tick ($0.01 under $3, $0.05 above, pennies for SPY/QQQ/IWM) |
| `client_order_id` | yes | Deterministic per cycle, so retries can't double-fill |
| `position_intent` | buy_to_open, sell_to_close, … | Always set: sells are sell_to_close, so they can't open shorts |
| `stop_limit` type | yes | Not used. A stop-limit can leave a position open when the price gaps through its limit, and a stop-market at least exits. |
| `trailing_stop` type | equities only | Trailing is done by ratcheting the broker stop instead |
| `notional` | equities only | Not applicable |
| `extended_hours` | not for options | Not applicable |
| `order_class` bracket/oco/oto | not for options | Not applicable; stops and take-profit are managed by code |
| `order_class: mleg`, `legs` | yes (level 3) | Not used. Debit spreads cap risk and cut IV/theta exposure, but they change the strategy, so that's your call. |
| `advanced_instructions` | Elite accounts | Not used |

Alpaca also stops accepting orders that open positions from 3:30 pm ET on expiration day, and
auto-exercises in-the-money long options at expiry. paperbot closes positions before expiration
day (`close_at_dte: 1`), so neither applies to it.

## Account settings: read-only checks

`check-config --broker` reads these but never changes them:

| Setting | Your paper account | Notes |
|---|---|---|
| Options trading level | 3 | 2 is needed to buy calls and puts; paperbot refuses to start below 2 |
| `no_shorting` | off | Optional extra guard. paperbot never shorts, but you can turn this on in Alpaca's dashboard. |
| `trading_blocked` | no | paperbot refuses to start if blocked |
| Pattern-day-trader rule | – | Eliminated by FINRA effective 2026-06-04; brokers may keep the old rules until 2027-10-20 |
| Rate limits | 200 requests/min | paperbot uses well under 50/min; alpaca-py retries 429s automatically |
