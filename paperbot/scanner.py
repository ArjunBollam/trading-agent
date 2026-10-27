"""`paperbot scan`: chart signals on your Robinhood positions and watchlist.

Every scan takes one Robinhood snapshot (positions, intraday bars, and daily bars once a day),
runs patterns.analyze on each symbol's completed bars, and alerts each new signal. A signal on a
symbol you hold says whether it agrees with your position or goes against it.

Information only: nothing here places, changes or cancels an order, on Robinhood or Alpaca.
"""

from __future__ import annotations

import json
import os
import time as time_
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .config import Config, ScanConfig
from .context import Context, assess
from .features import Bar, atr
from .journal import Journal
from .notify import Alerts
from .patterns import ChartSignal, Level, Summary, analyze, levels
from .robinhood import Holding, MarketSource, OptionHolding, mask

MARKET_OPEN, MARKET_CLOSE = time(9, 30), time(16, 0)


def _against(direction: int, side: str) -> bool:
    """Whether a buy or sell signal goes against a position that gains when price moves `direction`."""
    return direction * (1 if side == "buy" else -1) < 0


def money(x: float) -> str:
    return f"{'-' if x < 0 else '+'}${abs(x):,.0f}"


def option_pnl(o: OptionHolding, mark: float) -> tuple[float, float] | None:
    """($, %) on an option position at `mark`, or None without a cost."""
    if not o.avg_price:
        return None
    return (mark - o.avg_price) * o.qty * o.multiplier, (mark - o.avg_price) / o.avg_price * 100 * (1 if o.qty > 0 else -1)


def position_notes(side: str, price: float, holding: Holding | None, options: tuple[OptionHolding, ...],
                   noun: str = "signal", marks: dict[str, float] | None = None) -> str:
    """" You're short 42 (avg 216.36, +0.2%): this signal goes with your position." and one sentence per
    option position, with its live mark and P&L when `marks` has it."""
    out = ""
    h = holding
    if h is not None:
        pos = f"You're {'long' if h.qty > 0 else 'short'} {abs(h.qty):g}"
        if h.avg_cost:
            pnl = (price - h.avg_cost) / h.avg_cost * 100 * (1 if h.qty > 0 else -1)
            pos += f" (avg {h.avg_cost:.2f}, {pnl:+.1f}%)"
        out += f" {pos}: this {noun} goes {'against' if _against(1 if h.qty > 0 else -1, side) else 'with'} your position."
    for o in options:
        mark = (marks or {}).get(o.option_id)
        pnl = option_pnl(o, mark) if mark is not None else None
        if pnl is not None:
            cost = f" (mark {mark:.2f}, avg {o.avg_price:.2f}, {money(pnl[0])}, {pnl[1]:+.0f}%)"
        else:
            cost = f" (avg {o.avg_price:.2f})" if o.avg_price else ""
        out += f" You hold {o.label}{cost}: this {noun} goes {'against' if _against(o.direction, side) else 'with'} it."
    return out


@dataclass(frozen=True)
class Found:
    signal: ChartSignal
    holding: Holding | None
    options: tuple[OptionHolding, ...] = ()  # option positions on this symbol
    context: Context | None = None  # the bigger picture on the bar the signal came from

    @property
    def conflict(self) -> str | None:
        """Why this signal goes against the bigger picture, or None."""
        return self.context.conflict(self.signal.side) if self.context is not None else None

    @property
    def against(self) -> bool:
        h, side = self.holding, self.signal.side
        return ((h is not None and _against(1 if h.qty > 0 else -1, side))
                or any(_against(o.direction, side) for o in self.options))

    def text(self) -> str:
        s = self.signal
        line = f"{s.bar_time:%H:%M} {s.label}{' (weak)' if s.weak else ''} at {s.price:.2f}: {s.detail}."
        warn = f" WARNING: {c}" if (c := self.conflict) else ""
        return line + position_notes(s.side, s.price, self.holding, self.options) + warn


@dataclass
class Watch:
    """What the live check compares the price with between bar closes, from the last completed bar."""
    levels: list[Level]
    buffer: float  # a cross must clear the level by this much, so quote flicker doesn't count
    sides: dict[str, int]  # per level: +1 above, -1 below (by more than the buffer), 0 at it
    last: float


def watch_levels(today: list[Bar], prev_session: list[Bar], bar_minutes: int, vwap: float | None) -> list[Level]:
    """The scan's support and resistance levels, with high and low of day over every completed bar."""
    sup, res = levels(today, prev_session, bar_minutes)
    out = {lv.name: lv.price for lv in sup + res if lv.name not in ("high of day", "low of day")}
    out["high of day"], out["low of day"] = max(b.h for b in today), min(b.l for b in today)
    if vwap:
        out["VWAP"] = vwap
    return [Level(n, p) for n, p in out.items()]


def _side(price: float, level: float, buffer: float) -> int:
    return 1 if price > level + buffer else -1 if price < level - buffer else 0


@dataclass
class ScanResult:
    at: datetime
    holdings: list[Holding]
    summaries: list[Summary]
    found: list[Found]  # new signals only (after the cooldown)
    options: list[OptionHolding] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # held, but no bars came back
    contexts: dict[str, Context] = field(default_factory=dict)  # the bigger picture per symbol, on the last bar


class Scanner:
    def __init__(self, source: MarketSource, cfg: Config, account: str, journal: Journal, alerts: Alerts,
                 now: Callable[[], datetime], only: set[str] | None = None, state_path: Path | None = None):
        self.source = source
        self.cfg = cfg
        self.account = account
        self.journal = journal
        self.alerts = alerts
        self.now = now
        self.only = only  # scan just these symbols (positions are still read, for with/against)
        self.daily: tuple[date, dict] | None = None
        self.last_fired: dict[tuple, datetime] = {}
        self._seeded_for: date | None = None
        self.watch: dict[str, Watch] = {}  # for tick(), refreshed by every scan
        self.live_fired: dict[tuple, datetime] = {}
        self.positions: tuple[datetime, list[Holding], list[OptionHolding]] | None = None  # for tick(), with their time
        self.state_path = state_path  # where the web page reads the latest prices, candles, levels and positions
        self.view: dict[str, dict] = {}
        self.prices: dict[str, float] = {}
        self.marks: dict[str, float] = {}
        self.forming: dict[str, list] = {}  # symbol -> [bar start, open, high, low, close] from live prices
        self.no_data: list[str] = []  # symbols the last scan got no bars for

    def _seed(self, day: date) -> None:
        """Remember today's alerted signals, including ones another process sent (a restart, the
        scheduled job, a live scan running beside it), so none is sent twice. Read on every scan."""
        if self._seeded_for != day:
            self._seeded_for, self.last_fired = day, {}
        for e in self.journal.events(day, "chart_signal"):
            key = (e["symbol"], e.get("family", e["kind"]), e.get("level"))
            t = datetime.fromisoformat(e["bar_time"])
            if key not in self.last_fired or t > self.last_fired[key]:
                self.last_fired[key] = t

    def scan(self, send_alerts: bool = True, catch_up: bool = False) -> ScanResult:
        """Signals on the last completed bar, or with catch_up on every completed bar since the open
        (for scans hours apart). Only signals not already alerted today are returned and sent.
        A print-only scan (send_alerts=False) doesn't record its signals, so it can't hide them
        from a later alerting scan."""
        sc, tz = self.cfg.scan, self.cfg.schedule.tz
        now = self.now().astimezone(tz)
        day = now.date()
        need_daily = (self.daily is None or self.daily[0] != day
                      or bool(self.only and not self.only <= set(self.daily[1])))  # a symbol added since
        symbols = list(dict.fromkeys(sc.watchlist + sorted(self.only or ())))
        snap = self.source.snapshot(self.account, symbols, sc.bar_minutes, now - timedelta(days=6),
                                    now - timedelta(days=400) if need_daily else None)
        self.positions = (now, snap.holdings, snap.options)
        self._seed(day)  # after the fetch, so a scan that finished meanwhile is seen
        if need_daily:
            self.daily = (day, snap.daily)
        daily = self.daily[1] if self.daily else {}
        held = {h.symbol: h for h in snap.holdings}
        held_options: dict[str, tuple[OptionHolding, ...]] = {}
        for o in snap.options:
            held_options[o.symbol] = held_options.get(o.symbol, ()) + (o,)
        step = timedelta(minutes=sc.bar_minutes)
        summaries, found, contexts = [], [], {}
        for sym, rows in snap.bars.items():
            if self.only and sym not in self.only:
                continue
            bars = [b.__class__(b.t.astimezone(tz), b.o, b.h, b.l, b.c, b.v) for b in rows if b.t + step <= now]
            sessions = sorted({b.t.date() for b in bars})[-3:]  # today plus two sessions of indicator warm-up
            bars = [b for b in bars if b.t.date() in sessions]
            day_bars = [b.__class__(b.t.astimezone(tz), b.o, b.h, b.l, b.c, b.v) for b in daily.get(sym, [])]
            ends = [i + 1 for i, b in enumerate(bars) if b.t.date() == day] if catch_up else []
            signals: list[tuple[ChartSignal, Context]] = []
            for end in ends[:-1]:  # oldest first, so the cooldown sees signals in order
                if older := analyze(sym, bars[:end], day, sc, day_bars)[0]:
                    ctx_then = assess(bars[:end], day, sc)
                    signals += [(sig, ctx_then) for sig in older]
            last_signals, summary = analyze(sym, bars, day, sc, day_bars)
            ctx = contexts[sym] = assess(bars, day, sc)
            signals += [(sig, ctx) for sig in last_signals]
            summaries.append(summary)
            today_bars = [b for b in bars if b.t.date() == day]
            if summary.price is not None:
                self.prices[sym] = summary.price
            self.view[sym] = {"rsi": summary.rsi, "vwap": summary.vwap, "trend": summary.trend,
                              "bar_time": summary.bar_time.isoformat() if summary.bar_time else None,
                              "bars": [[int(b.t.timestamp()), b.o, b.h, b.l, b.c, b.v] for b in today_bars], "levels": [],
                              "context": {"bias": ctx.bias, "score": ctx.score,
                                          "factors": [{"side": f.side, "weight": f.weight, "text": f.text} for f in ctx.factors]}}
            if today_bars:
                prev_day = [b for b in bars if b.t.date() == max((d for d in sessions if d < day), default=None)]
                lv = watch_levels(today_bars, prev_day, sc.bar_minutes, summary.vwap)
                buf = sc.min_cross_atr * (atr(bars[-30:]) or 0.0)
                close = today_bars[-1].c
                self.watch[sym] = Watch(lv, buf, {x.name: _side(close, x.price, buf) for x in lv}, close)
                self.view[sym]["levels"] = [{"name": x.name, "price": round(x.price, 4)} for x in lv]
            new = []
            for sig, sig_ctx in signals:
                last = self.last_fired.get(sig.key)
                if last is not None and sig.bar_time - last < timedelta(minutes=max(sc.cooldown_minutes, 1)):
                    continue
                self.last_fired[sig.key] = sig.bar_time
                f = Found(sig, held.get(sym), held_options.get(sym, ()), sig_ctx)
                new.append(f)
                if not send_alerts:
                    continue
                self.journal.log("chart_signal", symbol=sym, kind=sig.kind, family=sig.family, side=sig.side,
                                 level=sig.level, price=sig.price, bar_time=sig.bar_time.isoformat(),
                                 detail=sig.detail, held=f.holding is not None or bool(f.options),
                                 against_position=f.against, weak=sig.weak, context=sig_ctx.bias,
                                 context_score=sig_ctx.score, conflict=f.conflict)
            found += new
            loud = [f for f in new if (sc.alert_weak or not f.signal.weak)
                    and not (sc.context_mode == "suppress" and f.conflict)]
            if send_alerts and loud:
                groups = [loud] if sc.one_alert_per_symbol else [[f] for f in loud]
                for g in groups:
                    self._alert(sym, g)
        missing = sorted(s for s in set(held) | set(held_options) | set(self.only or ())
                         if s not in snap.bars and (not self.only or s in self.only))
        self.no_data = missing  # e.g. a mistyped ticker: the page says so instead of waiting
        shown = [o for o in snap.options if not self.only or o.symbol in self.only]
        self.publish(now)
        return ScanResult(now, snap.holdings, summaries, found, shown, missing, contexts)

    def tick(self, send_alerts: bool = True) -> str:
        """Between bar closes: the live price against the last completed bar's levels. A cross is
        alerted at once, marked as inside the forming bar (the bar's close confirms it or not); every
        tick returns a status line with the live P&L on what you hold, for the log, not the phone."""
        sc, tz = self.cfg.scan, self.cfg.schedule.tz
        now = self.now().astimezone(tz)
        syms = [s for s in self.watch if not self.only or s in self.only]
        if not syms:
            return f"{now:%H:%M:%S} waiting for the first scan"
        cached = self.positions
        fresh = cached is not None and (now - cached[0]).total_seconds() < sc.live_positions_seconds
        live = self.source.live(self.account, syms, (cached[1], cached[2]) if fresh else None)
        if not fresh:
            self.positions = (now, live.holdings, live.options)
        self.prices.update(live.prices)
        self.marks.update(live.marks)
        bar = now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % sc.bar_minutes)
        lines = []
        for sym in syms:
            w, px = self.watch[sym], live.prices.get(sym)
            if px is None:
                lines.append(f"{now:%H:%M:%S} {sym} no live price")
                continue
            held = next((h for h in live.holdings if h.symbol == sym), None)
            opts = tuple(o for o in live.options if o.symbol == sym)
            crossed = []
            for lv in w.levels:
                side = _side(px, lv.price, w.buffer)
                was = w.sides.get(lv.name, 0)
                if side:
                    w.sides[lv.name] = side
                if side and was and side != was:
                    key = (sym, lv.name, side)
                    last = self.live_fired.get(key)
                    if last is None or now - last >= timedelta(minutes=sc.tick_cooldown_minutes):
                        self.live_fired[key] = now
                        crossed.append((lv, "buy" if side > 0 else "sell"))
            w.last = px
            self._form(sym, bar, px)
            vw = next((lv.price for lv in w.levels if lv.name == "VWAP"), None)
            status = f"{now:%H:%M:%S} {sym} {px:.2f}" + (f" ({(px - vw) / vw * 100:+.2f}% vs VWAP)" if vw else "")
            for o in opts:
                m = live.marks.get(o.option_id)
                pnl = option_pnl(o, m) if m is not None else None
                status += f" | {o.label}: mark {m:.2f}" if m is not None else f" | {o.label}: no quote"
                if pnl is not None:
                    status += f", {money(pnl[0])} ({pnl[1]:+.0f}%)"
            if held is not None and held.avg_cost:
                status += f" | {'long' if held.qty > 0 else 'short'} {abs(held.qty):g}: {money((px - held.avg_cost) * held.qty)}"
            lines.append(status)
            if not crossed:
                continue
            texts, against = [], False
            for lv, side in crossed:
                texts.append(f"{now:%H:%M:%S} Live: {sym} {px:.2f} crossed {'above' if side == 'buy' else 'below'} the "
                             f"{lv.name} {lv.price:.2f} inside the {bar:%H:%M} candle; not confirmed until it closes at "
                             f"{bar + timedelta(minutes=sc.bar_minutes):%H:%M}.")
                hit = (held is not None and _against(1 if held.qty > 0 else -1, side)) or any(
                    _against(o.direction, side) for o in opts)
                against |= hit
                if send_alerts:
                    self.journal.log("live_cross", symbol=sym, level=lv.name, level_price=lv.price, price=px,
                                     side=side, against_position=hit)
            side = crossed[-1][1]  # both ways in one tick can't happen: the price is on one side of each level
            text = "\n".join(texts) + position_notes(side, px, held, opts, "move", live.marks)
            lines.append(text)
            if send_alerts:
                names = " & ".join(lv.name for lv, _ in crossed)
                self.alerts.send(f"LIVE {'above' if side == 'buy' else 'below'} {names}: {sym}", text,
                                 "high" if against else "default",
                                 ["zap", "chart_with_upwards_trend" if side == "buy" else "chart_with_downwards_trend"])
        self.publish(now)
        return "\n".join(lines)

    def _form(self, sym: str, bar: datetime, px: float) -> None:
        """The candle still forming, from the live prices seen so far, for the page to draw after the
        completed ones. Built from checks seconds apart, so its high and low can miss a spike between them."""
        f = self.forming.get(sym)
        if f is None or f[0] != bar:
            self.forming[sym] = [bar, px, px, px, px]
        else:
            f[2], f[3], f[4] = max(f[2], px), min(f[3], px), px

    def _forming_for(self, sym: str, view: dict) -> list | None:
        f = self.forming.get(sym)
        last = view["bars"][-1][0] if view.get("bars") else 0
        return [int(f[0].timestamp()), *f[1:]] if f and int(f[0].timestamp()) > last else None

    def publish(self, now: datetime) -> None:
        """Write the latest state for the web page (atomically; a failed write never stops the scan)."""
        if self.state_path is None:
            return
        sc = self.cfg.scan
        holdings, options = (self.positions[1], self.positions[2]) if self.positions else ([], [])
        rows = []
        for h in holdings:
            px = self.prices.get(h.symbol)
            pnl = (px - h.avg_cost) * h.qty if px is not None and h.avg_cost else None
            rows.append({"symbol": h.symbol, "qty": h.qty, "avg": h.avg_cost, "price": px, "pnl": pnl,
                         "pnl_pct": pnl / abs(h.avg_cost * h.qty) * 100 if pnl is not None else None})
        opts = []
        for o in options:
            m = self.marks.get(o.option_id)
            pnl = option_pnl(o, m) if m is not None else None
            opts.append({"symbol": o.symbol, "label": o.label, "right": o.right, "strike": o.strike,
                         "expiration": o.expiration.isoformat(), "qty": o.qty, "avg": o.avg_price, "mark": m,
                         "pnl": pnl[0] if pnl else None, "pnl_pct": pnl[1] if pnl else None})
        symbols = {s: v | {"price": self.prices.get(s), "forming": self._forming_for(s, v)}
                   for s, v in self.view.items() if not self.only or s in self.only}
        state = {"updated": now.isoformat(), "account": mask(self.account), "bar_minutes": sc.bar_minutes,
                 "tick_seconds": sc.tick_seconds, "only": sorted(self.only) if self.only else None,
                 "symbols": symbols, "no_data": self.no_data, "holdings": rows, "options": opts}
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state))
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    def _alert(self, sym: str, group: list[Found]) -> None:
        sides = {f.signal.side for f in group}
        side = sides.pop().upper() if len(sides) == 1 else "MIXED"
        title = f"{side} signal{'s' if len(group) > 1 else ''}: {sym}"
        if any(f.conflict for f in group):
            title = f"{side} against the bigger picture: {sym}"
        against = any(f.against or f.conflict for f in group)
        tag = {"BUY": "chart_with_upwards_trend", "SELL": "chart_with_downwards_trend"}.get(side, "left_right_arrow")
        self.alerts.send(title, alert_text(group), "high" if against else "default", [tag])


def alert_text(group: list[Found], limit: int = 3800) -> str:
    """Every signal, oldest first, but within ntfy's 4 KB message limit (past it, ntfy sends a file
    attachment instead): when they don't fit, the oldest are dropped and counted."""
    lines = [f.text() for f in group]
    kept: list[str] = []
    for line in reversed(lines):
        if len("\n".join([line, *kept]).encode()) > limit - 80:
            break
        kept.insert(0, line)
    if len(kept) < len(lines):
        kept.insert(0, f"(+{len(lines) - len(kept)} earlier signals; run `paperbot scan --catch-up --no-alerts` for all)")
    return "\n".join(kept)


def market_hours(now: datetime) -> bool:
    return now.weekday() < 5 and MARKET_OPEN <= now.time() < MARKET_CLOSE


def format_result(r: ScanResult) -> str:
    held = {h.symbol: h for h in r.holdings}
    optioned = {o.symbol for o in r.options}
    lines = [f"Scan at {r.at:%Y-%m-%d %H:%M} ET. Technical signals only, not advice; nothing is traded.", ""]
    lines.append(f"{'symbol':8}{'position':>16}{'last':>10}{'P&L %':>8}{'RSI':>7}{'vs VWAP':>9}  trend")
    for s in sorted(r.summaries, key=lambda s: (s.symbol not in held and s.symbol not in optioned, s.symbol)):
        h = held.get(s.symbol)
        pos = f"{'long' if h.qty > 0 else 'short'} {abs(h.qty):g}" if h else "options" if s.symbol in optioned else "watch"
        pnl = ""
        if h and h.avg_cost and s.price:
            pnl = f"{(s.price - h.avg_cost) / h.avg_cost * 100 * (1 if h.qty > 0 else -1):+.1f}"

        def fmt(x, spec):
            return "-" if x is None else format(x, spec)

        lines.append(f"{s.symbol:8}{pos:>16}{fmt(s.price, '.2f'):>10}{pnl or '-':>8}{fmt(s.rsi, '.1f'):>7}"
                     f"{fmt(s.vs_vwap_pct, '+.2f'):>9}  {s.trend or '-'}")
    for sym in r.missing:
        lines.append(f"{sym:8}  (no bars returned: is the ticker right?)")
    for o in r.options:
        lines.append(f"  option: {o.label}" + (f", avg {o.avg_price:.2f}" if o.avg_price else ""))
    for sym, ctx in sorted(r.contexts.items()):
        lines.append(f"  bigger picture {sym}: {ctx.line()}")
    lines.append("")
    if r.found:
        for f in r.found:
            lines.append(f"{f.signal.side.upper():4} {f.signal.symbol:6} {f.text()}")
    else:
        lines.append("No new signals on the last completed bar.")
    return "\n".join(lines)


def scan_forever(scanner: Scanner, send_alerts: bool = True, print_fn: Callable[[str], None] = print,
                 sleep: Callable[[float], None] = time_.sleep, symbols: Callable[[], list[str]] | None = None,
                 settings: Callable[[], ScanConfig] | None = None) -> None:
    """Scan just after each bar closes during market hours and, with scan.tick_seconds, check the live
    price that often in between (Scanner.tick). `symbols` (a LiveList's) and `settings` (Overrides')
    are read before every check: a change applies at once. Ctrl-C to stop."""
    closed_noted, scanned_bar = False, None
    while True:
        now = scanner.now().astimezone(scanner.cfg.schedule.tz)
        if settings is not None and (new := settings()) is not scanner.cfg.scan:
            scanner.cfg.scan, scanned_bar = new, None
            print_fn(f"{now:%H:%M:%S} settings changed")
        minutes, tick = scanner.cfg.scan.bar_minutes, scanner.cfg.scan.tick_seconds
        bar = now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % minutes)  # the forming bar
        if symbols is not None:
            want = set(symbols()) or None
            if want != scanner.only:
                scanner.only, scanned_bar = want, None  # scan the new list now, not at the next bar
                print_fn(f"{now:%H:%M:%S} live symbols: {', '.join(sorted(want)) if want else 'everything held'}")
        if market_hours(now):
            closed_noted = False
            try:
                if bar != scanned_bar:  # a bar has closed since the last scan
                    print_fn(format_result(scanner.scan(send_alerts)))
                    scanned_bar = bar
                elif tick:
                    print_fn(scanner.tick(send_alerts))
            except Exception as e:  # a network or sign-in hiccup shouldn't end a day of scanning
                scanner.journal.log("scan_failed", error=repr(e))
                print_fn(f"scan failed: {e!r}")
        elif not closed_noted:
            print_fn(f"{now:%H:%M} ET: market closed; waiting for the open.")
            closed_noted = True
        now = scanner.now().astimezone(scanner.cfg.schedule.tz)
        bar = now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % minutes)
        nxt = bar + timedelta(minutes=minutes)
        if tick and market_hours(now):
            elapsed = (now - bar).total_seconds()
            nxt = min(nxt, bar + timedelta(seconds=(elapsed // tick + 1) * tick))
        sleep(max(1.0, (nxt - now).total_seconds() + 5))  # a few seconds for the bar to be published
