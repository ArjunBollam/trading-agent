"""`paperbot simulate-exits`: compare exit settings on real historical option prices.

Each past trading day, at a fixed time, it "buys" both the near-the-money call and put with
about a week or two to expiration, then replays the option's 5-minute bars under several exit
settings (stop, take-profit, breakeven). Buying both sides every day means there is no entry
edge on purpose: the results show how exit rules interact with time decay and costs, not
whether a strategy makes money.

How to read it (from the overfitting research in docs/RESEARCH.md):
- Compare settings against each other, not against zero.
- Prefer a broad plateau of similar results over one standout cell, which is usually noise.
- Treat anything from fewer than ~100 trades as anecdotal.

Fills are approximations: bars are trade prices, historical quotes aren't available, so a
round-trip spread cost is assumed, stops fill at the stop price (or the bar's open if it gaps
through), and a stop and a target touched in the same bar count as the stop.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from statistics import mean, median
from zoneinfo import ZoneInfo

from .features import Bar


@dataclass(frozen=True)
class ExitParams:
    stop_pct: float
    tp_pct: float
    breakeven_pct: float | None = None
    trail_start_pct: float | None = None
    trail_pct: float | None = None
    max_hold_minutes: int | None = None
    close_all_at: time | None = None

    def label(self) -> str:
        parts = [f"stop {self.stop_pct:g}%", f"target {self.tp_pct:g}%"]
        if self.breakeven_pct is not None:
            parts.append(f"breakeven {self.breakeven_pct:g}%")
        if self.trail_pct is not None:
            parts.append(f"trail {self.trail_pct:g}% after {self.trail_start_pct:g}%")
        if self.max_hold_minutes is not None:
            parts.append(f"time stop {self.max_hold_minutes}m")
        return ", ".join(parts)


@dataclass(frozen=True)
class TradeResult:
    ret_pct: float  # after the assumed round-trip cost
    reason: str
    minutes: float


def simulate_trade(bars: list[Bar], entry: float, p: ExitParams, expiry: date, close_at_dte: int,
                   tz: ZoneInfo, cost_pct: float) -> TradeResult:
    """Replay one long option position bar by bar. Bars start at the entry bar."""
    stop = entry * (1 - p.stop_pct / 100)
    target = entry * (1 + p.tp_pct / 100)
    start = bars[0].t
    exit_px, reason, at = bars[-1].c, "horizon", bars[-1].t
    for b in bars:
        local = b.t.astimezone(tz)
        minutes = (b.t - start).total_seconds() / 60
        forced = None
        if (expiry - local.date()).days <= close_at_dte and b.t > start:
            forced = "expiry"
        elif p.close_all_at is not None and local.time() >= p.close_all_at:
            forced = "end_of_day"
        elif p.max_hold_minutes is not None and minutes >= p.max_hold_minutes:
            forced = "time_stop"
        if forced:
            exit_px, reason, at = b.o, forced, b.t
            break
        if b.o <= stop:
            exit_px, reason, at = b.o, "stop (gap)", b.t
            break
        if b.l <= stop:  # a stop and a target in the same bar count as the stop
            exit_px, reason, at = stop, "stop", b.t
            break
        if b.h >= target:
            exit_px, reason, at = max(target, b.o), "take_profit", b.t
            break
        # Ratchets use the bar's close and apply from the next bar, like the live code's ticks.
        if p.breakeven_pct is not None and b.c >= entry * (1 + p.breakeven_pct / 100):
            stop = max(stop, entry)
        if p.trail_pct is not None and b.c >= entry * (1 + p.trail_start_pct / 100):
            stop = max(stop, b.c * (1 - p.trail_pct / 100))
    ret = (exit_px - entry) / entry * 100 - cost_pct
    return TradeResult(ret, reason, (at - start).total_seconds() / 60)


@dataclass
class Summary:
    params: ExitParams
    n: int
    avg: float
    med: float
    win_rate: float
    worst: float
    best: float


def summarize(params: ExitParams, results: list[TradeResult]) -> Summary:
    rets = [r.ret_pct for r in results]
    return Summary(params, len(rets), mean(rets), median(rets), sum(r > 0 for r in rets) / len(rets) * 100,
                   min(rets), max(rets))


def from_config(x) -> ExitParams:
    """The profile's full exit settings (an ExitConfig)."""
    return ExitParams(x.stop_loss_pct, x.take_profit_pct, x.breakeven_at_pct, x.trail_start_pct, x.trail_pct,
                      x.max_hold_minutes, x.close_all_at)


def grid(current: ExitParams) -> list[ExitParams]:
    """Vary stop, target and breakeven around the profile; keep its other exits (trailing, time
    stop, end-of-day close) fixed, so rows compare like with like."""
    s0, t0 = current.stop_pct, current.tp_pct
    stops = sorted({round(s0 * 0.7), s0, round(s0 * 1.3)})
    tps = sorted({round(t0 * 0.67), t0, round(t0 * 1.5)})
    bes = [None, current.breakeven_pct or round(t0 / 2)]
    out = []
    for s in stops:
        for t in tps:
            for b in bes:
                if b is None or b < t:
                    out.append(ExitParams(s, t, b, current.trail_start_pct, current.trail_pct,
                                          current.max_hold_minutes, current.close_all_at))
    return out if current in out else out + [current]


def rank_stability(samples: list[Sample], variants: list[ExitParams], close_at_dte: int, tz: ZoneInfo,
                   cost_pct: float) -> float | None:
    """Spearman correlation between the settings' rankings on the earlier and later half of the
    days. Near 1: the ranking holds up. Near 0 or negative: it's noise."""
    days = sorted({s.day for s in samples})
    if len(days) < 10 or len(variants) < 4:
        return None
    cut = days[len(days) // 2]
    halves = [[s for s in samples if s.day < cut], [s for s in samples if s.day >= cut]]
    ranks = []
    for half in halves:
        avgs = {x.params: x.avg for x in run(half, variants, close_at_dte, tz, cost_pct)}
        order = sorted(variants, key=lambda v: avgs.get(v, float("-inf")))
        ranks.append({v: i for i, v in enumerate(order)})
    n = len(variants)
    d2 = sum((ranks[0][v] - ranks[1][v]) ** 2 for v in variants)
    return 1 - 6 * d2 / (n * (n * n - 1))


def bars_from(rows: list[dict]) -> list[Bar]:
    out = []
    for r in rows:
        t = r["t"] if isinstance(r["t"], datetime) else datetime.fromisoformat(str(r["t"]).replace("Z", "+00:00"))
        out.append(Bar(t, float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"]), float(r["v"])))
    return sorted(out, key=lambda b: b.t)


@dataclass(frozen=True)
class Sample:
    day: str
    symbol: str
    expiry: str
    entry: float
    bars: list[dict]


def collect(broker, underlying: str, days: int, entry_time: time, dte_range: tuple[int, int], hold_days: int,
            tz: ZoneInfo, cache_dir: Path, today: date, now: datetime, log=print) -> list[Sample]:
    """Fetch (or load from cache) one call and one put per past trading day. Read-only."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    sessions = broker.trading_days(today - timedelta(days=days * 2 + 10), today - timedelta(days=1))[-days:]
    samples: list[Sample] = []
    for day in sessions:
        path = cache_dir / f"{underlying}-{day}-{entry_time:%H%M}-{dte_range[0]}-{dte_range[1]}-{hold_days}.json"
        if path.exists():
            samples += [Sample(**s) for s in json.loads(path.read_text())]
            continue
        entry_dt = datetime.combine(day, entry_time, tzinfo=tz)
        stock = [b for b in bars_from(broker.stock_bars(underlying, "5Min", 80, start=entry_dt - timedelta(minutes=5)))]
        if not stock:
            continue
        spot = stock[0].c
        contracts = broker.historical_contracts(underlying, day + timedelta(days=dte_range[0]),
                                                day + timedelta(days=dte_range[1]), spot * 0.97, spot * 1.03)
        picked = []
        for right in ("C", "P"):
            side = sorted((c for c in contracts if c[3] == right), key=lambda c: (abs(c[2] - spot), c[1]))
            if side:
                picked.append(side[0])
        if not picked:
            continue
        horizon = min(datetime.combine(day + timedelta(days=hold_days), time(16, 0), tzinfo=tz),
                      datetime.combine(min(c[1] for c in picked), time(16, 0), tzinfo=tz),
                      now - timedelta(minutes=16))  # the free data plan blocks the latest 15 minutes
        try:
            series = broker.option_bars([c[0] for c in picked], entry_dt, horizon, "5Min")
        except Exception as e:
            log(f"  {day}: skipped ({e})")
            continue
        day_samples = []
        for sym, expiry, _, _ in picked:
            rows = series.get(sym, [])
            bars = bars_from(rows)
            if not bars or bars[0].t > entry_dt + timedelta(minutes=30):
                continue  # no trading near the entry time
            day_samples.append(Sample(str(day), sym, str(expiry), bars[0].o, rows))
        path.write_text(json.dumps([asdict(s) for s in day_samples], default=str))
        samples += day_samples
        log(f"  {day}: {', '.join(s.symbol for s in day_samples) or 'no data'}")
    return samples


def run(samples: list[Sample], variants: list[ExitParams], close_at_dte: int, tz: ZoneInfo,
        cost_pct: float) -> list[Summary]:
    out = []
    for p in variants:
        results = [simulate_trade(bars_from(s.bars), s.entry, p, date.fromisoformat(s.expiry), close_at_dte, tz,
                                  cost_pct) for s in samples if s.bars]
        if results:
            out.append(summarize(p, results))
    return out


def format_results(summaries: list[Summary], current: ExitParams, n_days: int, cost_pct: float,
                   stability: float | None = None) -> str:
    if not summaries:
        return "No historical data found for that window."
    n = summaries[0].n
    lines = [f"{n} simulated trades over {n_days} trading days (a call and a put per entry time), "
             f"round-trip cost {cost_pct:g}% assumed.", ""]
    w = max(len(x.params.label()) for x in summaries)
    lines.append(f"{'exit settings':{w}} {'avg':>7} {'median':>7} {'win%':>5} {'worst':>7} {'best':>7}")
    for s in sorted(summaries, key=lambda s: s.avg, reverse=True):
        mark = "  <- current config" if s.params == current else ""
        lines.append(f"{s.params.label():{w}} {s.avg:>+6.1f}% {s.med:>+6.1f}% {s.win_rate:>4.0f}% "
                     f"{s.worst:>+6.0f}% {s.best:>+6.0f}%{mark}")
    if n < 100:
        lines.append("")
        lines.append(f"Only {n} trades: treat the ranking as anecdotal. Run with more --days.")
    if stability is not None:
        verdict = ("the ranking held up between halves" if stability >= 0.6 else
                   "the ranking only partly held up" if stability >= 0.3 else "the ranking looks like noise")
        lines.append("")
        lines.append(f"Stability: rank correlation {stability:+.2f} between the earlier and later half of the days "
                     f"({verdict}).")
    lines.append("")
    lines.append("Entries have no edge by design (both sides every day), so compare rows with each other, and "
                 "prefer settings whose neighbors score similarly over a single standout row.")
    return "\n".join(lines)
