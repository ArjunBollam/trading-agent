"""Chart signals on intraday bars (5-minute by default), for `paperbot scan`.

Code computes every signal from bars: nothing here asks the AI or places an order. Each signal
is an event on the last completed bar (a cross, a touch that held, a break), not a state, so a
condition that persists doesn't repeat on every scan.

Covered:
  VWAP: reclaim, loss, bounce off it, rejection at it.
  RSI(14): into oversold / overbought, and back out of it.
  EMA 9/21 cross, MACD(12, 26, 9) signal-line cross.
  Bollinger(20, 2): close back inside a band, and a squeeze that breaks out.
  Support and resistance (prior-day high/low/close, classic pivots, the 15-minute opening range,
  the high and low of day so far): a test that held, and a breakout or breakdown, with volume.
  Reversal candles at a level: engulfing, hammer, shooting star.
  Volume spikes. On daily bars: golden / death cross (50/200 SMA) and a new 52-week high or low.
  Chart patterns (formations.py): the classic ones (double and triple tops and bottoms, head and
  shoulders, triangles, wedges, channels, rectangles, flags, pennants), about 55 candlestick
  patterns, and Fibonacci ones (AB=CD, Gartley, Bat, Butterfly, Crab, Three Drives, retracements).

These are technical signals, not predictions. docs/RESEARCH.md has the evidence: most intraday
patterns are weak once costs are counted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from . import formations
from .config import ScanConfig
from .features import SESSION_CLOSE, Bar, atr, regular, session_of

LABELS = {
    "vwap_reclaim": "VWAP reclaim", "vwap_lost": "Lost VWAP",
    "vwap_bounce": "VWAP bounce", "vwap_reject": "VWAP rejection",
    "rsi_oversold": "RSI oversold", "rsi_oversold_exit": "RSI back out of oversold",
    "rsi_overbought": "RSI overbought", "rsi_overbought_exit": "RSI back out of overbought",
    "ema_cross_up": "EMA 9/21 bullish cross", "ema_cross_down": "EMA 9/21 bearish cross",
    "macd_cross_up": "MACD bullish cross", "macd_cross_down": "MACD bearish cross",
    "bb_reentry_up": "Back inside lower Bollinger band", "bb_reentry_down": "Back inside upper Bollinger band",
    "squeeze_breakout_up": "Bollinger squeeze breakout", "squeeze_breakout_down": "Bollinger squeeze breakdown",
    "support_test": "Support test held", "resistance_test": "Resistance test held",
    "breakout": "Breakout", "breakdown": "Breakdown",
    "bullish_engulfing": "Bullish engulfing", "bearish_engulfing": "Bearish engulfing",
    "hammer": "Hammer", "shooting_star": "Shooting star",
    "volume_spike_up": "Volume spike (up)", "volume_spike_down": "Volume spike (down)",
    "golden_cross": "Golden cross (daily 50/200)", "death_cross": "Death cross (daily 50/200)",
    "high_52w": "New 52-week high", "low_52w": "New 52-week low",
}


FAMILIES = {
    "support_test": "level_test", "resistance_test": "level_test", "breakout": "break", "breakdown": "break",
    "bullish_engulfing": "candle", "bearish_engulfing": "candle", "hammer": "candle", "shooting_star": "candle",
    "golden_cross": "ma_daily", "death_cross": "ma_daily", "high_52w": "52w", "low_52w": "52w",
}
LABELS |= formations.LABELS
for _kind in formations.LABELS:
    FAMILIES.setdefault(_kind, _kind)  # each chart pattern has its own cooldown

CLOSING_AUCTION = time(15, 50)  # bars from here on carry the closing-auction volume


@dataclass(frozen=True)
class Level:
    name: str
    price: float


@dataclass(frozen=True)
class ChartSignal:
    symbol: str
    kind: str  # a key of LABELS
    side: str  # buy | sell
    price: float
    bar_time: datetime
    detail: str
    level: str | None = None  # the level a support, resistance or breakout signal refers to
    weak: bool = False  # e.g. a breakout on light volume: printed and logged, alerted only if scan.alert_weak

    @property
    def label(self) -> str:
        return LABELS[self.kind]

    @property
    def family(self) -> str:
        """Signals that share a cooldown: a VWAP reclaim and a VWAP loss 10 minutes apart are chop."""
        if self.kind.startswith("rsi_"):
            return self.kind  # RSI's 30/70 thresholds already have hysteresis
        return FAMILIES.get(self.kind, self.kind.split("_")[0])

    @property
    def key(self) -> tuple[str, str, str | None]:
        return (self.symbol, self.family, self.level)


@dataclass(frozen=True)
class Summary:
    """Where a symbol stands on the last completed bar, for the scan table."""

    symbol: str
    price: float | None
    bar_time: datetime | None
    rsi: float | None = None
    vwap: float | None = None
    vs_vwap_pct: float | None = None
    trend: str | None = None  # up | down | flat, from EMA 9 vs 21


def ema_series(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < n:
        return out
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    out[n - 1] = e
    for i in range(n, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def rsi_series(closes: list[float], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) < n + 1:
        return out
    diffs = [b - a for a, b in zip(closes, closes[1:], strict=False)]
    gain = sum(max(0.0, d) for d in diffs[:n]) / n
    loss = sum(max(0.0, -d) for d in diffs[:n]) / n

    def value(g: float, lo: float) -> float:
        return 100.0 if lo == 0 else 100 - 100 / (1 + g / lo)

    out[n] = value(gain, loss)
    for i, d in enumerate(diffs[n:], start=n + 1):  # Wilder smoothing
        gain = (gain * (n - 1) + max(0.0, d)) / n
        loss = (loss * (n - 1) + max(0.0, -d)) / n
        out[i] = value(gain, loss)
    return out


def macd_series(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    f, s = ema_series(closes, fast), ema_series(closes, slow)
    line = [a - b if a is not None and b is not None else None for a, b in zip(f, s, strict=True)]
    start = next((i for i, v in enumerate(line) if v is not None), len(line))
    sig = [None] * start + ema_series([v for v in line[start:] if v is not None], signal)
    return line, sig


def bollinger(closes: list[float], n: int = 20, k: float = 2.0) -> tuple[float, float, float] | None:
    if len(closes) < n:
        return None
    w = closes[-n:]
    mid = sum(w) / n
    sd = (sum((c - mid) ** 2 for c in w) / n) ** 0.5
    return mid - k * sd, mid, mid + k * sd


def sma(values: list[float], n: int) -> float | None:
    return sum(values[-n:]) / n if len(values) >= n else None


def session_vwaps(today: list[Bar]) -> list[float | None]:
    """VWAP after each of today's bars, over the regular session only: None before the open, and
    the day's closing VWAP through after-hours bars."""
    out, pv, vol = [], 0.0, 0.0
    for b in today:
        if regular(b):
            pv += (b.h + b.l + b.c) / 3 * b.v
            vol += b.v
        out.append(pv / vol if vol > 0 else None)
    return out


def levels(today: list[Bar], prev_session: list[Bar], bar_minutes: int) -> tuple[list[Level], list[Level]]:
    """(supports, resistances) known before the last bar, so the last bar can test or break them.
    Prior-day levels, the opening range and the high and low of day come from regular-session
    bars, so pre-market and after-hours trading don't move them."""
    sup: list[Level] = []
    res: list[Level] = []
    prev_session = [b for b in prev_session if regular(b)]
    if prev_session:
        h, lo, c = max(b.h for b in prev_session), min(b.l for b in prev_session), prev_session[-1].c
        p = (h + lo + c) / 3
        sup += [Level("prior-day low", lo), Level("prior-day close", c), Level("pivot S1", 2 * p - h),
                Level("pivot S2", p - (h - lo))]
        res += [Level("prior-day high", h), Level("prior-day close", c), Level("pivot R1", 2 * p - lo),
                Level("pivot R2", p + (h - lo))]
    or_bars = max(1, 15 // bar_minutes)
    known = [b for b in today[:-1] if regular(b)]
    if len(known) >= or_bars:  # the opening range is complete before the last bar
        rng = known[:or_bars]
        sup.append(Level("opening-range low", min(b.l for b in rng)))
        res.append(Level("opening-range high", max(b.h for b in rng)))
    earlier = [b for b in today[:-3] if regular(b)]  # a swing needs a few bars of distance from the bar testing it
    if len(earlier) > or_bars:
        sup.append(Level("low of day", min(b.l for b in earlier)))
        res.append(Level("high of day", max(b.h for b in earlier)))
    return sup, res


def _swing_before(diff: list[float | None]) -> float:
    """The largest |diff| over the run of same-signed values that ends on the bar before the last."""
    best, i = 0.0, len(diff) - 2
    sign = None if i < 0 or diff[i] is None else diff[i] >= 0
    while i >= 0 and diff[i] is not None and (diff[i] >= 0) == sign:
        best = max(best, abs(diff[i]))
        i -= 1
    return best


def _crossed_up(prev_a, prev_b, a, b) -> bool:
    return None not in (prev_a, prev_b, a, b) and prev_a <= prev_b and a > b


def _crossed_down(prev_a, prev_b, a, b) -> bool:
    return None not in (prev_a, prev_b, a, b) and prev_a >= prev_b and a < b


def analyze(symbol: str, bars: list[Bar], day: date, cfg: ScanConfig, daily: list[Bar] | None = None,
            ) -> tuple[list[ChartSignal], Summary]:
    """Signals on the last bar of `bars` (completed bars, oldest first). Bars from earlier sessions
    warm up the indicators; VWAP, the opening range and today's high/low use today's regular-session
    bars only. Pre-market and after-hours bars, when there are any, count for everything else.
    `daily` (completed daily bars before today) adds the daily signals."""
    if not bars:
        return [], Summary(symbol, None, None)
    today = [b for b in bars if b.t.date() == day]
    last = bars[-1]
    closes = [b.c for b in bars]
    rsi = rsi_series(closes, cfg.rsi_period)
    e9, e21 = ema_series(closes, 9), ema_series(closes, 21)
    vw = session_vwaps(today)
    trend = None
    if e9[-1] is not None and e21[-1] is not None:
        trend = "up" if e9[-1] > e21[-1] * 1.0005 else "down" if e9[-1] < e21[-1] * 0.9995 else "flat"
    vwap_now = vw[-1] if vw else None
    summary = Summary(symbol, last.c, last.t, None if rsi[-1] is None else round(rsi[-1], 1),
                      None if vwap_now is None else round(vwap_now, 2),
                      None if not vwap_now else round((last.c - vwap_now) / vwap_now * 100, 2), trend)
    if not today or len(bars) < 2:
        return [], summary  # no completed bar today yet

    prev = bars[-2]
    out: list[ChartSignal] = []
    a = atr(bars[-30:]) or 0.0
    swing = cfg.min_cross_atr * a  # how far past a line a close must be, so chop around it doesn't count
    opening = len(today) == 1  # the gap bar: indicators jump on the overnight move, only level breaks mean much

    def add(kind: str, side: str, detail: str, level: str | None = None, weak: bool = False) -> None:
        if not opening or kind in ("breakout", "breakdown"):
            out.append(ChartSignal(symbol, kind, side, last.c, last.t, detail, level, weak))

    # VWAP (from the second bar of the day, so there's a VWAP before the last bar)
    if len(today) >= 2 and vw[-2] is not None and vwap_now is not None:
        tol = vwap_now * cfg.level_tolerance_pct / 100
        if prev.c < vw[-2] and last.c > vwap_now + swing:
            add("vwap_reclaim", "buy", f"closed back above VWAP {vwap_now:.2f}")
        elif prev.c > vw[-2] and last.c < vwap_now - swing:
            add("vwap_lost", "sell", f"closed below VWAP {vwap_now:.2f}")
        elif prev.c > vw[-2] and last.l <= vwap_now + tol and last.c > vwap_now + swing and last.c > last.o:
            add("vwap_bounce", "buy", f"dipped to VWAP {vwap_now:.2f} and closed above it")
        elif prev.c < vw[-2] and last.h >= vwap_now - tol and last.c < vwap_now - swing and last.c < last.o:
            add("vwap_reject", "sell", f"rallied to VWAP {vwap_now:.2f} and closed below it")

    # RSI
    r0, r1 = rsi[-2], rsi[-1]
    if r0 is not None and r1 is not None:
        lo, hi = cfg.rsi_oversold, cfg.rsi_overbought
        if r0 >= lo > r1:
            add("rsi_oversold", "buy", f"RSI({cfg.rsi_period}) fell to {r1:.1f}, under {lo:g}: oversold, watch for a bounce")
        elif r0 < lo <= r1:
            add("rsi_oversold_exit", "buy", f"RSI({cfg.rsi_period}) rose back to {r1:.1f}, above {lo:g}")
        if r0 <= hi < r1:
            add("rsi_overbought", "sell", f"RSI({cfg.rsi_period}) rose to {r1:.1f}, over {hi:g}: overbought, watch for a pullback")
        elif r0 > hi >= r1:
            add("rsi_overbought_exit", "sell", f"RSI({cfg.rsi_period}) fell back to {r1:.1f}, below {hi:g}")

    # Moving averages and MACD. A cross counts only after a real swing the other way, not a wobble.
    ema_gap = [x - y if x is not None and y is not None else None for x, y in zip(e9, e21, strict=True)]
    ema_swung = _swing_before(ema_gap) >= swing
    if ema_swung and _crossed_up(e9[-2], e21[-2], e9[-1], e21[-1]):
        add("ema_cross_up", "buy", f"EMA 9 ({e9[-1]:.2f}) crossed above EMA 21 ({e21[-1]:.2f})")
    elif ema_swung and _crossed_down(e9[-2], e21[-2], e9[-1], e21[-1]):
        add("ema_cross_down", "sell", f"EMA 9 ({e9[-1]:.2f}) crossed below EMA 21 ({e21[-1]:.2f})")
    line, sig = macd_series(closes)
    hist = [x - y if x is not None and y is not None else None for x, y in zip(line, sig, strict=True)]
    macd_swung = _swing_before(hist) >= swing / 2
    if macd_swung and _crossed_up(line[-2], sig[-2], line[-1], sig[-1]):
        add("macd_cross_up", "buy", f"MACD {line[-1]:.3f} crossed above its signal {sig[-1]:.3f}")
    elif macd_swung and _crossed_down(line[-2], sig[-2], line[-1], sig[-1]):
        add("macd_cross_down", "sell", f"MACD {line[-1]:.3f} crossed below its signal {sig[-1]:.3f}")

    # Bollinger bands
    bb_now, bb_prev = bollinger(closes), bollinger(closes[:-1])
    if bb_now and bb_prev:
        lower, _, upper = bb_now
        if prev.c < bb_prev[0] and last.c > lower:
            add("bb_reentry_up", "buy", f"closed back above the lower band {lower:.2f} after closing below it")
        elif prev.c > bb_prev[2] and last.c < upper:
            add("bb_reentry_down", "sell", f"closed back below the upper band {upper:.2f} after closing above it")
        widths = [w[2] - w[0] for i in range(max(20, len(closes) - 60), len(closes))
                  if (w := bollinger(closes[:i]))]
        if len(widths) >= 20 and bb_prev[2] - bb_prev[0] <= sorted(widths)[len(widths) // 10]:
            if last.c > upper:
                add("squeeze_breakout_up", "buy", f"bands were at their narrowest in {len(widths)} bars; closed above {upper:.2f}")
            elif last.c < lower:
                add("squeeze_breakout_down", "sell", f"bands were at their narrowest in {len(widths)} bars; closed below {lower:.2f}")

    # Volume against the previous 20 bars of the same kind: regular-session bars against regular ones,
    # pre-market and after-hours bars against extended-hours ones, which trade far less. A session's
    # first bar and the closing auction always spike, so they're skipped.
    window = [b.v for b in bars[:-1] if regular(b) == regular(last)][-20:]
    vol_ratio = last.v / (sum(window) / len(window)) if len(window) >= 10 and sum(window) > 0 else None
    auction = CLOSING_AUCTION <= last.t.time() < SESSION_CLOSE
    first = prev.t.date() != last.t.date() or session_of(prev.t) != session_of(last.t)
    if vol_ratio is not None and not first and not auction and vol_ratio >= cfg.volume_spike_ratio and last.c != last.o:
        up = last.c > last.o
        add("volume_spike_up" if up else "volume_spike_down", "buy" if up else "sell",
            f"volume {vol_ratio:.1f}x the 20-bar average on a {'green' if up else 'red'} bar")
    volume_note, light = "", vol_ratio is not None and vol_ratio < cfg.breakout_volume_ratio
    if vol_ratio is not None:
        volume_note = (f" on {vol_ratio:.1f}x volume" if vol_ratio >= cfg.breakout_volume_ratio
                       else f" on light volume ({vol_ratio:.1f}x), so less reliable")

    # Support and resistance
    prev_day = sorted({b.t.date() for b in bars if b.t.date() < day})
    prev_session = [b for b in bars if prev_day and b.t.date() == prev_day[-1]]
    sup, res = levels(today, prev_session, cfg.bar_minutes)
    tol = last.c * cfg.level_tolerance_pct / 100
    broken_up = [lv for lv in res if prev.c <= lv.price < last.c]
    broken_down = [lv for lv in sup if prev.c >= lv.price > last.c]
    if broken_up:
        lv = max(broken_up, key=lambda x: x.price)
        add("breakout", "buy", f"closed above the {lv.name} {lv.price:.2f}{volume_note}", lv.name, light)
    if broken_down:
        lv = min(broken_down, key=lambda x: x.price)
        add("breakdown", "sell", f"closed below the {lv.name} {lv.price:.2f}{volume_note}", lv.name, light)
    held_sup = [lv for lv in sup if prev.c > lv.price and lv.price - tol <= last.l <= lv.price + tol and last.c > lv.price]
    held_res = [lv for lv in res if prev.c < lv.price and lv.price - tol <= last.h <= lv.price + tol and last.c < lv.price]
    if held_sup and not broken_down:
        lv = min(held_sup, key=lambda x: abs(x.price - last.l))
        add("support_test", "buy", f"tested the {lv.name} {lv.price:.2f} (low {last.l:.2f}) and closed above it", lv.name)
    if held_res and not broken_up:
        lv = min(held_res, key=lambda x: abs(x.price - last.h))
        add("resistance_test", "sell", f"tested the {lv.name} {lv.price:.2f} (high {last.h:.2f}) and closed below it", lv.name)

    # Reversal candles, only where they mean something: at a support or resistance (VWAP included)
    near_sup = [lv for lv in sup if abs(last.l - lv.price) <= 2 * tol]
    near_res = [lv for lv in res if abs(last.h - lv.price) <= 2 * tol]
    if vwap_now is not None:
        if abs(last.l - vwap_now) <= 2 * tol:
            near_sup.append(Level("VWAP", vwap_now))
        if abs(last.h - vwap_now) <= 2 * tol:
            near_res.append(Level("VWAP", vwap_now))
    body = abs(last.c - last.o)
    upper_wick, lower_wick = last.h - max(last.c, last.o), min(last.c, last.o) - last.l
    big_enough = a > 0 and (last.h - last.l) >= 0.5 * a
    if near_sup:
        where = f"at the {near_sup[0].name} {near_sup[0].price:.2f}"
        if prev.c < prev.o and last.c > last.o and last.o <= prev.c and last.c >= prev.o:
            add("bullish_engulfing", "buy", f"green bar engulfed the prior red bar {where}")
        elif big_enough and body > 0 and lower_wick >= 2 * body and upper_wick <= body:
            add("hammer", "buy", f"long lower wick {where}: buyers stepped in")
    if near_res:
        where = f"at the {near_res[0].name} {near_res[0].price:.2f}"
        if prev.c > prev.o and last.c < last.o and last.o >= prev.c and last.c <= prev.o:
            add("bearish_engulfing", "sell", f"red bar engulfed the prior green bar {where}")
        elif big_enough and body > 0 and upper_wick >= 2 * body and lower_wick <= body:
            add("shooting_star", "sell", f"long upper wick {where}: sellers stepped in")

    # Chart, candlestick and Fibonacci patterns. A classic pattern's breakout on light volume is weak.
    have = {s.kind for s in out}
    for kind, side, detail, weak, level in formations.find(bars, a, cfg):
        if kind in have:
            continue  # already reported above, at a level
        if kind in formations.CHART:
            detail, weak = detail + volume_note, weak or light
        add(kind, side, detail, level, weak)

    # Daily: the live price stands in for today's close
    if daily:
        dc = [b.c for b in daily if b.t.date() < day]
        s50p, s200p = sma(dc, 50), sma(dc, 200)
        s50, s200 = sma(dc + [last.c], 50), sma(dc + [last.c], 200)
        if _crossed_up(s50p, s200p, s50, s200):
            add("golden_cross", "buy", f"the 50-day average {s50:.2f} is crossing above the 200-day {s200:.2f}")
        elif _crossed_down(s50p, s200p, s50, s200):
            add("death_cross", "sell", f"the 50-day average {s50:.2f} is crossing below the 200-day {s200:.2f}")
        year = [b for b in daily if day - timedelta(days=365) <= b.t.date() < day]
        if len(year) >= 200:
            hi52, lo52 = max(b.h for b in year), min(b.l for b in year)
            if prev.c <= hi52 < last.c:
                add("high_52w", "buy", f"above the 52-week high {hi52:.2f}")
            elif prev.c >= lo52 > last.c:
                add("low_52w", "sell", f"below the 52-week low {lo52:.2f}")
    return out, summary
