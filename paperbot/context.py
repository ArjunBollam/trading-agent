"""The bigger picture for `paperbot scan`: what the last few hours and a slower candle say.

A 5-minute signal is one candle's view. Before it's alerted, it's checked against the last
scan.context_minutes (3 hours by default) of the same chart and against slower candles
(scan.context_bar_minutes, 15 by default). Each piece of evidence is a Factor with a side and a
weight; the weights add up to a score, and past scan.context_threshold either way the picture is
bullish or bearish.

  weight 3  a chart pattern that broke in the window and still holds (price hasn't closed back
            through its line): head and shoulders, double and triple tops and bottoms, triangles,
            wedges, channels, rectangles, flags and pennants, on the 5-minute and the slower candle
  weight 2  a head and shoulders or triple top or bottom still forming (last shoulder in place,
            neckline not broken yet, price within 3 ATRs of it), and the swing structure: lower highs and lower lows, or
            higher highs and higher lows, over the window
  weight 1  a double top or bottom still forming; the slower candle's EMA 9/21 trend and MACD;
            its RSI past 70 or 30 (stretched, so the risk is the other way); price against VWAP

A signal conflicts with the bigger picture when any weight-2-or-more factor points the other way, or
the score does. With scan.context_mode "warn" the alert says so and goes out at high priority; with
"suppress" it's logged, not pushed. Code only, like patterns.py: no AI, no orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from . import formations
from .config import ScanConfig
from .features import Bar, atr
from .patterns import LABELS, ema_series, macd_series, rsi_series, session_vwaps

MAJOR = 2  # a factor this heavy can veto a signal on its own
NEAR_NECKLINE_ATR = 3.0  # a forming top or bottom counts only this close to its neckline


@dataclass(frozen=True)
class Factor:
    side: int  # +1 bullish, -1 bearish
    weight: int
    text: str


@dataclass(frozen=True)
class Context:
    factors: tuple[Factor, ...]
    threshold: int
    minutes: int

    @property
    def score(self) -> int:
        return sum(f.side * f.weight for f in self.factors)

    @property
    def bias(self) -> str:
        s = self.score
        return "bullish" if s >= self.threshold else "bearish" if s <= -self.threshold else "mixed"

    def against(self, side: str) -> list[Factor]:
        """The factors that make a buy or sell signal go against the bigger picture (none: it doesn't)."""
        d = 1 if side == "buy" else -1
        majors = [f for f in self.factors if f.side == -d and f.weight >= MAJOR]
        if majors or self.score * d <= -self.threshold:
            return sorted((f for f in self.factors if f.side == -d), key=lambda f: -f.weight)
        return []

    def conflict(self, side: str) -> str | None:
        """"Bigger picture (last 3h) is bearish, score -5: ...; this buy signal goes against it." or None."""
        why = self.against(side)
        if not why:
            return None
        hours = f"{self.minutes / 60:g}h" if self.minutes % 30 == 0 else f"{self.minutes}m"
        return (f"Bigger picture (last {hours}) is {self.bias}, score {self.score:+d}: "
                f"{'; '.join(f.text for f in why[:4])}. This {side} signal goes against it.")

    def line(self) -> str:
        """For the log: the bias and every factor."""
        parts = ", ".join(f"{'+' if f.side > 0 else '-'}{f.weight} {f.text}" for f in self.factors) or "nothing stands out"
        return f"{self.bias} ({self.score:+d}): {parts}"


def resample(bars: list[Bar], minutes: int, bar_minutes: int) -> list[Bar]:
    """Bars of `minutes` from `bar_minutes` bars, inside each session; a bucket still filling is dropped."""
    out: list[Bar] = []
    bucket: list[Bar] = []

    def start(b: Bar) -> datetime:
        since = b.t.hour * 60 + b.t.minute - 9 * 60 - 30  # buckets line up with the 9:30 open
        return b.t - timedelta(minutes=since % minutes, seconds=b.t.second, microseconds=b.t.microsecond)

    def flush() -> None:
        if bucket:
            out.append(Bar(start(bucket[0]), bucket[0].o, max(x.h for x in bucket), min(x.l for x in bucket),
                           bucket[-1].c, sum(x.v for x in bucket)))

    for b in bars:
        if bucket and start(b) != start(bucket[0]):
            flush()
            bucket = []
        bucket.append(b)
    if bucket and bucket[-1].t + timedelta(minutes=bar_minutes) < start(bucket[0]) + timedelta(minutes=minutes):
        bucket = []  # still filling
    flush()
    return out


def _since(t: datetime, day: date) -> str:
    return f"{t:%H:%M}" if t.date() == day else f"{t:%m/%d %H:%M}"


def _broken(bars: list[Bar], first: int, cfg: ScanConfig, day: date, candle: str) -> list[Factor]:
    """Classic chart patterns that broke on a bar from `first` on and still hold on the last bar."""
    out, seen = [], set()
    disabled = set(cfg.disabled_patterns)
    for end in reversed(range(max(first, 20), len(bars))):  # newest first: the latest break of a kind wins
        if bars[end].t.date() != bars[end - 1].t.date():
            continue  # the opening bar: a "break" there is the overnight gap, as in patterns.analyze
        b = bars[:end + 1]
        a = atr(b[-30:]) or 0.0
        for k in formations.chart_breaks(b, a, cfg):
            if (k.kind, k.side) in seen or k.kind in disabled:
                continue
            seen.add((k.kind, k.side))
            down, buf = k.side == "sell", cfg.min_cross_atr * a
            if (down and bars[-1].c > k.line + buf) or (not down and bars[-1].c < k.line - buf):
                continue  # price has closed back through the line: the pattern failed
            out.append(Factor(-1 if down else 1, 3,
                              f"{LABELS[k.kind].lower()} on the {candle} broke {'down' if down else 'up'} "
                              f"at {_since(b[-1].t, day)} through {k.line:.2f}, still {'below' if down else 'above'} it"))
    return out


def _structure(bars: list[Bar], first: int, threshold: float) -> Factor | None:
    """Lower highs and lower lows (or higher and higher) over the window, from its swing points."""
    piv = [p for p in formations.zigzag(bars, threshold) if p.i >= first]
    highs, lows = [p.price for p in piv if p.high][-2:], [p.price for p in piv if not p.high][-2:]
    if len(highs) < 2 or len(lows) < 2:
        return None
    if highs[1] < highs[0] and lows[1] < lows[0]:
        return Factor(-1, 2, f"lower highs ({highs[0]:.2f} then {highs[1]:.2f}) and lower lows ({lows[0]:.2f} then {lows[1]:.2f})")
    if highs[1] > highs[0] and lows[1] > lows[0]:
        return Factor(1, 2, f"higher highs ({highs[0]:.2f} then {highs[1]:.2f}) and higher lows ({lows[0]:.2f} then {lows[1]:.2f})")
    return None


def assess(bars: list[Bar], day: date, cfg: ScanConfig) -> Context:
    """The bigger picture on the last of `bars` (completed bars at scan.bar_minutes, oldest first, a
    couple of earlier sessions included for warm-up)."""
    ctx = Context((), cfg.context_threshold, cfg.context_minutes)
    if len(bars) < 30 or cfg.context_mode == "off":
        return ctx
    out: list[Factor] = []
    a = atr(bars[-30:]) or 0.0
    cutoff = bars[-1].t + timedelta(minutes=cfg.bar_minutes) - timedelta(minutes=cfg.context_minutes)
    first = next(i for i, b in enumerate(bars) if b.t >= cutoff)
    fast = f"{cfg.bar_minutes}-minute chart"

    out += _broken(bars, first, cfg, day, fast)
    for f in formations.forming(bars, a, cfg):
        if f.confirmed < first or f.kind + "_forming" in cfg.disabled_patterns:
            continue  # its last shoulder came before the window: stale
        dist = abs(bars[-1].c - f.neckline) / a if a else 0.0
        if dist > NEAR_NECKLINE_ATR:
            continue  # too far from its neckline to be the next thing that happens
        weight = 1 if f.kind.startswith("double") else MAJOR
        out.append(Factor(1 if f.side == "buy" else -1, weight,
                          f"{LABELS[f.kind].lower()} forming on the {fast} since {_since(bars[f.start].t, day)}, "
                          f"neckline {f.neckline:.2f} ({dist:.1f} ATR away)"))
    if (s := _structure(bars, first, cfg.pattern_swing_atr * a)) is not None:
        out.append(s)

    today = [b for b in bars if b.t.date() == day]
    vw = session_vwaps(today)
    if vw and vw[-1] and a:
        gap = bars[-1].c - vw[-1]
        if abs(gap) >= cfg.min_cross_atr * a:
            out.append(Factor(1 if gap > 0 else -1, 1, f"price {'above' if gap > 0 else 'below'} VWAP {vw[-1]:.2f}"))

    if cfg.context_bar_minutes > cfg.bar_minutes and cfg.context_bar_minutes % cfg.bar_minutes == 0:
        slow = resample(bars, cfg.context_bar_minutes, cfg.bar_minutes)
        candle = f"{cfg.context_bar_minutes}-minute chart"
        if len(slow) >= 30:
            slow_first = next((i for i, b in enumerate(slow) if b.t >= cutoff), len(slow))
            slow_cfg = cfg.model_copy(update={"pattern_lookback": max(20, cfg.pattern_lookback * cfg.bar_minutes
                                                                       // cfg.context_bar_minutes)})
            out += _broken(slow, slow_first, slow_cfg, day, candle)
        closes = [b.c for b in slow]
        e9, e21 = ema_series(closes, 9), ema_series(closes, 21)
        if e9 and e9[-1] is not None and e21[-1] is not None and abs(e9[-1] - e21[-1]) > e21[-1] * 0.0005:
            up = e9[-1] > e21[-1]
            out.append(Factor(1 if up else -1, 1, f"{candle} trending {'up' if up else 'down'} (EMA 9 {e9[-1]:.2f} vs 21 {e21[-1]:.2f})"))
        line, sig = macd_series(closes)
        if line and line[-1] is not None and sig[-1] is not None and line[-1] != sig[-1]:
            up = line[-1] > sig[-1]
            out.append(Factor(1 if up else -1, 1, f"{candle} MACD {'above' if up else 'below'} its signal"))
        r = rsi_series(closes, cfg.rsi_period)
        if r and r[-1] is not None and (r[-1] > cfg.rsi_overbought or r[-1] < cfg.rsi_oversold):
            hot = r[-1] > cfg.rsi_overbought
            out.append(Factor(-1 if hot else 1, 1, f"{candle} RSI {r[-1]:.0f}, {'overbought' if hot else 'oversold'}"))
    return Context(tuple(sorted(out, key=lambda f: -f.weight)), cfg.context_threshold, cfg.context_minutes)
