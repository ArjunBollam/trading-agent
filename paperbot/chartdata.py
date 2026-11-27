"""One chart for the Charts tab: its candles at the interval and in the chart type asked for, with the indicators,
QQE / S/R / squeeze / smart money drawings, volume profile, TPO and footprint over them, as of now or (for bar replay)
as of any earlier moment, using nothing that happened after it."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import charting as ch
from .config import ScanConfig
from .drawn import overlay_json
from .features import Bar
from .formula import combine, is_formula
from .orderflow import Tape, Trade
from .studies import compute
from .trendlines import HOLIDAYS, auto_lines, line_from_json, track

SYNTHETIC_ORIGIN = 946_684_800  # where charts that aren't drawn against time start their x axis (one step a minute)
TYPES = ("candles", "bars", "hollow", "line", "area", "baseline", "volume_candles", "heikin", "renko", "linebreak", "kagi", "pnf")
NO_TIME = ("renko", "linebreak", "kagi", "pnf")
FOOTPRINT_MINUTES = (1, 2, 3, 5, 10, 15, 30)


@dataclass
class ChartRequest:
    symbol: str
    tf: str = "5m"
    type: str = "candles"
    ext: bool = True  # pre-market and after hours too (always true for the 24-hour session)
    sessions: int | None = None  # how far back: sessions for intraday, years for D / W / M
    until: int | None = None  # for bar replay: as of this time (epoch seconds), nothing later
    studies: list[dict] = field(default_factory=list)
    box: float | None = None  # Renko / point & figure box, Kagi reversal; None: ATR(14) of the candles, rounded
    reversal: int = 3  # point & figure
    lines: int = 3  # line break
    high_low: bool = False  # point & figure on highs and lows instead of closes
    profile: bool = False  # session volume profile
    profile_rows: int = 24
    tpo: bool = False
    footprint: bool = False
    overlays: dict[str, bool] = field(default_factory=dict)  # qqe, sr, sqz, smc
    auto_lines: bool = False  # trend lines through 3+ swing points
    session: str = "ext"  # rth: 9:30-16:00 | ext: 4:00-20:00 | 24h: around the clock (Robinhood's overnight market too)

    @classmethod
    def from_json(cls, d: dict) -> ChartRequest:
        symbol = str(d.get("symbol") or "").strip().upper()
        if not symbol or len(symbol) > 80:
            raise ValueError("pick a symbol (or a formula such as SPY/QQQ)")
        kind = str(d.get("type") or "candles")
        if kind not in TYPES:
            raise ValueError(f"unknown chart type {kind!r}")

        def num(key, lo, hi, cast=float):
            v = d.get(key)
            if v in (None, ""):
                return None
            try:
                v = cast(v)
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a number") from None
            if not lo <= v <= hi:
                raise ValueError(f"{key} must be {lo:g} to {hi:g}")
            return v

        studies = d.get("studies") or []
        if not isinstance(studies, list):
            raise ValueError("studies must be a list")
        return cls(symbol, str(d.get("tf") or "5m"), kind, bool(d.get("ext", True)), num("sessions", 1, 3650, int),
                   num("until", 0, 4e9, int), studies, num("box", 1e-6, 1e6), num("reversal", 1, 10, int) or 3,
                   num("lines", 1, 10, int) or 3, bool(d.get("high_low")), bool(d.get("profile")), num("profile_rows", 4, 200, int) or 24,
                   bool(d.get("tpo")), bool(d.get("footprint")),
                   {k: bool((d.get("overlays") or {}).get(k)) for k in ("qqe", "sr", "sqz", "smc")}, bool(d.get("auto_lines")),
                   session(d))


def session(d: dict) -> str:
    s = d.get("session")
    if s in ("rth", "ext", "24h"):
        return s
    return "ext" if d.get("ext", True) else "rth"


def default_sessions(iv: ch.Interval) -> int:
    if iv.unit in "sTR":
        return 1
    if iv.unit == "m":
        n = iv.size
        return 3 if n <= 1 else 5 if n <= 5 else 15 if n <= 30 else 30 if n <= 60 else 90
    return {"D": 5, "W": 10, "M": 20}[iv.unit]  # years


def _cut(bars: list[Bar], until: datetime | None, span: timedelta) -> list[Bar]:
    """As of `until`: only candles finished by then (the one it's inside, for the chart's own interval, is still forming)."""
    return bars if until is None else [b for b in bars if b.t + span <= until]


def _prints(market, symbol: str, days: list[date], until: datetime | None, ext: bool, sessions: int) -> list[ch.Print]:
    """The trades of the last `sessions` days that had any (a holiday, or today before the open, is skipped), oldest first."""
    picked: list[list[ch.Print]] = []
    for d in reversed(days):
        if until is not None and d > until.date():
            continue
        got = [p for p in market.prints(symbol, d) if (until is None or p.t <= until) and (ext or ch.regular(p.t))]
        if got:
            picked.insert(0, got)
        if len(picked) == sessions:
            break
    return [p for day in picked for p in day]


def build(market, sc: ScanConfig, tz: ZoneInfo, req: ChartRequest, now: datetime, drawings: list[dict] | None = None) -> dict:
    iv = ch.parse_interval(req.tf)
    until = datetime.fromtimestamp(req.until, tz) if req.until else None
    sessions = req.sessions or default_sessions(iv)
    notes: list[str] = []
    formula = is_formula(req.symbol)
    if formula and (iv.needs_trades or req.type in NO_TIME):
        raise ValueError("formula charts are drawn from minute or daily candles: pick a time interval and candles")
    night = req.session == "24h"
    if night:
        req.ext = True
        if iv.needs_trades:
            raise ValueError("second, tick and range charts are built from IEX trades, which stop at 20:00: "
                             "pick extended hours for them, or a 5-minute (or longer) interval for the 24-hour session")
    iex_from: datetime | None = None
    minutes: dict[str, list[Bar]] = {}

    def minute_bars(sym: str) -> list[Bar]:
        nonlocal iex_from
        if sym not in minutes and night:
            step = 5 if iv.unit == "m" and iv.size % 5 == 0 else 1  # 1-minute candles for 1m, 2m, 3m...: two days at most
            days = min(sessions, 15) if step == 5 else min(req.sessions or 1, 2)  # 1-minute: one day unless more is asked for
            minutes[sym] = _cut(market.night(sym, days, step), until, timedelta(minutes=step))
        if sym not in minutes:
            got, frm = market.minutes(sym, min(sessions, 60) if iv.unit in "smTR" else 1)
            minutes[sym] = _cut(got, until, timedelta(minutes=1))
            if frm and (until is None or frm < until):
                iex_from = frm if iex_from is None else min(iex_from, frm)
        return minutes[sym]

    if iv.unit == "m":
        if formula:
            from .formula import parse

            _, symbols = parse(req.symbol)
            base = combine(req.symbol, {s: minute_bars(s) for s in symbols})
        else:
            base = minute_bars(req.symbol)
        bars = ch.resample_minutes(base, int(iv.size), req.ext, around_the_clock=night)
    elif iv.unit in "DWM":
        years = min(sessions, 20)
        if formula:
            from .formula import parse

            _, symbols = parse(req.symbol)
            base = combine(req.symbol, {s: _cut(market.daily(s, years), until, timedelta(days=1)) for s in symbols})
        else:
            base = _cut(market.daily(req.symbol, years), until, timedelta(days=1))
        bars = ch.resample_days(base, iv.unit, int(iv.size))
    else:
        days = trade_days(until or now, min(sessions, 5) + 4)
        prints = _prints(market, req.symbol, days, until, req.ext, min(sessions, 5))
        if not prints:
            notes.append("No IEX trades in the last few days for this symbol.")
        if iv.unit == "s":
            bars = ch.second_bars(prints, int(iv.size))
        elif iv.unit == "T":
            bars = ch.tick_bars(prints, int(iv.size))
        else:
            bars = ch.range_bars(prints, iv.size)
        notes.append("Built from IEX trades (one exchange, live on the free data plan): the prices are the market's, "
                     "the volume and number of trades are IEX's share only.")

    synthetic = not iv.timed or req.type in NO_TIME
    kind = req.type
    columns = None
    box = None
    if kind == "heikin":
        bars = ch.heikin_ashi(bars)
    elif kind == "renko":
        box = ch.box_size(bars, req.box)
        bars = ch.renko(bars, box)
    elif kind == "linebreak":
        bars = ch.line_break(bars, req.lines)
    elif kind == "kagi":
        box = ch.box_size(bars, req.box)
        cols = ch.kagi(bars, box)
        columns = [[SYNTHETIC_ORIGIN + i * 60, c.start, c.end, int(c.thick), c.switch, c.v, int(c.t.timestamp())] for i, c in enumerate(cols)]
        bars = [Bar(c.t, c.start, max(c.start, c.end), min(c.start, c.end), c.end, c.v) for c in cols]
    elif kind == "pnf":
        box = ch.box_size(bars, req.box)
        cols = ch.point_figure(bars, box, req.reversal, req.high_low)
        columns = [[SYNTHETIC_ORIGIN + i * 60, int(c.up), c.top, c.bottom, c.v, int(c.t.timestamp())] for i, c in enumerate(cols)]
        bars = [Bar(c.t, c.bottom if c.up else c.top, c.top, c.bottom, c.top if c.up else c.bottom, c.v) for c in cols]

    def stamp(i: int) -> int:
        return SYNTHETIC_ORIGIN + i * 60 if synthetic else int(bars[i].t.timestamp())

    intraday = iv.unit in "smTR"
    out: dict = {
        "symbol": req.symbol, "tf": iv.label, "type": kind, "synthetic": synthetic, "intraday": intraday,
        "seconds": int(iv.size) * (1 if iv.unit == "s" else 60) if iv.unit in "sm" else None,
        "bars": [[stamp(i), b.o, b.h, b.l, b.c, b.v, 0 if not intraday or ch.regular(b.t) else 1, int(b.t.timestamp())]
                 for i, b in enumerate(bars)],
        "columns": columns, "box": box,
        "iex_from": int(iex_from.timestamp()) if iex_from else None,
        "session": req.session,
        "source": ("IEX trades" if iv.needs_trades else "Alpaca daily candles (whole market)" if iv.unit in "DWM"
                   else f"Robinhood 24-hour market, {'5' if iv.unit == 'm' and iv.size % 5 == 0 else '1'}-minute candles "
                        "(overnight 20:00-4:00 included)" if night
                   else "Alpaca 1-minute candles (whole market" + (", newest minutes IEX only)" if iex_from else ")")),
        "until": req.until, "notes": notes, "studies": [], "overlays": {}, "profiles": [], "tpo": [], "footprint": {},
        "line_stages": {}, "auto_lines": [], "holidays": [h.isoformat() for h in HOLIDAYS],
    }
    if kind not in ("kagi", "pnf") and bars:
        if req.studies:
            out["studies"] = compute(bars, req.studies)
        if any(req.overlays.values()):
            rth = [i for i, b in enumerate(bars) if ch.regular(b.t)]
            if intraday_minutes(iv) and not synthetic and rth and len(rth) < len(bars):
                # like the dashboard (and an RTH chart): QQE, S/R breaks, squeeze and smart money from the regular-hours
                # candles only, so pre-market and overnight trading doesn't change them; labels go on those same candles
                out["overlays"] = overlay_json([bars[i] for i in rth], sc, lambda k: stamp(rth[k]), which=req.overlays,
                                               live=until is None)
                notes.append("QQE, S/R, squeeze and smart money are worked out from regular-hours candles, as on the dashboard.")
            else:
                out["overlays"] = overlay_json(bars, sc, stamp, which=req.overlays, live=until is None)
    elif req.studies or any(req.overlays.values()):
        notes.append("Indicators and signal labels aren't drawn on Kagi or point & figure charts: they have no candles.")
    timed_intraday = iv.unit in "sm" and not synthetic
    if (req.profile or req.tpo) and timed_intraday and not formula:
        sessions_1m = ch.by_session(minute_bars(req.symbol))
        first_x: dict[date, int] = {}
        last_x: dict[date, int] = {}
        rth_x: dict[date, tuple[int, int]] = {}  # the TPO is regular hours only: from the session's 9:30 candle to its last
        for i, b in enumerate(bars):
            first_x.setdefault(b.t.date(), stamp(i))
            last_x[b.t.date()] = stamp(i)
            if ch.regular(b.t):
                rth_x[b.t.date()] = (rth_x.get(b.t.date(), (stamp(i),))[0], stamp(i))
        for d, rows in sorted(sessions_1m.items()):
            if d not in first_x:
                continue
            if req.profile:
                vp = ch.volume_profile([b for b in rows if req.ext or ch.regular(b.t)], req.profile_rows)
                if vp:
                    out["profiles"].append({"from": first_x[d], "to": last_x[d], "step": vp.step, "poc": vp.poc, "vah": vp.vah,
                                            "val": vp.val, "rows": [[round(lo, 6), round(u), round(dn)] for lo, u, dn in vp.rows]})
            if req.tpo:
                t = ch.tpo(rows)
                if t and d in rth_x:
                    out["tpo"].append({"from": rth_x[d][0], "to": rth_x[d][1], "step": t.step, "poc": t.poc, "vah": t.vah, "val": t.val,
                                       "ib": [t.ib_low, t.ib_high], "open": t.open, "close": t.close, "singles": t.singles,
                                       "rows": [[round(lo, 6), s] for lo, s in t.rows]})
    elif req.profile or req.tpo:
        notes.append("The volume profile and TPO are drawn on minute and second charts of one symbol.")
    if not synthetic and bars and (drawings or req.auto_lines):
        lines_on(out, bars, stamp, iv, req, drawings or [], tz, until or now)
    if req.footprint:
        n = int(iv.size) if iv.unit == "m" else 0
        if n in FOOTPRINT_MINUTES and not formula and not synthetic:
            out["footprint"] = footprint(market, req.symbol, tz, n, sorted({b.t.date() for b in bars})[-2:], until, stamp, bars)
            notes.append("Footprint: regular hours, from IEX trades, each trade signed buy or sell by the tick rule.")
        else:
            notes.append(f"The footprint is drawn on {', '.join(f'{m}m' for m in FOOTPRINT_MINUTES)} candle charts of one symbol.")
    return out


def lines_on(out: dict, bars: list[Bar], stamp, iv: ch.Interval, req: ChartRequest, drawings: list[dict], tz: ZoneInfo,
             now: datetime) -> None:
    """Each drawn line's break and retest on this chart's closed candles, and (if asked) the lines through swing points."""
    span = timedelta(seconds=int(iv.size)) if iv.unit == "s" else timedelta(minutes=int(iv.size)) if iv.unit == "m" else None
    if span is not None:
        closed = [b for b in bars if b.t + span <= now]
    else:  # days, weeks, months: today's candle is closed once the session is over
        closed = [b for b in bars if b.t.date() < now.date() or now.hour >= 16]
    for d in drawings:
        try:
            line = line_from_json(d, tz)
        except ValueError:
            continue
        out["line_stages"][line.id] = [{"stage": x.stage, "x": stamp(x.index), "side": x.side, "line": round(x.line, 4), "text": x.text}
                                       for x in track(line, closed)]
    if req.auto_lines:
        clock = "24h" if req.session == "24h" else "ext" if req.ext or iv.unit in "DWM" else "rth"
        for a in auto_lines(closed, clock):
            out["auto_lines"].append({"side": a.side, "t1": int(bars[a.i1].t.timestamp()), "p1": round(a.p1, 4),
                                      "t2": int(bars[a.i2].t.timestamp()), "p2": round(a.p2, 4), "touches": a.touches,
                                      "end": stamp(a.end), "broken": a.broken, "clock": clock})


def intraday_minutes(iv: ch.Interval) -> bool:
    """Seconds and minute candles up to 30 minutes: each lies inside one part of the day (pre-market, regular or after
    hours) when counted from 4:00 or midnight, so the regular-hours ones can be picked out."""
    return iv.unit == "s" or (iv.unit == "m" and iv.size <= 30 and 30 % iv.size == 0)


def trade_days(now: datetime, n: int) -> list[date]:
    from .market import weekdays

    return weekdays(now.date(), n)


def footprint(market, symbol: str, tz: ZoneInfo, minutes: int, days: list[date], until: datetime | None, stamp, bars: list[Bar]) -> dict:
    """Each candle's volume at every price, sold at the bid and bought at the offer, with its delta and busiest price."""
    at = {b.t: stamp(i) for i, b in enumerate(bars)}
    out: dict[str, dict] = {}
    for d in days:
        tape = Tape(d, minutes, tz)
        tape.add([Trade(p.t, p.price, p.size) for p in market.prints(symbol, d) if until is None or p.t <= until])
        for fb in tape.bars:
            x = at.get(fb.t)
            if x is None or not fb.rows:
                continue
            rows = sorted(fb.rows.items())
            poc = max(rows, key=lambda r: r[1][0] + r[1][1])[0]
            out[str(x)] = {"step": fb.step, "delta": round(fb.delta), "volume": round(fb.volume), "unsigned": round(fb.other),
                           "poc": round(poc * fb.step, 6),
                           "rows": [[round(k * fb.step, 6), round(s), round(b)] for k, (s, b) in rows]}
    return out


def csv_for(result: dict, bars_tz: ZoneInfo) -> str:
    """The chart's candles and its indicators' lines as CSV."""
    rows = [Bar(datetime.fromtimestamp(b[7], bars_tz), b[1], b[2], b[3], b[4], b[5]) for b in result["bars"]]
    extra = {}
    for s in result["studies"]:
        for o in s["outputs"]:
            extra[(s['name'] if len(s['outputs']) == 1 else f"{s['name']} {o['name']}").replace(",", " ")] = o["values"]
    return ch.to_csv(rows, extra)

