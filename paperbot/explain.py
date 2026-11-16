"""What each chart signal means, with a worked example, for the web page's hover tooltips.

Every kind the scanner can report (patterns.LABELS, plus the live crosses) has an entry here,
and a test checks that none is missing. The examples use a made-up stock near $100 on
5-minute candles. They describe how traders read a pattern; they are not predictions, and
docs/RESEARCH.md shows how weak most intraday patterns are once costs are counted.
"""

from __future__ import annotations

from .formations import CANDLES
from .patterns import LABELS

LIVE_CROSS = "live_cross"

# kind: (what it means, example)
EXPLAIN: dict[str, tuple[str, str]] = {
    # --- VWAP -------------------------------------------------------------------------------
    "vwap_reclaim": (
        "Price closed back above VWAP, the average price every share traded at today, weighted by volume. "
        "Buyers took back the day's fair price; intraday traders read it as control shifting to buyers.",
        "Below its VWAP of 100.00 all morning, the stock closes a candle at 100.30."),
    "vwap_lost": (
        "Price closed back below VWAP. Sellers pushed it under the day's average traded price, "
        "and VWAP often turns into resistance after that.",
        "Above VWAP 100.00 since the open, the stock closes a candle at 99.70."),
    "vwap_bounce": (
        "Price dipped to VWAP and closed back above it: buyers defended the day's average price. "
        "A pullback in an uptrend that held.",
        "Trending above VWAP 100.00, a candle dips to 100.02 and closes at 100.40."),
    "vwap_reject": (
        "Price rallied up to VWAP and closed back below it: sellers defended the day's average price. "
        "A bounce in a downtrend that failed.",
        "Below VWAP 100.00 all morning, a candle reaches 99.98 and closes at 99.60."),
    # --- RSI ---------------------------------------------------------------------------------
    "rsi_oversold": (
        "RSI(14) fell below 30: the recent drop has been fast and one-sided. It flags a stretched move "
        "that may bounce, but in a strong downtrend RSI can stay oversold for a long time.",
        "After six red candles from 102.00 to 98.50, RSI drops from 34 to 27."),
    "rsi_oversold_exit": (
        "RSI climbed back above 30 after being oversold: the selling pressure eased. Traders treat this "
        "as a better-timed buy hint than the oversold reading itself.",
        "RSI bottoms at 24, then a green candle to 99.10 lifts it to 31."),
    "rsi_overbought": (
        "RSI(14) rose above 70: the recent rise has been fast and one-sided. It flags a stretched move "
        "that may pause or pull back; in a strong uptrend RSI can stay overbought.",
        "After six green candles from 98.00 to 101.50, RSI rises from 66 to 73."),
    "rsi_overbought_exit": (
        "RSI dropped back below 70 after being overbought: the buying pressure eased. "
        "A sell hint, better timed than the overbought reading itself.",
        "RSI peaks at 78, then a red candle to 100.90 drops it to 68."),
    # --- moving averages and MACD -------------------------------------------------------------
    "ema_cross_up": (
        "The fast 9-candle EMA crossed above the slow 21-candle EMA: short-term momentum turned up.",
        "The 9 EMA (99.80) was under the 21 EMA (99.90); two green candles lift it to 100.05, above the 21 EMA at 99.97."),
    "ema_cross_down": (
        "The 9-candle EMA crossed below the 21-candle EMA: short-term momentum turned down.",
        "The 9 EMA falls from 100.10 to 99.85 while the 21 EMA sits at 99.95."),
    "macd_cross_up": (
        "The MACD line (the gap between the 12- and 26-candle EMAs) crossed above its 9-candle signal line: "
        "momentum is turning up. More meaningful below zero, after a decline.",
        "MACD rises from -0.12 to -0.03 and crosses its signal line at -0.05."),
    "macd_cross_down": (
        "The MACD line crossed below its signal line: momentum is turning down. "
        "More meaningful above zero, after a rise.",
        "MACD falls from 0.15 to 0.06 and crosses below its signal line at 0.08."),
    # --- Bollinger bands ----------------------------------------------------------------------
    "bb_reentry_up": (
        "Price closed below the lower Bollinger band (the 20-candle average minus 2 standard deviations), "
        "then closed back inside it: a stretched drop snapping back toward the average.",
        "The lower band is at 98.80; one candle closes at 98.60, outside it, and the next at 99.10, back inside."),
    "bb_reentry_down": (
        "Price closed above the upper Bollinger band, then back inside it: a stretched rise fading back "
        "toward the average.",
        "The upper band is at 101.20; one candle closes at 101.40 and the next at 100.90."),
    "squeeze_breakout_up": (
        "The Bollinger bands had narrowed to their tightest in a while (a squeeze: a quiet, low-volatility "
        "stretch), then price closed above the upper band. Quiet stretches often end in a bigger move; this one went up.",
        "The bands pinch to 99.80–100.20 for an hour, then a candle closes at 100.45."),
    "squeeze_breakout_down": (
        "After a Bollinger squeeze, price closed below the lower band: the quiet stretch ended with a move down.",
        "The bands pinch to 99.80–100.20 for an hour, then a candle closes at 99.55."),
    # --- support and resistance ---------------------------------------------------------------
    "support_test": (
        "Price dipped to a support level (prior-day low or close, a pivot, the opening-range low, the low "
        "of day, or VWAP) and closed back above it: buyers defended the level.",
        "The prior day's low is 99.00; a candle dips to 99.03 and closes at 99.40."),
    "resistance_test": (
        "Price rose to a resistance level and closed back below it: sellers defended the level.",
        "The opening-range high is 101.00; a candle reaches 100.98 and closes at 100.60."),
    "breakout": (
        "Price closed clearly above a resistance level. Breakouts on light volume fail more often, "
        "so those are marked weak.",
        "The prior-day high is 101.00; a candle closes at 101.35 on twice the usual volume."),
    "breakdown": (
        "Price closed clearly below a support level. Weak if the volume was light.",
        "The low of day is 99.00; a candle closes at 98.70 on heavy volume."),
    # --- volume -------------------------------------------------------------------------------
    "volume_spike_up": (
        "A green candle traded at least 3 times the average volume of the last 20 candles: heavy buying, "
        "from news, a large buyer, or a breakout drawing attention.",
        "Candles usually trade 50K shares; a green one from 100.00 to 100.80 trades 180K."),
    "volume_spike_down": (
        "A red candle traded at least 3 times the recent average volume: heavy selling.",
        "Candles usually trade 50K shares; a red one from 100.00 to 99.20 trades 200K."),
    # --- daily --------------------------------------------------------------------------------
    "golden_cross": (
        "On daily candles, the 50-day average crossed above the 200-day average: the last few months have "
        "been stronger than the last year. A slow, widely watched long-term trend signal.",
        "The 50-day average climbs from 94.80 to 95.20 and passes the 200-day average at 95.00."),
    "death_cross": (
        "On daily candles, the 50-day average crossed below the 200-day average: a slow, widely watched "
        "long-term downtrend signal.",
        "The 50-day average falls from 95.20 to 94.80, under the 200-day average at 95.00."),
    "high_52w": (
        "Price made a new 52-week high. Nobody who bought in the past year is sitting on a loss above it, "
        "so momentum traders watch for follow-through.",
        "The year's highest price was 100.00; today the stock trades at 100.40."),
    "low_52w": (
        "Price made a new 52-week low: everyone who bought in the past year is under water, and the trend is weak.",
        "The year's lowest price was 80.00; today the stock trades at 79.60."),
    # --- the live scanner ---------------------------------------------------------------------
    LIVE_CROSS: (
        "The live price crossed a level (VWAP, the high or low of day, the opening range, a prior-day level "
        "or a pivot) inside a candle that hasn't closed yet. An early warning only: it isn't confirmed until "
        "the candle closes beyond the level.",
        "At 11:12 the price ticks from 99.98 to 100.05, above VWAP 100.00, while the 11:10 candle is still forming."),

    # --- order flow (orderflow.py) -----------------------------------------------------------
    "of_absorption_bull": (
        "Order flow: sellers hit the bid much harder than usual, most of the candle's volume traded near its low, "
        "yet the candle closed in its upper half. A large passive buyer absorbed the selling (Bookmap: repeated "
        "execution at a level without the price moving). Not an entry by itself: wait for confirmation.",
        "At support 100.00, a candle's delta is -40,000 shares (3x normal), most trades print at 100.02, and it closes at 100.20."),
    "of_absorption_bear": (
        "Order flow: buyers lifted the offer much harder than usual, most of the candle's volume traded near its high, "
        "yet the candle closed in its lower half. A large passive seller absorbed the buying. Not an entry by itself: "
        "wait for confirmation.",
        "At resistance 100.00, a candle's delta is +40,000 shares (3x normal), most trades print at 99.98, and it closes at 99.80."),
    "of_absorption_long": (
        "The confirmation of an absorption at support: within three candles buyers took over (positive delta, CVD rising) "
        "and price closed above the absorption candle's high. Entry at the close, stop just under the absorbed level, "
        "target the next level up.",
        "After absorption at 100.00 (candle high 100.25), the next candle has delta +15,000 and closes at 100.35: entry 100.35, stop 99.95."),
    "of_absorption_short": (
        "The confirmation of an absorption at resistance: within three candles sellers took over (negative delta, CVD "
        "falling) and price closed below the absorption candle's low. Entry at the close, stop just above the absorbed "
        "level, target the next level down.",
        "After absorption at 100.00 (candle low 99.75), the next candle has delta -15,000 and closes at 99.65: entry 99.65, stop 100.05."),
    "of_stacked_buy": (
        "Footprint: on several prices in a row, buyers lifting the offer outnumbered sellers hitting the bid one price "
        "lower by 2.5 to 1 or more. Aggressive, initiative buying; the zone tends to act as support while price stays above it.",
        "In a candle from 100.00 to 100.40, buying beats the selling a row below on 100.15, 100.20 and 100.25."),
    "of_stacked_sell": (
        "Footprint: on several prices in a row, sellers hitting the bid outnumbered buyers lifting the offer one price "
        "higher by 2.5 to 1 or more. Aggressive, initiative selling; the zone tends to act as resistance while price stays below it.",
        "In a candle from 100.40 down to 100.00, selling beats the buying a row above on 100.25, 100.20 and 100.15."),
    "of_divergence_bull": (
        "Price made a lower low than its last swing low, but CVD (cumulative buying minus selling) made a higher low and "
        "the candle's delta was positive. The drop wasn't driven by sellers, so it may not last.",
        "Price falls to 99.40 below the 11:00 low of 99.60, while CVD is -20,000 against -35,000 at 11:00."),
    "of_divergence_bear": (
        "Price made a higher high than its last swing high, but CVD made a lower high and the candle's delta was "
        "negative. The rally wasn't driven by buyers, so it may not last.",
        "Price rises to 100.60 above the 11:00 high of 100.40, while CVD is +20,000 against +35,000 at 11:00."),
    "of_exhaustion_low": (
        "Footprint: a candle made a new low, but almost nothing traded at its lowest price and it closed in its upper "
        "half. Sellers ran out at the bottom (Axia's exhaustion print).",
        "A candle drops to a new low of 99.50 with only 200 shares there, against 5,000 on an average price, and closes at 99.90."),
    "of_exhaustion_high": (
        "Footprint: a candle made a new high, but almost nothing traded at its highest price and it closed in its lower "
        "half. Buyers ran out at the top (Axia's exhaustion print).",
        "A candle rallies to a new high of 100.50 with only 200 shares there, against 5,000 on an average price, and closes at 100.10."),
    "of_trend_long": (
        "Fabio Valentini's trend model: price is out of balance above the day's value area and VWAP, pulls back to VWAP, "
        "the value area high or a low-volume node, and meets aggressive buying there. Entry at the close, stop under that "
        "level, target the day's high or 2x the risk.",
        "Above value (high 100.50) since 11:00, price dips to VWAP 100.60 and a candle with delta +30,000 closes at 100.80."),
    "of_trend_short": (
        "Fabio Valentini's trend model, short: price is out of balance below the value area and VWAP, rallies to VWAP, the "
        "value area low or a low-volume node, and meets aggressive selling. Entry at the close, stop above that level, "
        "target the day's low or 2x the risk.",
        "Below value (low 99.50) since 11:00, price rallies to VWAP 99.40 and a candle with delta -30,000 closes at 99.20."),
    "of_failed_breakdown": (
        "Fabio Valentini's mean-reversion model: price broke below the value area low, the opening range low or the prior "
        "day's low, then came straight back inside with aggressive buying. Trapped sellers; target the point of control.",
        "Price breaks the value area low of 99.50 to 99.30, then a candle with delta +25,000 closes at 99.60. Target: POC 100.00."),
    "of_failed_breakout": (
        "Fabio Valentini's mean-reversion model: price broke above the value area high, the opening range high or the prior "
        "day's high, then came straight back inside with aggressive selling. Trapped buyers; target the point of control.",
        "Price breaks the value area high of 100.50 to 100.70, then a candle with delta -25,000 closes at 100.40. Target: POC 100.00."),

    # --- classic chart patterns ---------------------------------------------------------------
    "double_top": (
        "Two peaks at about the same price, then a close below the low between them (the neckline). "
        "Buyers failed twice at one level and sellers took over. A common target is the pattern's height "
        "measured down from the neckline.",
        "Peaks at 102.00 and 101.95 with a dip to 100.50 between; a candle closes at 100.30."),
    "double_bottom": (
        "Two lows at about the same price, then a close above the high between them (the neckline). "
        "Sellers failed twice at one level and buyers took over.",
        "Lows at 98.00 and 98.05 with a bounce to 99.50 between; a candle closes at 99.70."),
    "triple_top": (
        "Three peaks at about the same price, then a close below the lows between them: "
        "a double top with one more failed attempt.",
        "Peaks at 102.00, 101.90 and 102.05 with dips to 100.80; a candle closes at 100.60."),
    "triple_bottom": (
        "Three lows at about the same price, then a close above the highs between them: "
        "a double bottom with one more failed attempt by sellers.",
        "Lows at 98.00, 98.10 and 97.95 with bounces to 99.20; a candle closes at 99.40."),
    "head_and_shoulders": (
        "A peak (left shoulder), a higher peak (head), a lower peak (right shoulder), then a close below the "
        "neckline joining the lows between them. The uptrend made a lower high and then broke support: "
        "one of the best-known reversal patterns.",
        "Shoulders at 101.00 and 100.90, head at 102.00, neckline at 100.00; a candle closes at 99.80."),
    "inverse_head_and_shoulders": (
        "The upside-down head and shoulders: a low, a lower low (head), a higher low, then a close above "
        "the neckline. The downtrend made a higher low and broke resistance.",
        "Shoulders at 99.00 and 99.10, head at 98.00, neckline at 100.00; a candle closes at 100.20."),
    "ascending_triangle": (
        "A flat ceiling with rising lows under it, then a close through one of its lines. Buyers keep paying "
        "more while sellers hold one price; traders watch for the break up through the ceiling.",
        "Highs stop at 101.00 three times while lows rise 99.00, 99.50, 100.20; a candle closes at 101.30."),
    "descending_triangle": (
        "A flat floor with falling highs above it, then a close through one of its lines. Sellers keep "
        "accepting less while buyers hold one price; traders watch for the break down through the floor.",
        "Lows hold 99.00 three times while highs fall 101.00, 100.50, 99.80; a candle closes at 98.70."),
    "triangle": (
        "Falling highs and rising lows squeezing together, then a close through either line. Neither side "
        "is in control until the break, so the direction of the break is the signal.",
        "Highs 101.50, 101.00, 100.60 and lows 98.50, 99.00, 99.40 converge; a candle closes at 100.90, above the upper line."),
    "rising_wedge": (
        "Price climbs between two rising lines that converge, then closes through one. The rise is losing "
        "steam, so a break of the lower line is the usual, bearish outcome.",
        "Highs 100.50, 101.00, 101.30 and lows 99.00, 100.00, 100.80; a candle closes at 100.50, below the lower line."),
    "falling_wedge": (
        "Price falls between two falling lines that converge, then closes through one. The decline is losing "
        "steam, so a break of the upper line is the usual, bullish outcome.",
        "Lows 99.50, 99.00, 98.70 and highs 101.00, 100.00, 99.30; a candle closes at 99.60, above the upper line."),
    "channel_up": (
        "Price rose between two parallel rising lines and has now closed through one. Through the upper line, "
        "the rise is speeding up; through the lower line, the uptrend broke.",
        "Lows rise 99, 100, 101 and highs 100, 101, 102; a candle closes at 100.40, below the lower line at 101.20."),
    "channel_down": (
        "Price fell between two parallel falling lines and has now closed through one. Through the upper line, "
        "the downtrend broke; through the lower line, it's speeding up.",
        "Highs fall 102, 101, 100 and lows 101, 100, 99; a candle closes at 100.30, above the upper line at 99.80."),
    "rectangle": (
        "Price bounced between a flat ceiling and a flat floor (a trading range), then closed through one of "
        "them. The side it breaks shows which group won.",
        "The stock trades between 99.00 and 101.00 for two hours, then a candle closes at 101.25."),
    "bull_flag": (
        "A sharp rise (the pole), a short, gentle pullback in a small channel (the flag), then a close above "
        "the flag: a pause in an uptrend that resumes. A common target adds the pole's height to the break.",
        "A rally from 98.00 to 101.00 in 20 minutes, a drift down to 100.40 over 30 minutes, then a candle closes at 101.10."),
    "bear_flag": (
        "A sharp drop (the pole), a short, gentle bounce (the flag), then a close below the flag: "
        "a pause in a downtrend that resumes.",
        "A drop from 102.00 to 99.00, a drift up to 99.60, then a candle closes at 98.90."),
    "bull_pennant": (
        "Like a bull flag, but the pause after the sharp rise is a small triangle instead of a channel.",
        "A rally from 98.00 to 101.00, a tightening range between 100.40 and 100.90, then a candle closes at 101.05."),
    "bear_pennant": (
        "Like a bear flag, but the pause after the sharp drop is a small triangle instead of a channel.",
        "A drop from 102.00 to 99.00, a tightening range between 99.10 and 99.60, then a candle closes at 98.95."),
    # --- tops and bottoms still forming -------------------------------------------------------
    "double_top_forming": (
        "A double top is in place but not confirmed: two peaks at the same price, and price is now close to "
        "the neckline. A close below it would confirm the top; a bounce from here cancels it.",
        "Peaks at 102.00 and 101.95, neckline 100.50, and the price is now 100.65."),
    "double_bottom_forming": (
        "A double bottom is in place but not confirmed: two lows at the same price, and price is now close "
        "to the neckline above. A close above it would confirm the bottom.",
        "Lows at 98.00 and 98.05, neckline 99.50, and the price is now 99.35."),
    "triple_top_forming": (
        "Three peaks at the same price, and price is nearing the neckline under them. "
        "A close below it would confirm a triple top.",
        "Peaks at 102.00, 101.90 and 102.05, neckline 100.80, and the price is now 100.95."),
    "triple_bottom_forming": (
        "Three lows at the same price, and price is nearing the neckline above them. "
        "A close above it would confirm a triple bottom.",
        "Lows at 98.00, 98.10 and 97.95, neckline 99.20, and the price is now 99.05."),
    "head_and_shoulders_forming": (
        "The left shoulder, head and right shoulder are in place and price is nearing the neckline. "
        "A close below it would confirm the head and shoulders; holding above keeps the uptrend alive.",
        "Shoulders at 101.00 and 100.90, head at 102.00, neckline 100.00, and the price is now 100.15."),
    "inverse_head_and_shoulders_forming": (
        "An inverse head and shoulders is in place and price is nearing the neckline above. "
        "A close above it would confirm the bottom.",
        "Shoulders at 99.00 and 99.10, head at 98.00, neckline 100.00, and the price is now 99.85."),

    # --- Fibonacci and harmonic patterns ------------------------------------------------------
    "bullish_gartley": (
        "A harmonic pattern: four swings (X, A, B, C, D) sized by Fibonacci ratios, the last drop ending near "
        "78.6% of the first leg. Traders treat D as a zone where the decline may turn up; it fires when price turns there.",
        "X 95 rises to A 105, B pulls back to 98.80 (61.8%), C bounces to 102, D falls to 97.14 (78.6%), and the next candle turns up."),
    "bearish_gartley": (
        "The Gartley upside down: the last rally ends near 78.6% of the first leg, a zone where the rise may turn down.",
        "X 105 falls to A 95, B bounces to 101.20, C dips to 98, D rallies to 102.86 (78.6%), and price turns down."),
    "bullish_bat": (
        "A harmonic pattern like the Gartley, but point D is deeper, near 88.6% of the first leg. "
        "A buy zone if price turns up there.",
        "X 95 rises to A 105, B pulls back to 100, C bounces to 103, D falls to 96.14 (88.6%), then a green candle."),
    "bearish_bat": (
        "The bearish Bat: the last rally reaches about 88.6% of the first leg, a zone where the rise may turn down.",
        "X 105 falls to A 95, B bounces to 100, C dips to 97, D rallies to 103.86 (88.6%), then a red candle."),
    "bullish_butterfly": (
        "A harmonic pattern whose last leg runs past X, to about 127% of the first leg. D is a new low where "
        "traders look for the selling to exhaust and price to turn up.",
        "X 95 rises to A 105, B pulls back to 97.14, C bounces to 102, D falls to 92.30 (127%), then price turns up."),
    "bearish_butterfly": (
        "The bearish Butterfly: the last rally overshoots X to about 127% of the first leg, a stretched high "
        "where the rise may turn down.",
        "X 105 falls to A 95, B bounces to 102.86, C dips to 98, D rallies to 107.70 (127%), then price turns down."),
    "bullish_crab": (
        "The most stretched harmonic: the last drop reaches about 161.8% of the first leg. D is a deep low "
        "where traders look for a sharp turn up.",
        "X 95 rises to A 105, B pulls back to 98.80, C bounces to 103, D falls to 88.82 (161.8%), then a green candle."),
    "bearish_crab": (
        "The bearish Crab: the last rally reaches about 161.8% of the first leg, a stretched high where "
        "traders look for a sharp turn down.",
        "X 105 falls to A 95, B bounces to 101.20, C dips to 97, D rallies to 111.18 (161.8%), then a red candle."),
    "bullish_abcd": (
        "Two equal down legs (A to B, then C to D) with a bounce between them. When the second leg matches "
        "the first, the selling push may be done; a buy hint as price turns up at D.",
        "A 104 falls to B 100, bounces to C 102.50, falls another 4 points to D 98.50, and turns up."),
    "bearish_abcd": (
        "Two equal up legs with a dip between them. When the second leg matches the first, the buying push "
        "may be done; a sell hint as price turns down at D.",
        "A 96 rises to B 100, dips to C 97.50, rises another 4 points to D 101.50, and turns down."),
    "bullish_three_drives": (
        "Three pushes down to lower lows, spaced by Fibonacci ratios. The third drive often marks the "
        "exhaustion of the decline; a buy hint when price turns.",
        "Lows at 99.00, 97.80 and 96.50 with bounces between them, then a green candle off 96.50."),
    "bearish_three_drives": (
        "Three pushes up to higher highs, spaced by Fibonacci ratios. The third drive often marks the "
        "exhaustion of the rise; a sell hint when price turns.",
        "Highs at 101.00, 102.20 and 103.50 with dips between them, then a red candle off 103.50."),
    "fib_support": (
        "After a big swing up, price pulled back to a Fibonacci retracement level (38.2%, 50%, 61.8% or "
        "78.6% of the swing) and held. Traders watch these levels as places where pullbacks often stop.",
        "A swing from 95 to 105; the pullback reaches 98.82 (61.8%) and the candle closes at 99.40."),
    "fib_resistance": (
        "After a big swing down, price bounced to a Fibonacci retracement level and was turned back there.",
        "A swing from 105 to 95; the bounce reaches 101.18 (61.8%) and the candle closes at 100.60."),

    # --- candlestick patterns (what they look like is in formations.CANDLES) -------------------
    "hammer": (
        "Sellers pushed price well down during the candle, but buyers drove it back up by the close. "
        "After a decline it hints the selling is being absorbed; a higher close next confirms it.",
        "After a slide from 102, a candle opens 100.20, dips to 99.10 and closes 100.30."),
    "shooting_star": (
        "Buyers pushed price well up during the candle, but sellers drove it back down by the close. "
        "After a rise it hints the buying is fading.",
        "After a rally to 102, a candle opens 101.80, spikes to 102.90 and closes 101.70."),
    "inverted_hammer": (
        "After a decline, buyers pushed higher during the candle before sellers pulled it back. "
        "A weaker bullish hint than a hammer; it needs a higher close next.",
        "After a slide, a candle opens 99.00, reaches 100.00 and closes 99.10."),
    "hanging_man": (
        "Shaped like a hammer, but after a rise: sellers showed they can push price down hard. "
        "A warning that the rise may be tiring; it needs a lower close next.",
        "After a rally, a candle opens 102.00, dips to 100.90 and closes 102.05."),
    "dragonfly_doji": (
        "Sellers drove price far down and buyers brought it all the way back to the open. "
        "After a decline, a strong rejection of lower prices.",
        "A candle opens 100.00, drops to 98.90 and closes at 100.00."),
    "gravestone_doji": (
        "Buyers drove price far up and sellers brought it all the way back to the open. "
        "After a rise, a strong rejection of higher prices.",
        "A candle opens 100.00, rises to 101.10 and closes at 100.00."),
    "bullish_belt_hold": (
        "It opened at its low and buyers never let it trade lower, closing near the high: "
        "a forceful bullish candle after a decline.",
        "After a slide, a candle opens at its low of 98.00 and closes at 99.40."),
    "bearish_belt_hold": (
        "It opened at its high and sold off the whole candle: a forceful bearish candle after a rise.",
        "After a rally, a candle opens at its high of 102.00 and closes at 100.60."),
    "bullish_marubozu": (
        "Buyers were in control from the first trade to the last: it opened at the low and closed at the high.",
        "A candle opens 100.00, never trades lower, and closes at its high of 101.20."),
    "bearish_marubozu": (
        "Sellers were in control from the first trade to the last: it opened at the high and closed at the low.",
        "A candle opens 100.00, never trades higher, and closes at its low of 98.80."),
    "bullish_engulfing": (
        "A green body that covers the whole prior red body: buyers overpowered the last candle's sellers. "
        "One of the most watched reversal candles, strongest at support.",
        "A red candle from 100.50 to 100.00, then a green one opening 99.90 and closing 100.70."),
    "bearish_engulfing": (
        "A red body that covers the whole prior green body: sellers overpowered the last candle's buyers. "
        "Strongest at resistance.",
        "A green candle from 100.00 to 100.50, then a red one opening 100.60 and closing 99.80."),
    "bullish_harami": (
        "A small green candle inside the prior big red one: the selling momentum stalled. "
        "A mild reversal hint that needs confirmation.",
        "A red candle from 101.00 to 99.50, then a green one from 99.80 to 100.20."),
    "bearish_harami": (
        "A small red candle inside the prior big green one: the buying momentum stalled.",
        "A green candle from 99.00 to 100.50, then a red one from 100.20 to 99.80."),
    "bullish_harami_cross": (
        "A doji inside the prior big red candle: the selling stopped and the market is undecided. "
        "Slightly stronger than a plain harami.",
        "A red candle from 101.00 to 99.50, then a doji opening and closing at 100.00."),
    "bearish_harami_cross": (
        "A doji inside the prior big green candle: the buying stopped and the market is undecided.",
        "A green candle from 99.00 to 100.50, then a doji opening and closing at 100.00."),
    "homing_pigeon": (
        "Two red candles, the second small and inside the first: sellers are still in charge but losing "
        "force. A mild bullish hint after a decline.",
        "A red candle from 101.00 to 99.50, then a red one from 100.20 to 99.90."),
    "piercing_line": (
        "After a red candle, the next opens lower but buyers push it back above the middle of the red body. "
        "Buyers are fighting back: a bullish reversal hint.",
        "A red candle from 101.00 to 99.00, then a green one opening 98.70 and closing 100.30."),
    "dark_cloud_cover": (
        "After a green candle, the next opens higher but sellers push it below the middle of the green body: "
        "a bearish reversal hint.",
        "A green candle from 99.00 to 101.00, then a red one opening 101.30 and closing 99.70."),
    "tweezer_bottom": (
        "Two candles with the same low, the second green: price was turned back twice at one level, "
        "so that level looks like support.",
        "A red candle with a low of 99.00, then a green one with a low of 99.00 that closes at 99.80."),
    "tweezer_top": (
        "Two candles with the same high, the second red: price was turned back twice at one level, "
        "so that level looks like resistance.",
        "A green candle with a high of 101.00, then a red one with a high of 101.00 that closes at 100.20."),
    "matching_low": (
        "Two red candles closing at the same price: sellers couldn't push the close any lower the second time, "
        "so that price may be support.",
        "Two red candles in a row both close at 99.00."),
    "bullish_meeting_lines": (
        "A red candle, then a green one that opened much lower and rallied back to the red candle's close: "
        "buyers erased a big gap down.",
        "A red candle closes at 99.00; the next opens 98.00 and closes at 99.00."),
    "bearish_meeting_lines": (
        "A green candle, then a red one that opened much higher and fell back to the green candle's close: "
        "sellers erased a big gap up.",
        "A green candle closes at 101.00; the next opens 102.00 and closes at 101.00."),
    "bullish_kicker": (
        "A long red candle, then a long green one opening above the red one's open: a sudden, gap-driven "
        "switch to buyers, often on news. One of the strongest candle signals.",
        "A red candle from 101.00 to 99.50; the next opens 101.20 and closes 102.30."),
    "bearish_kicker": (
        "A long green candle, then a long red one opening below the green one's open: a sudden switch to sellers.",
        "A green candle from 99.00 to 100.50; the next opens 98.80 and closes 97.70."),
    "bullish_separating_lines": (
        "In a rise, a red candle is followed by a green one that opens at the same price and closes higher: "
        "the pullback was brushed aside and the rise continues.",
        "A red candle from 101.00 to 100.40; the next opens 101.00 and closes 101.80."),
    "bearish_separating_lines": (
        "In a decline, a green candle is followed by a red one that opens at the same price and closes lower: "
        "the bounce was brushed aside and the decline continues.",
        "A green candle from 99.00 to 99.60; the next opens 99.00 and closes 98.20."),
    "on_neck": (
        "In a decline, a green candle bounced only back to the prior red candle's low: a weak bounce, "
        "usually followed by more selling.",
        "A red candle from 100.00 to 98.50 (low 98.40); the next opens 97.90 and closes at 98.40."),
    "in_neck": (
        "In a decline, a green candle closed just inside the prior red body: still a weak bounce, "
        "usually followed by more selling.",
        "A red candle from 100.00 to 98.50; the next opens 98.00 and closes at 98.60."),
    "thrusting": (
        "In a decline, a green candle pushed into the prior red body but not past its middle: "
        "buyers tried and fell short. Mildly bearish.",
        "A red candle from 100.00 to 98.00; the next opens 97.60 and closes 98.80, under the 99.00 midpoint."),
    "morning_star": (
        "A long red candle, a small candle below it (indecision), then a green candle closing well into the "
        "red one: a classic three-candle bottom.",
        "Red from 101.00 to 99.00, a small candle around 98.60, then green from 98.90 to 100.40."),
    "evening_star": (
        "A long green candle, a small candle above it (indecision), then a red candle closing well into the "
        "green one: a classic three-candle top.",
        "Green from 99.00 to 101.00, a small candle around 101.40, then red from 101.10 to 99.60."),
    "morning_doji_star": (
        "A morning star whose middle candle is a doji: an even clearer pause in the selling before the turn up.",
        "Red from 101.00 to 99.00, a doji at 98.60, then green from 98.90 to 100.40."),
    "evening_doji_star": (
        "An evening star whose middle candle is a doji: an even clearer pause in the buying before the turn down.",
        "Green from 99.00 to 101.00, a doji at 101.40, then red from 101.10 to 99.60."),
    "bullish_abandoned_baby": (
        "A doji that gapped below the candles on both sides of it, left 'abandoned' at the bottom: "
        "a rare and strong reversal up.",
        "A red candle closes 99.00, a doji at 98.40 (high 98.60), then a green candle from 98.90 to 100.00."),
    "bearish_abandoned_baby": (
        "A doji that gapped above the candles on both sides of it, left 'abandoned' at the top: "
        "a rare and strong reversal down.",
        "A green candle closes 101.00, a doji at 101.60 (low 101.40), then a red candle from 101.10 to 100.00."),
    "three_white_soldiers": (
        "Three strong green candles in a row, each closing higher: steady, persistent buying. "
        "Bullish, though a long run can also be stretched.",
        "Green candles 99.00 to 99.80, 99.60 to 100.50, and 100.30 to 101.20."),
    "three_black_crows": (
        "Three strong red candles in a row, each closing lower: steady, persistent selling.",
        "Red candles 101.00 to 100.20, 100.40 to 99.50, and 99.70 to 98.80."),
    "identical_three_crows": (
        "Three strong red candles, each opening exactly at the last close: relentless selling with no bounce "
        "in between. Strongly bearish.",
        "Red candles 101.00 to 100.20, 100.20 to 99.40, and 99.40 to 98.60."),
    "three_inside_up": (
        "A bullish harami confirmed: the third candle closes above the first candle's open, so the turn up "
        "has follow-through.",
        "Red from 101.00 to 99.50, a small green from 99.80 to 100.20, then a green close at 101.20."),
    "three_inside_down": (
        "A bearish harami confirmed: the third candle closes below the first candle's open.",
        "Green from 99.00 to 100.50, a small red from 100.20 to 99.80, then a red close at 98.80."),
    "three_outside_up": (
        "A bullish engulfing confirmed by a higher green close the next candle.",
        "Red from 100.50 to 100.00, green from 99.90 to 100.70, then a green close at 101.10."),
    "three_outside_down": (
        "A bearish engulfing confirmed by a lower red close the next candle.",
        "Green from 100.00 to 100.50, red from 100.60 to 99.80, then a red close at 99.40."),
    "upside_tasuki_gap": (
        "In a rise, price gapped up and a red candle pulled back without closing the gap: buyers defended it, "
        "and the rise usually continues.",
        "A green candle to 100.50, a gap, a green one from 100.80 to 101.50, then a red one to 100.70 that stays above 100.50."),
    "downside_tasuki_gap": (
        "In a decline, price gapped down and a green candle bounced without closing the gap: sellers defended "
        "it, and the decline usually continues.",
        "A red candle to 99.50, a gap, a red one from 99.20 to 98.50, then a green one to 99.30 that stays below 99.50."),
    "bullish_tri_star": (
        "Three doji in a row at a low, the middle one lower: deep indecision after a decline, "
        "often just before a turn up. Rare.",
        "Doji at 99.00, 98.60 and 99.00."),
    "bearish_tri_star": (
        "Three doji in a row at a high, the middle one higher: deep indecision after a rise, "
        "often just before a turn down. Rare.",
        "Doji at 101.00, 101.40 and 101.00."),
    "stick_sandwich": (
        "Red, green, red, with both red candles closing at the same price: that price is acting as support. "
        "Mildly bullish.",
        "A red candle closes 99.00, a green one from 99.10 to 99.80, then a red one closes at 99.00 again."),
    "unique_three_river": (
        "A long red candle, a red candle that makes a new low but recovers, then a small green candle: "
        "the sellers are running out. A rare bottom pattern.",
        "Red from 101.00 to 99.50, red from 99.80 to 99.40 with a low of 98.70, then green from 99.20 to 99.40."),
    "concealing_baby_swallow": (
        "Four red candles in a decline where the last one swallows a gap-down candle: the sellers' final push "
        "gets absorbed. A rare bullish reversal.",
        "Red marubozu candles to 99.50 and 99.00, a red candle gapping to 98.70 with a wick to 99.20, then a red one from 99.40 to 98.40 engulfing it."),
    "upside_gap_three_methods": (
        "In a rise, two long green candles with a gap between them, then a red candle that fills the gap. "
        "The dip only filled the gap; the rise usually continues.",
        "Green from 99.00 to 100.00, a gap, green from 100.30 to 101.20, then red from 101.00 to 100.10."),
    "downside_gap_three_methods": (
        "In a decline, two long red candles with a gap between them, then a green candle that fills the gap. "
        "The bounce only filled the gap; the decline usually continues.",
        "Red from 101.00 to 100.00, a gap, red from 99.70 to 98.80, then green from 99.00 to 99.90."),
    "high_price_gapping_play": (
        "After a long green candle, price rests near its top in small candles, then gaps above them: "
        "the pause resolved upward.",
        "Green from 99.00 to 101.00, small candles between 100.70 and 101.00, then a candle opens at 101.40."),
    "low_price_gapping_play": (
        "After a long red candle, price rests near its bottom in small candles, then gaps below them: "
        "the pause resolved downward.",
        "Red from 101.00 to 99.00, small candles between 99.00 and 99.30, then a candle opens at 98.60."),
    "bullish_side_by_side_white_lines": (
        "In a rise, two similar green candles side by side, both gapped above the candle before: "
        "buyers are holding the gap, and the rise usually continues.",
        "A candle closes 100.00, then two green candles from 100.40 to 100.90 and from 100.40 to 101.00."),
    "bearish_side_by_side_white_lines": (
        "In a decline, two similar green candles gapped below the candle before: the bounces can't close the "
        "gap, and the decline usually continues.",
        "A candle closes 100.00, then two green candles from 99.20 to 99.60 and from 99.20 to 99.70."),
    "three_stars_in_the_south": (
        "Three shrinking red candles with higher lows after a decline: each push down is weaker than the last. "
        "A rare sign the selling is drying up.",
        "Red from 101.00 to 99.50 (low 98.80), red from 99.80 to 99.20 (low 99.00), then a small red from 99.30 to 99.20."),
    "williams_fractal_high": (
        "A candle whose high is above the two candles on each side: a local peak that traders use as a "
        "resistance or stop level. Very common on short charts.",
        "Highs of 100.40, 100.70, 101.00, 100.80 and 100.50: the 101.00 candle is a fractal high."),
    "williams_fractal_low": (
        "A candle whose low is below the two candles on each side: a local trough that traders use as a "
        "support or stop level. Very common on short charts.",
        "Lows of 99.60, 99.30, 99.00, 99.20 and 99.50: the 99.00 candle is a fractal low."),
    "zigzag_step_up": (
        "Price made a higher swing low, then closed above the last swing high: one more step up the "
        "staircase of higher highs and higher lows that defines an uptrend.",
        "A swing high at 101.00, a pullback to a higher low at 99.80, then a close at 101.20."),
    "zigzag_step_down": (
        "Price made a lower swing high, then closed below the last swing low: one more step down the "
        "staircase of lower highs and lower lows that defines a downtrend.",
        "A swing low at 99.00, a bounce to a lower high at 100.20, then a close at 98.80."),
    "two_crows": (
        "After a long green candle, a red candle gaps up and fades, then another red candle closes into the "
        "green body: the rally is being sold. Bearish.",
        "Green from 99.00 to 101.00, red from 101.50 to 101.20, then red from 101.40 to 100.30."),
    "upside_gap_two_crows": (
        "After a long green candle, two red candles gap above it, the second engulfing the first: "
        "buyers can't hold the higher prices. Bearish.",
        "Green from 99.00 to 101.00, red from 101.60 to 101.40, then red from 101.80 to 101.20."),
    "advance_block": (
        "Three green candles with shrinking bodies and growing upper wicks: still rising, but each push meets "
        "more selling. A warning that the rise is tiring.",
        "Green from 99.00 to 100.00, from 99.90 to 100.50 (high 100.80), then from 100.40 to 100.70 (high 101.20)."),
    "deliberation": (
        "Two long green candles, then a small one: buyers are hesitating and the rise is stalling. "
        "A caution sign at the top of a run.",
        "Green from 99.00 to 100.00, from 99.90 to 101.00, then from 101.00 to 101.10."),
    "bullish_three_line_strike": (
        "Three falling red candles, then one green candle that erases all three: the sellers' whole run "
        "undone in a single candle.",
        "Red closes at 100.00, 99.50 and 99.00, then a green candle opens 98.90 and closes 100.20."),
    "bearish_three_line_strike": (
        "Three rising green candles, then one red candle that erases all three: the buyers' whole run "
        "undone in a single candle.",
        "Green closes at 100.00, 100.50 and 101.00, then a red candle opens 101.10 and closes 99.80."),
    "rising_three_methods": (
        "A long green candle, three small candles drifting inside its range, then a long green candle closing "
        "higher: a rest inside an uptrend that resumes.",
        "Green from 99.00 to 101.00, small candles between 100.20 and 100.80, then a green close at 101.60."),
    "falling_three_methods": (
        "A long red candle, three small candles drifting inside its range, then a long red candle closing "
        "lower: a rest inside a downtrend that resumes.",
        "Red from 101.00 to 99.00, small candles between 99.20 and 99.80, then a red close at 98.40."),
    "bullish_mat_hold": (
        "A long green candle, a gap up, three small candles holding above its low, then a higher close: "
        "a firmer version of rising three methods.",
        "Green from 99.00 to 101.00, small candles from 101.30 drifting to 100.70, then a green close at 101.80."),
    "bearish_mat_hold": (
        "A long red candle, a gap down, three small candles holding below its high, then a lower close: "
        "a firmer version of falling three methods.",
        "Red from 101.00 to 99.00, small candles from 98.70 drifting to 99.30, then a red close at 98.20."),
    "bullish_breakaway": (
        "A gap down and three more lower candles, then a green candle that closes back into the gap: "
        "the selling spree reversed.",
        "A red candle to 100.00, a gap to 99.50, lower candles to 98.80, then a green close at 99.80."),
    "bearish_breakaway": (
        "A gap up and three more higher candles, then a red candle that closes back into the gap: "
        "the buying spree reversed.",
        "A green candle to 100.00, a gap to 100.50, higher candles to 101.20, then a red close at 100.20."),
    "ladder_bottom": (
        "Three long red candles, a fourth red one with an upper wick (buyers testing), then a green candle "
        "opening higher: a bottom after a staircase down.",
        "Red candles to 99.00, 98.20 and 97.50, a red one from 97.60 to 97.20 with a high of 98.00, then green from 97.80 to 98.60."),
    "doji": (
        "It opened and closed at the same price: buyers and sellers ended even. On its own it means indecision; "
        "it matters more after a long move or at a key level.",
        "A candle opens 100.00, trades between 99.80 and 100.30, and closes at 100.00."),
    "long_legged_doji": (
        "A doji with long wicks both ways: big swings in both directions ended where they started. "
        "Strong indecision, often before a bigger move.",
        "A candle opens 100.00, swings to 101.00 and to 99.00, and closes at 100.00."),
    "spinning_top": (
        "A small body with wicks on both sides: neither buyers nor sellers won the candle. Indecision.",
        "A candle opens 100.00, trades between 99.40 and 100.60, and closes at 100.10."),
}


def glossary() -> dict[str, dict[str, str]]:
    """Every signal kind: its label, what it looks like (candles only), what it means and an example."""
    out = {}
    for kind, label in {**LABELS, LIVE_CROSS: "Live level cross"}.items():
        meaning, example = EXPLAIN[kind]
        out[kind] = {"label": label, "looks": CANDLES.get(kind, ("", ""))[1], "meaning": meaning, "example": example}
    return out
