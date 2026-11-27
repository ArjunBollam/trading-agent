"""What the charts draw besides the scanner's own signals: QQE, support and resistance breaks, squeeze releases and
smart money concepts, as the JSON the page reads. Used by the scanner for the dashboard's charts and by the Charts tab
for any interval, always on the candles the chart shows."""

from __future__ import annotations

from collections.abc import Callable

from .config import ScanConfig
from .features import Bar
from .overlays import level_breaks, overnight_gap, pivot_levels, squeeze
from .qqe import qqe
from .smc import smart_money


def overlay_json(bars: list[Bar], sc: ScanConfig, stamp: Callable[[int], int], keep: Callable[[int], bool] = lambda i: True,
                 live: bool = True, which: dict[str, bool] | None = None) -> dict:
    """Each overlay that's on, worked out over all of `bars` (the earlier ones warm it up), keeping what happens on the
    candles `keep` picks. `stamp` gives a candle's time on the chart; `live`: the last candle is the newest there is."""
    on = which or {"qqe": sc.qqe, "sr": sc.sr_breaks, "sqz": sc.squeeze, "smc": sc.smc}
    out: dict = {}
    t = stamp
    drawn = [i for i in range(len(bars)) if keep(i)]

    if on.get("qqe"):
        out["qqe"] = [{"t": t(s.index), "side": s.side, "rsi": round(s.rsi, 1), "line": round(s.line, 1),
                       "gap": overnight_gap(bars, s.index)}
                      for s in qqe([b.c for b in bars], sc.qqe_rsi_period, sc.qqe_smoothing, sc.qqe_factor) if keep(s.index)]
    if on.get("sr"):
        levels = pivot_levels(bars, sc.sr_pivot_bars, sc.sr_pivot_bars)
        out["sr"] = {
            "breaks": [{"t": t(x.index), "side": x.side, "kind": x.kind, "level": round(x.level, 4), "vol": round(x.volume),
                        "gap": x.gap, "open": round(bars[x.index].o, 4)}
                       for x in level_breaks(bars, sc.sr_pivot_bars, sc.sr_pivot_bars, sc.sr_volume, levels) if keep(x.index)],
            "levels": [[t(i), *levels[i]] for n, i in enumerate(drawn) if n == 0 or levels[i] != levels[i - 1]]}
    if on.get("sqz"):
        found, lasting = squeeze(bars, sc.squeeze_length, sc.squeeze_bb_mult, sc.squeeze_kc_mult)
        out["sqz"] = {"on": lasting if drawn and live else 0,
                      "signals": [{"t": t(x.index), "side": x.side, "mom": round(x.momentum, 3), "candles": x.candles, "gap": x.gap}
                                  for x in found if keep(x.index)]}
    if on.get("smc") and bars:
        m = smart_money(bars, sc.smc_swing)
        inner = [x for x in m.blocks if x.internal][:sc.smc_blocks]
        outer = [x for x in m.blocks if not x.internal][:sc.smc_swing_blocks]
        out["smc"] = {
            "swing": m.swing_bias, "internal": m.internal_bias,
            "structure": [{"from": t(x.pivot), "t": t(x.index), "level": round(x.level, 4), "side": x.side, "kind": x.kind,
                           "internal": x.internal, "gap": x.gap} for x in m.structures if keep(x.index)],
            "blocks": [{"from": t(x.index), "top": round(x.top, 4), "bottom": round(x.bottom, 4), "side": x.side,
                        "internal": x.internal} for x in outer + inner],
            "equal": [{"from": t(x.first), "t": t(x.second), "levels": [round(v, 4) for v in x.levels], "side": x.side,
                       "known": t(x.known)} for x in m.equals if keep(x.known)],
            "fvg": [{"from": t(x.index), "top": round(x.top, 4), "bottom": round(x.bottom, 4), "side": x.side}
                    for x in m.gaps if keep(x.index)][:10],
            "range": {"top": round(m.top, 4), "bottom": round(m.bottom, 4), "topFrom": t(m.top_index),
                      "bottomFrom": t(m.bottom_index), "from": t(m.range_index),
                      "zones": {k: [round(v, 4) for v in z] for k, z in m.zones().items()}}
            if m.top is not None and m.bottom is not None else None}
    return out
