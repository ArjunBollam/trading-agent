"""QQE signals: a smoothed RSI and a trailing line a few times its own average movement away.

A long is the smoothed RSI crossing above its line, a short is it crossing below. Same rules and
defaults as the TradingView script "QQE signals" by colinmck (RSI 14, smoothing 5, factor 4.238), so
the two match on the same candles. The signals are for looking at on the charts: they aren't
alerted and no order depends on them."""

from __future__ import annotations

from dataclasses import dataclass

from .patterns import ema_series, rsi_series


@dataclass(frozen=True)
class QQESignal:
    index: int  # into the closes
    side: str  # long | short
    rsi: float  # the smoothed RSI on that candle
    line: float  # the trailing line it crossed


def _ema(values: list[float | None], n: int) -> list[float | None]:
    """ema_series on a series that starts with Nones (it is seeded once it has n values, as in Pine)."""
    start = next((i for i, v in enumerate(values) if v is not None), len(values))
    return [None] * start + ema_series([v for v in values[start:] if v is not None], n)


def qqe(closes: list[float], rsi_period: int = 14, smoothing: int = 5, factor: float = 4.238) -> list[QQESignal]:
    rsi = _ema(rsi_series(closes, rsi_period), smoothing)
    wilders = rsi_period * 2 - 1
    moves: list[float | None] = [None] * len(rsi)
    for i in range(1, len(rsi)):
        if rsi[i] is not None and rsi[i - 1] is not None:
            moves[i] = abs(rsi[i] - rsi[i - 1])
    dar = [None if v is None else v * factor for v in _ema(_ema(moves, wilders), wilders)]

    out: list[QQESignal] = []
    up, down, trend = 0.0, 0.0, 1  # the bands under and over the RSI, and which one is the line
    up1 = down1 = up2 = down2 = None  # the previous candles' bands (None until there are any)
    above = below = None  # whether the RSI was over / under the line on the previous candle
    prev_rsi = None
    for i, (r, d) in enumerate(zip(rsi, dar, strict=True)):
        if r is None or d is None or prev_rsi is None:
            prev_rsi = r
            continue
        new_up, new_down = r - d, r + d
        up = max(up1, new_up) if up1 is not None and prev_rsi > up1 and r > up1 else new_up
        down = min(down1, new_down) if down1 is not None and prev_rsi < down1 and r < down1 else new_down
        # Pine's cross(RSI, shortband[1]) and cross(longband[1], RSI)
        crossed_up = (down1 is not None and down2 is not None
                      and ((r > down1 and prev_rsi <= down2) or (r < down1 and prev_rsi >= down2)))
        crossed_down = (up1 is not None and up2 is not None
                        and ((up1 > r and up2 <= prev_rsi) or (up1 < r and up2 >= prev_rsi)))
        trend = 1 if crossed_up else -1 if crossed_down else trend
        line = up if trend == 1 else down
        is_above, is_below = r > line, r < line
        # the first candle with a line is warm-up: it has no earlier side to change from
        if above is not None:
            if is_above and not above:
                out.append(QQESignal(i, "long", r, line))
            if is_below and not below:
                out.append(QQESignal(i, "short", r, line))
        above, below = is_above, is_below
        up2, down2, up1, down1, prev_rsi = up1, down1, up, down, r
    return out
