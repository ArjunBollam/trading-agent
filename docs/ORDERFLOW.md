# Order flow

[paperbot/orderflow.py](../paperbot/orderflow.py) reads the stock's trade tape: who was aggressive in each candle, where the session traded most, and whether aggression is being absorbed. It implements the ideas in three sources:

| Source | What the bot takes from it | Signals |
|---|---|---|
| Fabio Valentini, *The Only Orderflow Guide You'll Ever Need* | State first (in balance, or out of balance and trending), then location (the session's volume profile), then aggression at that location | Trend continuation long and short, failed breakout and failed breakdown |
| Axia Futures, *How To Start Using Footprint Charts* | The footprint: volume at each price, split into buyers lifting the offer and sellers hitting the bid. Delta, diagonal imbalances, stacked imbalances, exhaustion | Stacked buy and sell imbalances, buying and selling exhaustion, delta divergence |
| Bookmap, *Layering and Absorption* | Absorption: heavy aggression into a level that doesn't break. Don't act on it alone; wait for delta and CVD to turn and price to leave the level | Absorption at support or resistance, then *absorption confirmed* long or short |

The rules come from how each source describes the method, not from a copy of anyone's settings. Every signal has a tooltip on the dashboard ([explain.py](../paperbot/explain.py)).

## Signals

Each candle is judged only on the candles before it.
- **Normal** means the average of the last 20 candles.
- **Key levels:**
  - the prior day's high, low and close
  - VWAP
  - the 15-minute opening range
  - the session's point of control and value area (70% of volume)
- **Low-volume nodes** count only for the trend model.

| Signal | Rule |
|---|---|
| Absorption at support | Delta at most −1.5× normal with sellers ≥ 55% of volume, the candle's most-traded price in its bottom 40%, and a close in its upper half. Weak (logged, not alerted) unless at a key level. |
| Absorption confirmed: long | Within 3 candles: positive delta, CVD above the absorption candle's, and a close above that candle's high. Entry at the close, stop one tick under the absorbed level, target the next level (or 2R). If price closes beyond the level first, the absorption is dropped. |
| Stacked imbalances | 3+ prices in a row where buying at a price beats selling one price lower by 2.5:1, each at least the candle's average volume per price. Axia uses 3:1 on futures; stocks are deeper. |
| Exhaustion | A new 6-candle high (or low), a range at least normal, under a quarter of the average per-price volume at the extreme, and a close back in the other half. |
| Delta divergence | A higher high than the last swing high with a lower CVD and negative delta, or the mirror at a low. At most once per 30 minutes. |
| Trend continuation | Close above the value area high and VWAP; the candle dipped to VWAP, the value area high or a low-volume node; delta at least 1.5× normal. Stop under that level; target the session high or 2R. The mirror for shorts. |
| Failed breakout | One of the last 2 candles broke the value area high, opening-range high or prior-day high; this candle closes back inside with heavy selling. Stop above the breakout high; target the point of control. The mirror for failed breakdowns. |

## What it can and can't see

- **Who was aggressive.** Alpaca doesn't label trades as bought or sold. The tick rule infers it: a trade above the last different price counts as bought at the offer. On 23,536 SPY trades (10 minutes, full tape) it agreed with the quote-based method on **82.3% of volume**.
- **Layering.** Bookmap's layering is visible only in depth-of-book data, which Alpaca doesn't offer for stocks, so it isn't detected.
- **The data.** Alpaca's free plan gives live trades from IEX only. The full tape (SIP) is free 15 minutes late, which is fine for studies but not for live trading.

### IEX vs the full tape, 9–13 November 2026

| | IEX share of volume | 5-min delta correlation | CVD correlation | Same delta sign |
|---|---|---|---|---|
| SPY 11-09 | 4.3% | 0.23 | 0.19 | 63% |
| SPY 11-10 | 4.4% | 0.14 | 0.63 | 62% |
| SPY 11-11 | 4.2% | 0.33 | 0.39 | 62% |
| SPY 11-12 | 4.2% | 0.05 | −0.69 | 54% |
| SPY 11-13 | 3.6% | 0.44 | 0.87 | 68% |
| QQQ 11-09 | 2.3% | 0.00 | −0.36 | 47% |
| QQQ 11-10 | 1.8% | 0.17 | −0.81 | 54% |
| QQQ 11-11 | 2.4% | −0.25 | −0.68 | 65% |
| QQQ 11-12 | 2.3% | 0.11 | −0.62 | 45% |
| QQQ 11-13 | 1.7% | −0.20 | −0.67 | 56% |

IEX sees 2–4% of the trades:
- Its 5-minute delta has the same sign as the full tape's about as often as a coin flip.
- On four of five days, QQQ's IEX CVD moved *against* the full tape's.

Over the same 10 sessions, only 6 of 44 IEX signals, and 1 of 20 IEX setups, matched a full-tape signal on the same candle. **On the free feed, these signals don't reflect the market's real order flow.**

### Results on the full tape, same week

| Feed | Setups | Winners | Total | Per setup |
|---|---|---|---|---|
| SIP (whole market) | 26 | 16 | +6.4R | +0.24R |
| IEX (free, live) | 20 | 11 | +7.6R | +0.38R |

- R is measured on the underlying: stop or target first, a candle touching both counts as a loss, and anything else exits at the close. Costs are not included.
- The bot trades 0–1 DTE options with their own −15%/+20% exits, which these stops and targets don't map onto.
- One week of setups is no evidence of an edge.
- The IEX row is a different set of trades (1 in common), so its number says nothing about order flow.

## Using it

Study any past session. The full tape is free once it's 15 minutes old:

```bash
.venv/bin/python -m paperbot orderflow SPY,QQQ --date 2026-11-13 --days 5 --detail
```

`--feed iex` shows what the free plan would have seen live, and `--all` adds the weak signals.

To run it live, set `orderflow.enabled: true` in the profile's config file:
- The AI gets a summary each cycle: CVD, recent delta, the value area, recent setups.
- The watcher journals every signal and alerts each setup, and starts an AI cycle when `schedule.trigger_cycle_on_signal` is on.
- `paperbot scan` puts the signals on the dashboard charts.

Leave `feed: iex` only for practice. `feed: sip` needs Alpaca's paid market-data plan for live use.

Nothing here places an order. Setups are information for the AI and for you, and the risk gate applies to anything the AI proposes.
