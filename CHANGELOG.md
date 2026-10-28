# Changelog

## 2026-10-28 (11:35 AM): an extreme RSI overrides other signals

- When the 5-minute RSI on a signal's candle is under `scan.rsi_extreme_low` (15), sell signals are held back
  from the phone (still printed, journaled with `held_back`, and tagged "held back · RSI 12 extremely
  oversold" on the web page), and buy signals go out at high priority, titled "BUY · RSI 12 extremely
  oversold", without the bigger-picture warning. Over `rsi_extreme_high` (78), the other way round. Live
  level crosses between candles follow the last completed candle's RSI. `rsi_extreme_override: false` turns it off.
- Two new signals: RSI extremely oversold (crossing under 15, a buy) and extremely overbought (crossing over
  78, a sell). Every signal now carries its candle's RSI (`ChartSignal.rsi`, `rsi` in the journal).
- Replayed on today's candles for the 7 live symbols: of 1,071 signals, 12 would have been held back (among
  them SPCX's 09:35-09:45 sells at RSI 8.6-12.8 and TSLA's 10:05 breakdown at 14.3) and 13 would have taken
  priority. 5 new tests; 267 pass.

## 2026-10-28 (11:30 AM): VWAP bounces and rejections need a real touch

- A VWAP bounce or rejection now needs the candle's low or high within `scan.vwap_touch_atr` (0.1) ATRs of
  VWAP, instead of within `level_tolerance_pct` (0.15%) of it. On NVDA (about $218, an ATR near 50 cents) the
  old band was 33 cents, so candles that stopped 24-26 cents under VWAP were called rejections (10:05 and
  11:05 today). Replayed on today's NVDA candles, 10:05, 10:35 and 11:05 no longer fire; the 09:45 rejection
  (3 cents short) and the 09:35 bounce still do. Support and resistance tests keep `level_tolerance_pct`.
- Editable on the web page's Settings tab. 2 new tests; 262 pass.

## 2026-10-27 (6:10 PM): pre-market and after-hours scanning

- `scan.sessions` (default `[pre, regular, post]`): the live scanner runs 4:00–20:00 ET instead of stopping at
  16:00, on Robinhood's extended-hours candles and the latest trade in or out of regular hours. VWAP, the
  opening range, high and low of day and prior-day levels still come from the regular session; extended-hours
  volume is compared with extended-hours volume. Editable on the web page's Settings tab.
- The last candle of a session is now scanned when it closes. Before, the 15:55 candle never was.
- The web page shows the session (Pre-market, Market open, After hours, Market closed). Between sessions the
  scanner checks in every candle, so the page says "Scanner idle · resumes ..." instead of "live" with a stale
  candle. Extended-hours candles are drawn faded.
- Bars are fetched for the 3 sessions the scanner uses, in as many calls as it takes to keep each reply
  under the connector's 1 MB message limit.

## 2026-10-27 (1:30 PM): the bigger picture, and tops and bottoms before they break

- New `context.py`: every scan signal is checked against the last 3 hours and the 15-minute chart: chart
  patterns that broke and still hold (5- and 15-minute), heads and shoulders and triple tops and bottoms still
  forming, lower highs and lower lows (or higher and higher), the 15-minute trend, MACD and RSI, and VWAP. A
  signal that goes against it gets "against the bigger picture" in the title, high priority, and the reasons
  (`scan.context_mode: warn`), or is only logged (`suppress`). Scans print the bigger picture per symbol; the
  journal and the web page's state carry `context`, `context_score` and `conflict`.
- Six new pattern kinds: head and shoulders, inverse, double and triple tops and bottoms **forming**, alerted
  when price first closes within `scan.forming_near_atr` (0.5) ATRs of the neckline, before the break.
- New settings: `forming_near_atr`, `context_mode`, `context_minutes`, `context_bar_minutes`,
  `context_threshold`. `formations.chart_breaks` and `formations.forming` return patterns as data. 12 new tests;
  249 pass.

## 2026-10-27 (afternoon): web page

- `paperbot web`: a local page (127.0.0.1:8765) with a dashboard of the live scanner (candles, signal markers,
  levels, positions with live P&L, a live signal feed, the live symbol list) and pages for signal history,
  patterns, scanner settings and the paper bot's kill switch, commands and journal.
- The live scan writes `logs/scan_state.json` after every check, and rereads `logs/settings.json` (scanner
  settings changed on the page, kept over config.yaml) before every check. New `paperbot/overrides.py`.
- Every scan setting has a description, shown on the settings page. The journal records whether a signal was
  weak. Starlette and uvicorn are now listed dependencies (they were already installed with `mcp`).
- Changes need a per-start token and a local Host header. 9 new tests.

## 2026-10-27 (1 PM): thinkorswim candlesticks, a live list you can change, 10-second checks

- The rest of thinkorswim's Candlestick tab: concealing baby swallow, downside and upside gap three methods,
  high and low price gapping play, side-by-side white lines (both ways), three stars in the south, Williams
  fractals and ZigZag steps. `paperbot patterns` lists all 110 kinds by group with what the scan does.
- Pattern settings in `config.yaml`: `pattern_groups`, `disabled_patterns`, `log_only_patterns`
  (doji, spinning tops and fractals by default) and `candle_trend_atr`.
- The live scan's symbols and timing moved to `config.yaml` (`live_symbols`, `tick_seconds: 10`,
  `live_positions_seconds`); the launchd job is now plain `scan --live`. `paperbot live-symbols
  add|remove|set|reset` (or `paperbot.livelist.LiveList`, for an AI tool or a web page) changes the list
  while it runs; new symbols are scanned within one check. Live checks reuse positions for up to a minute,
  so a 10-second check is two quote calls. 228 tests pass.

## 2026-10-27 (afternoon): chart, candlestick and Fibonacci patterns

- New `formations.py`: all 16 classic patterns from thinkorswim's pattern list (ascending, descending and
  symmetrical triangles, channels up and down, double and triple tops and bottoms, rising and falling
  wedges, flags, pennants, head and shoulders and inverse, rectangles), about 55 candlestick patterns, and
  Fibonacci patterns (AB=CD, Gartley, Bat, Butterfly, Crab, Three Drives, retracement tests). Each is an
  event on the bar that completes it, with the same cooldown, with/against and alert rules as the rest.
- New `scan` settings: `pattern_swing_atr`, `pattern_tolerance_atr`, `pattern_lookback`, `flag_pole_atr`,
  `fib_swing_atr`. 14 new tests on drawn charts; 224 pass. On today's real bars the patterns added 11–22
  signals per symbol, about a third of them weak (printed, not alerted).

## 2026-10-27: option positions and a SPY-only live scan

- The scanner reads option positions too (two more read-only tools: `get_option_positions`,
  `get_option_instruments`). Calls and short puts count as bullish, puts and short calls as bearish,
  and alerts say whether each signal goes with or against them.
- `scan --only SPY` scans just the named symbols, for a 5-minute live scan beside the 2-hourly job.
  Scans reread the journal after each fetch, so two running at once don't send the same signal.
- TSLA added to `scan.watchlist`. 207 tests pass.
- A launchd job (`com.paperbot.spy-live`) keeps the SPY live scan running every trading day; its log
  is `~/Library/Logs/paperbot/spy-live.log`. The 2-hourly job moved to 11:32, 13:32, 15:32 and 16:07.
- `scan --live --tick 30` checks the live price every 30 seconds between candle closes and alerts level
  crosses inside the forming candle (VWAP, high/low of day, opening range, prior-day levels, pivots), with
  live option marks and P&L. Two more read-only tools: `get_equity_quotes`, `get_option_quotes`. 210 tests pass.

## 2026-10-26 (night): Robinhood chart signals

- `paperbot scan [--live]` reads a Robinhood account's positions over Robinhood's MCP server
  and pushes buy and sell signals: VWAP, RSI oversold/overbought on 5-minute bars, support
  tests, breakouts and breakdowns, EMA and MACD crosses, Bollinger re-entries and squeezes,
  reversal candles at levels, volume spikes, and daily golden/death crosses and 52-week breaks.
- Read-only by construction: only three `get_` tools can be called; anything else is refused
  before a request is sent. `paperbot robinhood-login` signs in with OAuth in the browser.
- Alerts say whether a signal goes with or against your position. Noise filters (ATR distance,
  swing before a cross, family cooldown, one alert per symbol per scan) took a real choppy NVDA
  session from 61 alerts to 33.
- New dependency: `mcp` (the MCP Python SDK).
- `scan --catch-up` and a weekday launchd schedule (11:30, 13:30, 15:30, 16:05 ET) for scans every
  2 hours. Alerts show each signal's bar time and stay under ntfy's 4 KB limit. 203 tests pass.

## 2026-10-26: strategy profiles and aggressive scalping

- `--profile default|scalp|aggressive` picks the strategy at startup; `paperbot profiles`
  compares them side by side.
- New `config.aggressive.yaml`: 0–1 DTE SPY/QQQ options, the AI every 5 minutes plus an extra
  cycle whenever a setup alert fires (`trigger_cycle_on_signal`), 10 s exit checks, −15%/+20% with
  breakeven at +8% and an 8% trail after +12%, a 15-minute time stop, flat by 15:15, up to 20
  entries a day. Per-trade risk, the daily loss limit, the streak breaker and runaway protection
  are unchanged.
- Same-day expiries are allowed safely: `min_dte: 0` requires `close_at_dte: -1` (no
  close-by-DTE), a `close_all_at` of 15:30 or earlier, and an entry cutoff of 15:25 or earlier
  (Alpaca's expiration-day rule).
- `check-config` estimates when a Groq daily token budget will run out.
- Simulated on 240 real 0–1 DTE SPY trades with no-edge entries: −2.1% per trade, worst −17%
  (docs/RESEARCH.md, finding 35).
- 177 tests pass.

## 2026-10-25 (night): round 2, research-driven improvements

Snapshot of the code before this round: `.snapshots/round2-start/`. Research findings 11–28
and the round 2 source list are in [docs/RESEARCH.md](docs/RESEARCH.md); 140 sources are
cited in total. Every change tightens risk, adds information, or adds a tool. No limit was
loosened.

**Entries**
- Earnings blackout: no new entries 2 trading days before a report. Positions in that stock are
  closed by 15:30 on the last session before the gap. Dates: MSFT 10-28 and AAPL 11-02, both after
  the close; NVDA 11-18 is projected.
- Filters on a fresh option snapshot: |delta| 0.30–0.70, open interest ≥ 100, and decay
  ≤ 10% of price per day. Measured: 2-DTE near-the-money SPY decays ~25%/day.
- At most 2 positions in the same direction, because the universe is ~0.98 correlated.
- The spread cap is tightened from 12% to 10%.
- Blackouts on the mornings of the jobs report (11-06) and CPI (11-10), 09:45–10:15.

**Sizing**
- `stop_slippage_factor: 1.5`: sizing assumes a stop-out loses 1.5× the stop distance. The largest
  entry falls from ~$1,430 to ~$950 of premium.
- Soft loss limit: past a 1% drawdown, per-trade risk halves for the rest of the day (one alert).

**Safety and reliability**
- Runaway protection: more than 30 order submits and cancels, or 5 broker rejections, in 10
  minutes trips HALT_ALL with an urgent alert.
- If Alpaca rejects stop orders 3 times in a row, paperbot falls back to code stops and alerts
  once. Alpaca's docs disagree on whether options support stops; the current API reference says
  they do.
- Early-close days (11-27, 12-24) shift every cutoff, using Alpaca's calendar.
- Refuses to start if the account is blocked or below options level 2. Pushes config warnings at
  startup.
- Underlyings whose data is over 5 minutes old are left out; if all are stale, the cycle is skipped.
- Fix: with a trailing stop, the broker stop was replaced on every tick while the price rose,
  which would trip runaway protection within minutes. Stops now move in steps of 3% of entry
  (`min_stop_step_pct`); a size change still replaces the stop at once.
- Reconciliation: a position paperbot didn't open (for example a manual trade in the same
  account) gets one alert. `manage_external_positions` (default true) decides whether paperbot
  still puts stops and exits on it.
- AI outage alerts fire on the 1st, 4th and 16th failure in a row, plus one when it recovers,
  instead of every cycle.
- Fix: a broker stop that fills while paperbot is replacing it no longer leads to a stop order
  for a position that's gone. An entry with no quote now says "no valid quote".
- Slow-tick alert (over 20 s). Optional `HEALTHCHECK_URL` dead man's switch. Warns if `.env` is
  readable by other users.

**Measurement**
- Fill alerts with slippage against the decision-time mid. The report shows fill rate, slippage,
  Monte Carlo drawdowns, MAE/MFE and rejection reasons.
- `paperbot replay --date`: a day's decisions, orders and fills.
- Edge check in the report: probabilistic Sharpe ratio and minimum track-record length (Bailey &
  López de Prado), so you can see how many trades it would take to trust the result.
- `paperbot preflight`: pre-open checklist (broker, account, clock skew, AI, config, kill switch,
  running instance). Startup also alerts if the clock is more than 5 s off Alpaca's.
- `paperbot eval-prompts`: replays saved prompts to the AI, with tools off and no orders,
  measuring how often it repeats its own decision and agrees with the original run.
- Verified live: Groq called a read-only tool, accepted the result in OpenAI tool format, and
  returned a valid decision.
- The Friday summary reminds you to update next week's event dates.

**AI input**
- A compact candidate table (CSV-style), filtered to what the gate would accept, with daily
  decay and the maximum size the limits allow. A real prompt fell from 4,391 to 3,485 tokens.
- Gap size, whether it has filled, prior-day high/low, and implied daily move vs today's range.

- Candidate order rotates every cycle (LLM order effects); `max_qty` is labeled a ceiling.
- The system prompt now lists every limit the gate enforces (decay, delta band, direction cap,
  earnings, soft limit, streak), so fewer proposals get rejected.
- `groq.daily_token_budget: 180000`: AI cycles stop for the day once it's used (one alert);
  exits and stops keep running.
- The open-interest floor is waived for spreads ≤ 3%: real SPY/QQQ weeklies with tight spreads
  often show OI under 100.

**Rehearsal**
- `paperbot rehearse`: one full cycle on real data with the real AI, any time of day. It is
  always a dry run and keeps its own logs in `logs/rehearsal/`.

**Tools**
- `paperbot check-config [--broker]`: every parameter, implied numbers (e.g. a 43% breakeven
  win rate after costs), warnings and account checks. `docs/PARAMETERS.md` covers every setting
  and every Alpaca order parameter.
- `paperbot simulate-exits`: replays exit settings on real historical option bars, with a
  half-vs-half stability check. Result: breakeven helps consistently and +90% targets hurt; the
  current settings sit on a plateau near the top, so they were left alone.

**Verified:** 154 tests pass, including a randomized gate test that checks 6,000 orders against
every limit and catches removed limits. Real read-only checks ran against Alpaca: features, OI,
greeks, calendar, account settings and historical bars. Groq answered a real full-size
prompt with a valid decision.

**Takes effect on restart.** The running bot still uses the code from the last restart.

## 2026-10-25 (evening): research-driven improvements

The version that was running before this is saved in `.snapshots/baseline-2026-10-25/`
(code and config, no secrets or logs). To compare:
`diff -r .snapshots/baseline-2026-10-25/paperbot paperbot`. The research behind each change is
in [docs/RESEARCH.md](docs/RESEARCH.md).

Every change either tightens risk or adds information. No limit was loosened.

**Measurement**
- `paperbot report [--days N]`: round-trip trades rebuilt from broker fills, with P&L, win rate,
  expectancy, profit factor and losing streaks, and a warning below 30 trades.
- The daily summary now includes the day's closed trades and the AI tokens used.

**Risk (all entry-side, all tighter)**
- Entries are refused on option quotes older than 60 s.
- Losing-streak breaker: after 3 losing trades in a row, no new entries for the day, with an alert.
- 5-minute cooldown between new entries.
- Each entry's risk to its stop is capped at 0.5% of start-of-day equity.
- Entry blackout windows, including the FOMC decision on 2026-10-28, 13:45–15:00 ET.

**Exits**
- The broker stop moves to breakeven once a position is up 30%, and only ever moves up.
  Optional trailing stop (`trail_start_pct`, `trail_pct`).
- Optional time stop (`max_hold_minutes`) and end-of-day close (`close_all_at`).
- Fix: a replacement stop that fails the risk check no longer cancels the old stop first.
  Before, a rejected replacement could leave a position with no stop until the next tick.

**AI input**
- Intraday features computed by code (VWAP, opening range, first-half-hour return, RSI, ATR,
  EMA trend, relative volume) are added to the prompt.
- News text is stripped of invisible characters, normalized, capped in length and labeled
  untrusted; the prompt says never to follow instructions inside tool output.
- The decision interval is now 15 minutes, to stay inside Groq's free daily token limit.

**Alerts**
- Real-time setup alerts: opening-range breaks and strong first-half-hour moves, once per
  symbol per day, every 5 minutes during trading hours. Information only, never orders.

**Profiles**
- `config.scalp.yaml`: SPY/QQQ, 1–3 DTE, tight stops, breakeven and trailing stops, a 45-minute
  time stop, and everything closed by 15:50.

**Verified:** 113 tests pass, including 25 new ones. Breaking the stale-quote check, the
never-lower stop rule or the stop-replacement order each makes a test fail. Features were
computed from real Friday SPY bars, and Groq returned a valid decision for a real full-size
prompt (4,391 tokens).

**Takes effect on restart.** The running bot still uses the code it started with.

## 2026-10-25: provider setting

- `llm.provider`: anthropic (Claude), xai (Grok) or groq, set to groq.

## 2026-10-25: first version

- LangGraph agent on Autobelay's design, with Autobelay's five gaps fixed. See README.
