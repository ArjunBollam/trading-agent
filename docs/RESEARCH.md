# Research notes: AI trading agents, intraday and scalping

Collected 2026-10-25 to decide what to change in paperbot. Each finding ends with what it
means for this project. Reddit and Facebook groups couldn't be read (Reddit blocks the search
tool; Facebook groups need a login), so community input comes from Quora, practitioner blogs
and replications instead.

## What the evidence says

1. **Most day traders lose money, and options scalpers are no exception.** Among Brazilian
   day traders who kept going past 300 days, 97% lost money (Chague, De-Losso, Giovannetti).
   In Taiwan, under 1% earned reliable positive returns after fees (Barber, Lee, Liu, Odean).
   Retail 0DTE option traders lost over $70M in about two years, and the losses came from
   lottery-like directional bets, not spreads (Beckmeyer, Branger, Gayda 2023).
   *For paperbot: no setting makes profit likely. What helps is cost control, small risk per
   trade, hard stops on bad days, and honest measurement.*

2. **Published LLM trading results are weak evidence.** TradingAgents' backtest covers three
   months, with no ablations and no live trading. Other papers concede possible look-ahead
   leakage. Models that did well on static benchmarks did worse live (LiveTradeBench).
   *For paperbot: judge it by its own paper-trading record over many trades, not by any
   backtest. It needs a performance report.*

3. **Where LLMs do help is reading news.** GPT-4 headline scores capture the initial reaction
   and predict some follow-through drift, mostly for small stocks and negative news, and the
   edge shrinks as more people use LLMs (Lopez-Lira & Tang, v6 2025).
   *For paperbot: let code compute price features, and let the AI weigh them with the news.
   Don't ask the AI to predict prices from raw numbers.*

4. **Intraday patterns exist, but they're small and cost-sensitive.**
   - *Intraday momentum:* SPY's return from the prior close to 10:00 predicts the last
     half-hour, more strongly on volatile, high-volume and news days. The effect is small
     (R² about 2%) (Gao, Han, Li, Zhou, JFE 2018).
   - *Opening-range breakout:* the gross edge on QQQ replicates, but one replication found it
     indistinguishable from zero after costs (Zarattini & Aziz 2023, and a 2026 replication).
   - *VWAP trend:* strong reported results, lower in community replications, and very high
     turnover (Zarattini & Aziz).
   *For paperbot: compute VWAP, opening range and the first half-hour return in code, show
   them to the AI, and alert on them. Treat them as context, not as a money machine.*

5. **Quote quality matters more than speed for a bot like this.** Alpaca's free "indicative"
   options feed sends at most about one quote per second, and has had stretches of stale
   quotes after the open (Alpaca docs and forum).
   *For paperbot: refuse to open a trade on a stale quote.*

6. **Stops control risk; they don't create profit.** Stop-loss rules lower expected returns
   when prices are random and add value when momentum or regime shifts are present (Kaminski
   & Lo 2014).
   *For paperbot: keep the hard stops. Use breakeven stops and time stops to limit damage,
   not to chase gains.*

7. **The risk rules disciplined traders use:** stop after N losses in a row, wait between
   trades, cap trades per day, risk 0.25–0.5% of the account per trade, and set a personal
   daily stop tighter than the hard one (prop-firm guides, Babypips).
   *For paperbot: add a losing-streak breaker, an entry cooldown, and a per-trade risk cap as
   a percentage of equity.*

8. **Scheduled events and time of day matter.** Volatility is U-shaped through the day, with
   spikes around scheduled releases. FOMC days carry a measurable intraday volatility premium
   (Cboe). The next FOMC decision is **Wed 2026-10-28 at 14:00 ET**, with the press
   conference at 14:30.
   *For paperbot: add entry blackout windows, starting with that FOMC decision.*

9. **News can be weaponized against LLM traders.** Invisible Unicode and hidden-text edits to
   one day's headlines cut an LLM trader's annual return by up to 17.7 points (Rizvani et al.
   2026). The defense that works is isolation: the model that reads text can't place orders.
   *For paperbot: the AI already can't trade. Also strip invisible characters from news, and
   label all tool output as untrusted data in the prompt.*

10. **Backtests overstate.** The best of many variants looks better than it is (Bailey &
    López de Prado's deflated Sharpe). A common rule of thumb is to halve a backtest's Sharpe.
    *For paperbot: the report should warn when there are too few trades to judge.*

## Questions this raises, and the answers

These are the prompts I set myself from the research, with what I found in paperbot:

| # | Question | Answer before this work | Fix |
|---|---|---|---|
| 1 | Can paperbot tell whether it's making money? | No. Nothing computes P&L per trade. | `paperbot report`, plus stats in the daily summary |
| 2 | Can a stale quote cause a bad entry? | Yes. Quotes have no timestamp check. | Reject entries on quotes older than 60 s |
| 3 | What stops a losing streak from compounding? | Only the 2% daily limit. | Losing-streak breaker, entry cooldown, per-trade risk cap |
| 4 | Can a poisoned headline steer the AI? | It can't place orders, but it can skew proposals. | Strip invisible characters, cap length, label news as untrusted |
| 5 | Does the AI see the intraday features with some evidence behind them? | No. It sees price, change and day range. | Code-computed VWAP, opening range, first-half-hour return, RSI, ATR, EMAs |
| 6 | Do I get real-time alerts when setups appear? | No. Only trade alerts. | Signal alerts for opening-range breaks and VWAP crosses |
| 7 | Do winners give back their gains? | Yes. The stop stays at −35% until take-profit. | Breakeven stop after +30%, optional trailing stop |
| 8 | Can it scalp? | Not with a 10-minute AI cycle. | Time stop, end-of-day close, and a scalp profile config |
| 9 | Does it avoid scheduled event volatility? | No. | Entry blackout windows, with this week's FOMC added |

## Not done, because it needs a decision or money from you

- **Streaming quotes over websockets.** Free accounts get sampled indicative quotes anyway,
  and polling every 30 s is enough for 10-minute decisions. It's worth it only with the paid
  OPRA feed and true scalping.
- **Backtesting on historical options data.** That needs a data source and a careful
  leakage-free harness. Running the bot on paper, with the report, is the more honest test.
- **Switching the default strategy to scalping, or loosening any limit.** That's your call.
  The scalp profile is a separate file you can choose to run.

## Sources

- [Day trading studies review (Substack)](https://faisalharoon.substack.com/p/day-trading-failure-rate-analysis), [CNBC on day trader losses](https://www.cnbc.com/2020/11/20/attention-robinhood-power-users-most-day-traders-lose-money.html)
- [0DTE retail losses, CXO summary](https://cxoadvisory.com/individual-investing/retail-0dte-option-trader-performance), [Bloomberg/BNN](https://www.bnnbloomberg.ca/day-traders-lose-358-000-per-day-gambling-on-zero-day-options-1.1910634), [Neudata review](https://neudata.co/literature-reviews/zero-day-to-expiry-options-a-losing-bet-for-retail-traders)
- [TradingAgents paper](https://arxiv.org/pdf/2412.20138), [TradingAgents GitHub](https://github.com/tauricresearch/tradingagents), [QuantAgents (EMNLP 2025)](https://aclanthology.org/2025.findings-emnlp.945.pdf), [When Agents Trade](https://www.alphaxiv.org/abs/2510.11695v1), [Can Blindfolded LLMs Still Trade?](https://arxiv.org/html/2603.17692v1), [Long-run LLM strategies](https://arxiv.org/html/2505.07078v5), [LLM trading survey](https://arxiv.org/html/2408.06361v2), [Rogue Quant on LLM strategies](https://roguequant.substack.com/p/can-llms-really-build-robust-trading)
- [Lopez-Lira & Tang](https://arxiv.org/html/2304.07619v6)
- [Market intraday momentum (WUSTL)](https://profiles.wustl.edu/en/publications/market-intraday-momentum/), [Alpha Architect summary](https://alphaarchitect.com/2014/08/attention-prop-traders-the-first-half-hour-of-trading-predicts-the-last-half-hour/)
- [ORB, CXO summary](https://www.cxoadvisory.com/technical-trading/day-trading-with-an-opening-range-breakout-strategy), [ORB replication net of costs](https://www.mql5.com/en/blogs/post/776235), [VWAP paper (Concretum)](https://concretumgroup.com/volume-weighted-average-price-vwap-the-holy-grail-for-day-trading-systems/), [VWAP QuantConnect reproduction](https://www.quantconnect.com/terminal/cache/embedded_backtest_0cab7e083e99d5a372e02fe882bbf078.html)
- [Alpaca real-time option data](https://docs.alpaca.markets/docs/real-time-option-data), [Alpaca forum: update frequency](https://forum.alpaca.markets/t/options-price-update-frequency/18082), [Alpaca forum: frozen chain](https://forum.alpaca.markets/t/options-chain-frozen/19462)
- [Kaminski & Lo, stop-loss rules](https://dspace.mit.edu/handle/1721.1/114876)
- [LuxAlgo loss-control rules](https://www.luxalgo.com/library/concept/loss-control-rules.md), [Tradezella on prop discipline](https://www.tradezella.com/blog/ai-prop-firm-trading), [Tradovate risk rules](https://prop.tradovate.com/blogs/how-to-manage-risk-in-prop-trading), [Babypips max loss](https://www.babypips.com/trading/why-you-should-set-maximum-trading-loss-limits-2023-07-03)
- [Intraday volatility pattern](https://eranraviv.com/intra-day-volatility-pattern/), [Cboe FOMC volatility premium](https://ww2.cboe.com/insights/posts/s-p-index-volatility-surface-embeds-point-fomc-intraday-volatility-premium), [FOMC October 2026](https://fedratecalc.com/fomc-meeting-schedule/october-2026/)
- [Adversarial headlines vs LLM traders](https://arxiv.org/html/2601.13082v1), [CLEANROOM isolation design](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon/cleanroom/cleanroom-a-trading-agent-that-cant-be-hijacked)
- [Deflated Sharpe ratio](https://papers.ssrn.com/abstract=2460551)
- [Quora: are trading bots profitable long-term?](https://www.quora.com/Are-automated-trading-bots-profitable-in-the-long-run)

---

# Round 2 (2026-10-25, evening): more sources, more checks

Each finding below is followed by what changed in paperbot. "Sim" means paperbot's own exit
simulator on real Alpaca option bars (`paperbot simulate-exits`).

## Findings

11. **Earnings crush short-dated option buyers.** IV rises into earnings and collapses after.
    The shorter the expiry, the bigger the event's share of remaining variance, so weeklies get
    crushed the hardest (FlashAlpha). MSFT reports 2026-10-28 after the close (Microsoft IR).
    AAPL reports 2026-11-02 after the close (per StockTitan, citing Apple IR). NVDA is projected
    for 2026-11-18 but unconfirmed.
    *Changed: no new entries 2 trading days before a report, and positions in that stock are
    closed by 15:30 on the last session before the gap.*

12. **Paper fills aren't live fills.** Alpaca's paper engine fills buys at the ask and sells at
    the bid, and randomly partial-fills 10% of eligible orders. Staff say paper slippage may
    not match live.
    *Changed: every fill is logged against the mid at the time the order went out. The report
    shows fill rate and slippage, and you get a phone alert when an order actually fills.*

13. **Liquidity rules of thumb:** daily volume of 500–1,000+ contracts, a spread under 10%, and
    avoiding zero-activity contracts. Liquidity concentrates near the money.
    *Changed: open interest of at least 100, a |delta| band of 0.30–0.70, and the spread cap
    tightened from 12% to 10%.*

14. **Time decay in the last week is steep.** Measured from Alpaca's own greeks on 2026-10-23,
    near-the-money SPY options lose about 25% of their price per day at 2 DTE and about 13% at
    4 DTE.
    *Changed: entries are refused above 10% decay per day in the default profile (35% in the
    scalp profile). Daily decay is shown on every candidate.*

15. **Stops fill worse than their price.** In the sim (116 SPY and 116 QQQ trades), the worst
    stop fills reached about 2× the stop distance (−62% and −75% on a 35% stop), because option
    trades are sparse and prices gap between bars.
    *Changed: sizing now assumes a stop-out loses 1.5× the stop distance
    (`stop_slippage_factor`). The largest entry falls from about $1,430 to about $950 of
    premium.*

16. **Breakeven stops help, consistently.** In the sim, moving the stop to breakeven at +30%
    improved the average in 8 of 9 stop/target pairs on SPY and 9 of 9 on QQQ. The
    current settings (35% stop, 60% target, breakeven) ranked in the top group on both.
    The finer ranking held up between halves of the period on SPY (rank correlation +0.75) but
    not on QQQ (+0.13), so only the breakeven effect is a robust finding. A +90% target was
    consistently the worst. Scalp settings came out flat (−2.5% to +1.2% for every variant,
    stability +0.34): no basis for retuning.
    A larger rerun (232 SPY trades, entries at 10:30 and 13:00) agreed: breakeven helped in 8
    of 9 pairs (the ninth was a tie), +90% targets were worst, and the current settings sat
    within about a point of the top. That's a plateau, not a peak.
    *Changed: nothing beyond keeping breakeven on. The robustness research says to prefer
    plateaus over the best single cell, and to tune few parameters.*

17. **Runaway algorithms.** Knight Capital sent over 4 million erroneous orders in 45 minutes,
    and its warning emails weren't recognized as alerts. The SEC's lesson: assume every
    component can fail, and have safety nets.
    *Changed: more than 30 order actions, or 5 broker rejections, in 10 minutes trips HALT_ALL,
    with an urgent alert. Separately, if Alpaca keeps rejecting stop orders, paperbot falls back
    to code-only stops and tells you once.*

18. **Early closes.** NYSE closes at 13:00 ET on 2026-11-27 and 2026-12-24 (NYSE 2026
    calendar).
    *Changed: every cutoff shifts earlier by the same amount, using Alpaca's exchange
    calendar.*

19. **A bot can't report that it's dead.** A dead man's switch (for example healthchecks.io)
    alerts you when check-ins stop. Pinging after a successful tick is better than pinging
    from a timer.
    *Changed: an optional `HEALTHCHECK_URL` is pinged after each healthy watch tick, and its
    `/fail` endpoint on errors.*

20. **The pattern-day-trader rule is gone.** FINRA eliminated it effective 2026-06-04, and
    brokers may keep the old rules until 2027-10-20.
    *Changed: documented only. It doesn't apply to the $100K paper account.*

21. **Token-efficient prompts.** CSV and markdown tables use far fewer tokens than JSON for
    tables (TQA-Bench and others).
    *Changed: candidates are a compact table. A real full-size prompt fell from 4,391 to 3,485
    tokens while gaining decay, sizing and gap context.*

22. **Gaps:** small SPY gaps (0.1–0.25%) filled the same day 78% of the time, versus 26% for
    1–2% gaps (thetrading.tools, since 1993). **Implied move** = IV / √252; options
    usually price more movement than actually happens.
    *Changed: gap size, whether it has filled, distance from the prior day's high and low, and
    each underlying's implied daily move (with how much of it today's range has used) are in
    the prompt.*

23. **Correlation.** S&P 500 vs Nasdaq-100 rolling correlation hit 0.98 in 2026 (CME).
    NVDA+AAPL+MSFT are about 21.6% of SPY. Correlations rise in sell-offs (IMF).
    *Changed: at most 2 positions in the same direction (1 in the scalp profile).*

24. **Where AI judgment is weak:** verbalized confidence is overconfident (several 2023–2026
    papers). Self-consistency voting has no trading evidence and multiplies cost. VIX regime
    filters are anecdotal and contradictory. LLMs hallucinate financial numbers unless they
    use tools or retrieval (Kang & Liu 2023).
    *Changed: no confidence gating, no voting, no VIX filter. Numbers keep coming from code and
    tools, not from the model.*

25. **MAE/MFE** (Sweeney): plot how far winners dipped and losers rallied to place stops.
    *Changed: each position's worst and best mark is tracked, and the report summarizes them
    once there are enough trades.*

26. **Overtrading costs returns.** Barber & Odean: the highest-turnover households lagged the
    market by 5.5% a year after costs.
    *Changed: supports the existing caps, cooldowns and streak breaker. Nothing new.*

27. **Stale data.** Alpaca's indicative options feed has had minutes-long stale stretches.
    *Changed: underlyings whose last trade is over 5 minutes old are dropped from the prompt;
    if all are stale, the cycle is skipped with an alert.*

28. **More simulator checks** (SPY, 60 days, current exit settings):
    - *Short expiries:* 2–4 DTE entries averaged −5.5% vs −1.1% for 7–14 DTE. Consistent with
      the decay data, and with the 10%/day decay filter.
    - *Entry time:* 14:00 entries averaged −3.8% vs −1.1% at 10:30, within noise at this sample
      size. No change.
    - *Weekends:* trades actually held over a weekend (only 11 on SPY, 15 on QQQ) showed no
      difference from the rest (+1.4 and +0.7 points, standard error ~14). An earlier,
      wrongly grouped cut suggested otherwise; it was an artifact. No close-before-weekend
      rule.

29. **Order effects.** LLMs' choices shift with the order options are listed in. Reordering
    moved benchmark accuracy by 13–75%, more so when the model is unsure. The direction depends
    on option quality, and rotating the order is the standard mitigation.
    *Changed: the candidate table rotates which underlying, and whether calls or puts, comes
    first, every cycle. The prompt says the order means nothing.*

30. **Anchoring.** Numbers placed in a prompt bias LLM forecasts, and "ignore that" instructions
    barely help (Nguyen 2024; several 2024–2025 papers).
    *Changed: `max_qty` is labeled a ceiling, not a target. Since a size suggestion can't be
    fully neutral, the hard limit is enforced in code anyway.*

31. **Daylight saving time.** A trading platform's time windows shifted an hour after a DST
    change in 2024 (TradersPost). Scheduling in local server time is the usual cause. US clocks
    fall back on Sunday 2026-11-01.
    *Changed: paperbot already uses America/New_York for every time of day and UTC broker
    timestamps. A test now checks entries at 09:50 ET on both sides of the change.*

32. **Open interest is a poor liquidity filter for SPY and QQQ.** I sampled 8 real
    near-the-money 7–14 DTE contracts per underlying (Alpaca, 2026-10-23 close). In SPY and in
    QQQ, 3 contracts with spreads of 3% or less had open interest under 100: those chains spread
    open interest across $1 strikes and daily expiries. In the single stocks, low open interest
    came with wide spreads, which the spread cap already catches. This matches the research
    warning that open interest alone misleads without volume.
    *Changed: the open-interest floor is waived when the live spread is 3% or less.*

33. **End-to-end rehearsal.** `paperbot rehearse` ran a full cycle on real Alpaca data with
    Groq. The 2,825-token prompt came back as a valid hold, in a dry run that sends nothing. A
    separate live test confirmed Groq's tool-call round trip works.

34. **The 250-day simulation weakens finding 16.** With about 1,000 trades each (SPY: 990 over
    248 days; QQQ: 970 over 245 days; entries at 10:30 and 13:00), the current settings averaged
    −3.3% (SPY) and −3.4% (QQQ). The best rows (40% targets) were −2.4% and −1.4%. The ranking
    did not hold up between halves (SPY −0.31, QQQ +0.51), and the worst fills reached −91% and
    −94%. So no exit setting is robustly better, and the 60-day breakeven result looks
    period-specific. Nothing was changed. Revisit with live MAE/MFE from `paperbot report`.

35. **Aggressive 0–1 DTE scalping, simulated.** 240 trades on real same-day and next-day SPY
    options (40 days, entries at 10:00, 11:30 and 13:30, 1.5% round-trip cost, no-edge
    entries). The aggressive profile's exits averaged −2.1% per trade (41% wins, worst −17%,
    best +19%). Every variant lost money (−0.6% to −3.8%), and the ranking only partly held up
    (+0.36). The exits contain losses; any profit has to come from the AI's entries beating a
    roughly 43–52% win rate after costs. Wider targets (30%) looked slightly better, but within
    noise, so nothing was retuned.

## Still open (needs data or a decision)

- **Exit tuning from live results.** The sim has no entry edge by design. Once paperbot has
  30+ real trades, compare `paperbot report`'s MAE/MFE with the sim before changing stops.
- **CPI** for October is Tue 2026-11-10 at 08:30 ([BLS CPI page](https://www.bls.gov/cpi/)),
  and the jobs report is Fri 2026-11-06 at 08:30. Both land before trading starts. paperbot
  skips 09:45–10:15 on those days, which is a judgment call rather than a research-backed
  window.
- **Debit spreads** (Alpaca `mleg`, level 3) would cap risk and cut decay exposure, but they
  change the strategy.

## Round 2 sources

- Earnings and IV: [FlashAlpha IV crush numbers](https://flashalpha.com/articles/how-much-does-iv-drop-after-earnings-real-crush-numbers), [Option Alpha on IV crush](https://optionalpha.com/learn/iv-crush), [Quantcha earnings research](https://quantcha.com/news/quantchas-latest-research-report-on-trading-earnings-with-options-now-available/), [moomoo on post-earnings IV](https://www.moomoo.com/us/learn/approaching-post-earnings-iv-crush-with-options), [Microsoft IR](https://www.microsoft.com/en-us/investor/default), [StockTitan earnings week](https://www.stocktitan.net/articles/earnings-calendar-week-of-october-26-2026), [MarketBeat MSFT](https://www.marketbeat.com/stocks/NASDAQ/MSFT/earnings/), [Wall Street Horizon NVDA](https://www.wallstreethorizon.com/nvidia-earnings-calendar)
- Paper fills and execution: [Alpaca forum: paper vs real slippage](https://forum.alpaca.markets/t/slippage-paper-trading-vs-real-trading/2801), [TradersPost on Alpaca paper](https://blog.traderspost.io/article/alpaca-paper-trading), [TradersPost paper vs live](https://blog.traderspost.io/article/paper-trading-vs-live-results), [QuantConnect Alpaca docs](https://www.quantconnect.com/docs/v2/cloud-platform/live-trading/brokerages/alpaca), [Implementation shortfall (QuestDB)](https://questdb.com/docs/cookbook/sql/finance/implementation-shortfall-order/), [Slippage and execution quality](https://www.elliptic.co/corpus/gen-2539/financial-market-efficiency/slippage-and-execution-quality.html)
- Liquidity and delta: [Option Alpha volume vs OI](https://optionalpha.com/learn/options-volume-vs-open-interest), [Robot Wealth rules of thumb](https://robotwealth.com/rules-of-thumb-for-trading-equity-options/), [TopDog on open interest](https://www.topdogtrading.com/options-open-interest-explained/), [InCred on option volume](https://www.incredmoney.com/knowledge-center/futures-and-options/option-volume-meaning-importance-calculation-how-to-analyze-it/), [Barchart NVDA delta comparison](https://www.barchart.com/story/news/33121643/nvidia-3-long-call-plays-one-was-clearly-built-for-profit), [Amberdata delta surfaces](https://blog.amberdata.io/fine-tuning-option-strategies-via-delta-surfaces)
- Alpaca API: [Create an order (reference)](https://docs.alpaca.markets/reference/postorder), [Options trading overview](https://docs.alpaca.markets/docs/options-trading-overview), [Options trading](https://docs.alpaca.markets/docs/options-trading), [Historical option data](https://docs.alpaca.markets/docs/historical-option-data), [Option bars endpoint](https://docs.alpaca.markets/reference/optionbars), [Forum: OPRA agreement error](https://forum.alpaca.markets/t/error-opra-agreement-is-not-signed/18445), [Rate limits (apis.io)](https://apis.io/rate-limits/alpaca-markets/alpaca-markets-rate-limits/), [Alpaca News API](https://alpaca.markets/docs/news-api), [Alpaca + Benzinga](https://alpaca.markets/blog/alpaca-partners-with-benzinga-to-deliver-real-time-embedded-financial-news/)
- AI decisions: [Confidence elicitation in LLMs](https://arxiv.org/pdf/2306.13063), [Wired for overconfidence](https://arxiv.org/pdf/2604.01457), [Taming overconfidence (RLHF)](https://arxiv.org/html/2410.09724v1), [DINCO calibration](https://arxiv.org/html/2509.25532v2), [Confidence improves self-consistency](https://arxiv.org/pdf/2502.06233), [Soft self-consistency](https://preview.aclanthology.org/dois-2013-emnlp/2024.acl-short.28), [LLM hallucination in finance (Kang & Liu)](https://ideas.repec.org/p/arx/papers/2311.15548.html), [FinMem](https://arxiv.org/pdf/2311.13743)
- Regimes and sizing: [QuantRocket intraday strategy](https://www.quantrocket.com/blog/hedging-long-term-risk-intraday-strategy), [Concretum intraday shorts](https://concretumgroup.com/conditional-profitability-of-intraday-shorts/), [VIX regime filter post](https://algotr.substack.com/p/stop-leaving-money-on-the-table-a), [LuxAlgo Kelly criterion](https://www.luxalgo.com/library/concept/kelly-criterion/), [NexusFi Kelly](https://nexusfi.com/a/risk-management/kelly-criterion), [Position sizing guide](https://orthogonal.info/risk-management-position-sizing-an-engineers-guide-to-trading/)
- Stops: [Questrade stop-limit](https://www.questrade.com/learning/investing-basics/lc-stop-limit-order-what-it-is-how-it-works-and-when-to-use-one), [LuxAlgo order types](https://www.luxalgo.com/library/concept/order-types/), [Options order types](https://legalclarity.org/options-order-types-market-limit-stop-and-more/), [Optimus Futures stop orders](https://learn.optimusfutures.com/stop-orders-explained), [Chandelier exit](https://www.mql5.com/en/market/product/185703), [Adaptive chandelier](https://id.tradingview.com/script/S4WGeup3-Adaptive-Chandelier-Exit-MAB/), [LuxAlgo MAE/MFE](https://www.luxalgo.com/library/concept/mae-mfe-distributions.md), [NexusFi MAE](https://nexusfi.com/a/risk-management/maximum-adverse-excursion-mae)
- Reliability: [SEC: Knight Capital](https://www.sec.gov/news/digest/2013/dig101613.htm), [Taming runaway algos](https://www.marketsmedia.com/taming-runaway-algos-in-the-wake-of-knight/), [Knight 10-K](https://www.sec.gov/Archives/edgar/data/0001060749/000119312513087636/d449921d10k.htm), [FCA Citigroup notice](https://fca.org.uk/publication/final-notices/citigroup-global-markets-limited-2024.pdf), [HKEX incident report](https://www.hkex.com.hk/-/media/HKEX-Market/News/News-Release/2020/2012282news/HKATS-Incident-Public-Report_e.pdf?la=en), [JPX investigation report](https://www.jpx.co.jp/english/corporate/news/news-releases/0020/b5b4pj000003xrsa-att/InvestigationReport.pdf), [Healthchecks.io FAQ](https://healthchecks.io/faq), [Cron monitoring docs](https://healthchecks.sre.cfainstitute.org/docs/monitoring_cron_jobs/), [Crontap dead man's switch](https://crontap.com/blog/healthchecks-dead-man-pair-with-scheduler), [Automated trading failures](https://digiqt.com/blog/automated-trading-system-failures/)
- Testing and evaluation: [NautilusTrader testing](https://nautilustrader.io/docs/nightly/developer_guide/testing), [RustyBT testing](https://rustybt.readthedocs.io/en/stable/api/testing/), [Property-based testing](https://www.zealynx.io/glossary/property-based-testing), [Monte Carlo for trading (MQL5)](https://www.mql5.com/en/blogs/post/774072), [Tradezella Monte Carlo](https://www.tradezella.com/blog/monte-carlo-simulation-for-trading-stress-test-any-strategy-before-you-risk-real-capital), [NexusFi Monte Carlo](https://nexusfi.com/a/automation/monte-carlo-simulation-futures-trading), [Walk-forward analysis](https://nexusfi.com/a/automation/walk-forward-analysis), [Notis: are your profits robust?](https://www.traders.com/Documentation/FEEDbk_docs/2000/04/Abstracts_new/Notis/Notis.html), [Backtrex overfitting red flags](https://backtrex.com/en/blog/backtest-strategy-avoid-overfitting-red-flags), [Walk-forward validation](https://whatworksintrading.substack.com/p/understanding-walk-forward-validation)
- Calendar and rules: [NYSE 2026–2028 holidays](https://s2.q4cdn.com/154085107/files/doc_news/NYSE-Group-Announces-2026-2027-and-2028-Holiday-and-Early-Closings-Calendar-2025.pdf), [Fidelity market holidays](https://www.fidelity.com/learning-center/smart-money/stock-market-holidays), [Alpaca on PDT retirement](https://alpaca.markets/blog/finra-retires-the-pdt-rule-introducing-alpacas-new-intraday-margin-framework/), [King & Spalding on FINRA 4210](https://www.kslaw.com/insights/articles/finra-adopts-sweeping-changes-to-margin-requirements-for-day-trading), [Investor.gov PDT](https://www.investor.gov/introduction-investing/investing-basics/glossary/pattern-day-trader), [Jobs report Nov 2026](https://www.simianx.ai/stories/us-jobs-report-nonfarm-payrolls-latest-data-and-release-schedule), [CPI calendar](https://www.calendarx.com/events/cpi-november-2026)
- Prompts and data: [TQA-Bench](https://arxiv.org/pdf/2411.19504), [TOON benchmarks critique](https://dev.to/ikaganacar/toon-benchmarks-a-critical-analysis-of-different-results-5h66), [JSON vs TOON vs Markdown-KV](https://medium.com/softserve-technical-communication/rethinking-llm-inputs-json-against-toon-and-markdown-kv-b713bcbe7eb5), [Groq rate limits](https://console.groq.com/docs/rate-limits), [Groq TPM forum example](https://community.crewai.com/t/tpm-tokens-per-minute-limiting/1941)
- Market structure: [SPY gap statistics](https://www.thetrading.tools/gap-analysis), [Trade That Swing gap fills](https://tradethatswing.com/sp-500-spy-es-gap-fill-strategy-and-statistics/), [Fading the open](https://global-macro-monitor.com/2011/05/30/fading-the-open/), [Expected move (Macroption)](https://www.macroption.com/converting-implied-volatility-to-daily-move/), [tastylive expected move](https://www.tastylive.com/definitions/expected-move), [Intraday stock market behavior (CXO)](https://www.cxoadvisory.com/calendar-effects/intraday-u-s-stock-market-behavior), [Intraday SPX/SPY spreads (Mishra & Daigler)](https://discovery.fiu.edu/display/pub109110), [tastylive spreads and volatility](https://www.tastylive.com/shows/options-jive/episodes/option-bid-ask-spreads-impact-of-volatility-06-10-2016), [Weekend theta (SpotGamma)](https://support.spotgamma.com/hc/en-us/articles/15249846531219-Weekend-Theta), [Weekly vs monthly options](https://apexvol.com/compare/weekly-vs-monthly-options), [Schwab weekly options](https://international.schwab.com/story/how-to-use-weekly-stock-options), [Opening range and initial balance](https://www.luxalgo.com/library/concept/opening-range-and-orb.md), [NexusFi ORB](https://nexusfi.com/a/strategies/opening-range-breakout)
- Concentration and correlation: [CME on tech-heavy S&P 500](https://www.cmegroup.com/openmarkets/economics/2026/What-Past-Sector-Concentrations-Tell-Us-About-Todays-Tech-Heavy-SP-500.html), [S&P DJI: in the shadows of giants](https://www.spglobal.com/spdji/en/documents/research/research-in-the-shadows-of-giants.pdf), [SSGA mega caps](https://www.ssga.com/au/en_gb/intermediary/insights/opportunities-outside-us-mega-caps), [IMF on diversification in sell-offs](https://www.imf.org/en/blogs/articles/2026/02/18/stock-bond-diversification-offers-less-protection-from-market-selloffs), [Beta weighting (Option Alpha)](https://optionalpha.com/learn/beta-weighting), [Beta-weighted delta](https://optionstradingiq.com/beta-weighted-delta/)
- Order effects, anchoring, DST: [Fragile preferences: order effects in LLMs](https://arxiv.org/html/2506.14092v2), [Order matters (Megagon, NAACL 2024)](https://megagon.ai/order-matters-llm-sensitiv-mult-choice-tsks/), [Primacy bias in fine-tuned LLMs](https://arxiv.org/pdf/2507.13949v1), [Position bias in audio LMs](https://arxiv.org/html/2510.00628v2), [Anchoring in LLM forecasts (Nguyen 2024)](https://ideas.repec.org/a/eee/beexfi/v43y2024ics2214635024000868.html), [Anchoring bias in LLMs: an experimental study](https://arxiv.org/pdf/2412.06593), [Anchoring mechanisms](https://arxiv.org/pdf/2505.15392), [LLM stock return forecasts](https://arxiv.org/pdf/2409.11540), [Financial bias in LLMs](https://arxiv.org/pdf/2402.12713), [TradersPost DST fix](https://docs.traderspost.io/releases/march-11th-2024), [InformationWeek on 2007 DST](https://www.informationweek.com/software-services/mini-y2k-major-hassle), [DST software bugs](https://converttime.com/en/blog/dst-software-bugs), [Time as a feature](https://dataconomy.com/2026/05/18/time-is-a-feature-one-decision-that-removes-a-whole-class-of-bugs/)
- Behavior: [Barber & Odean individual investor performance](https://faculty.haas.berkeley.edu/odean/Papers%20current%20versions/Individual_Investor_Performance_Final.pdf), [Odean returns paper](https://faculty.haas.berkeley.edu/odean/papers/returns/Returns.html), [NBER w16022](https://www.nber.org/papers/w16022.pdf)
