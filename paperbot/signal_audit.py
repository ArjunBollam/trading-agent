"""`paperbot audit`: a monitor that checks every signal the scanner and the charts show, while the market runs.

It is a separate process and only reads. For each signal (the scanner's own, its live level crosses, and the QQE, S/R,
Squeeze and Smart Money labels on the 5-minute and the 1-minute charts) it answers three questions:

1. Is it right? Worked out again from freshly downloaded candles, cut at the signal's candle so nothing later can leak in,
   and checked from its textbook definition (claims.py) with none of the code that made it. The other way round too: a
   label the candles call for that the page doesn't show is a missed signal, and one that disappears is reported.
2. Does a second source agree? Once 16 minutes have passed, again on Alpaca's full-market candles.
3. What happened next? How far price went its way after 15 and 30 minutes, beside what any candle that day did.

Being right is not the same as paying off: question 1 is about the calculation, question 3 about the market.
Results go to log_dir/audit/<day>.json for the web page's Audit tab, and a new problem goes to your phone."""

from __future__ import annotations

import json
import os
import time as time_
from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path

from . import claims
from .config import Config, ScanConfig
from .features import Bar, regular
from .journal import Journal
from .overlays import level_breaks, squeeze
from .patterns import LABELS, analyze
from .qqe import qqe
from .scanner import scan_hours, weekdays_back
from .smc import INTERNAL, smart_money

HORIZONS = (15, 30)  # minutes after a signal at which to see what price did
FLAT_PCT = 0.02  # a move smaller than this is no move
SECOND_AFTER = timedelta(minutes=16)  # Alpaca's free plan has the full market 15 minutes late
ALERTS_PER_DAY = 10
BAD = ("wrong", "missed", "vanished")
SOURCES = {"scan": "Scanner", "live": "Live cross", "qqe": "QQE", "sr": "S/R", "sqz": "Squeeze", "smc": "Smart Money"}


class AlpacaBars:
    """Candles from every US exchange (SIP), at least 15 minutes old."""

    def __init__(self, api_key: str, secret_key: str):
        from alpaca.data.historical.stock import StockHistoricalDataClient

        if not api_key or not secret_key:
            raise ValueError("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set")
        self._client = StockHistoricalDataClient(api_key, secret_key, raw_data=True)

    def bars(self, symbols: list[str], minutes: int, start: datetime, end: datetime, adjustment: str = "raw") -> dict[str, list[Bar]]:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        if start >= end:
            return {}
        data = self._client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbols, timeframe=TimeFrame(minutes, TimeFrameUnit.Minute), start=start, end=end, feed=DataFeed.SIP,
            adjustment=Adjustment(adjustment)))
        return {s: [Bar(datetime.fromisoformat(r["t"].replace("Z", "+00:00")).astimezone(start.tzinfo), float(r["o"]), float(r["h"]),
                        float(r["l"]), float(r["c"]), float(r["v"])) for r in rows]
                for s, rows in (data if isinstance(data, dict) else {}).items()}


class Candles:
    """One source's candles: earlier sessions once a day, today's on every look."""

    def __init__(self, fetch: Callable[[list[str], int, datetime, bool], dict[str, list[Bar]]]):
        self.fetch = fetch
        self.old: dict[tuple, dict[str, list[Bar]]] = {}
        self.today: dict[tuple, dict[str, list[Bar]]] = {}

    def update(self, symbols: list[str], minutes: int, sessions: int, extended: bool, now: datetime) -> None:
        midnight = datetime.combine(now.date(), time(0), tzinfo=now.tzinfo)
        key = (minutes, sessions, extended)
        old = self.old.setdefault((now.date(), *key), {})
        new = [s for s in symbols if s not in old]
        if new:
            got = self.fetch(new, minutes, weekdays_back(now, sessions), extended)
            for s in new:
                old[s] = [b for b in self._clean(got.get(s, []), now, extended, minutes) if b.t < midnight]
                self.today.setdefault(key, {})[s] = [b for b in self._clean(got.get(s, []), now, extended, minutes) if b.t >= midnight]
        rest = [s for s in symbols if s not in new]
        if rest:
            got = self.fetch(rest, minutes, midnight, extended)
            for s in rest:
                rows = [b for b in self._clean(got.get(s, []), now, extended, minutes) if b.t >= midnight]
                if len(rows) >= len(self.today.get(key, {}).get(s, [])) or not rows or rows[0].t.date() != now.date():
                    self.today.setdefault(key, {})[s] = rows  # an answer cut short doesn't replace a fuller one
        self.old = {k: v for k, v in self.old.items() if k[0] == now.date()}

    @staticmethod
    def _clean(rows: list[Bar], now: datetime, extended: bool, minutes: int) -> list[Bar]:
        out = [Bar(b.t.astimezone(now.tzinfo), b.o, b.h, b.l, b.c, b.v) for b in rows]
        return [b for b in out if b.t + timedelta(minutes=minutes) <= now and (extended or regular(b))]

    def get(self, symbol: str, minutes: int, sessions: int, extended: bool, day: date) -> list[Bar]:
        key = (minutes, sessions, extended)
        today = [b for b in self.today.get(key, {}).get(symbol, []) if b.t.date() == day]
        return self.old.get((day, *key), {}).get(symbol, []) + today


def last_sessions(bars: list[Bar], n: int = 3) -> list[Bar]:
    keep = set(sorted({b.t.date() for b in bars})[-n:])
    return [b for b in bars if b.t.date() in keep]


def labels_on(bars: list[Bar], sc: ScanConfig) -> dict[str, dict[tuple, dict]]:
    """Every label the charts' own code puts on these candles: {indicator: {(start, side, kind): facts}}."""
    out: dict[str, dict[tuple, dict]] = {k: {} for k in ("qqe", "sr", "sqz", "smc")}

    def t(i: int) -> int:
        return int(bars[i].t.timestamp())

    for s in qqe([b.c for b in bars], sc.qqe_rsi_period, sc.qqe_smoothing, sc.qqe_factor):
        out["qqe"][(t(s.index), s.side, "")] = {}
    for b in level_breaks(bars, sc.sr_pivot_bars, sc.sr_pivot_bars, sc.sr_volume):
        out["sr"][(t(b.index), b.side, b.kind)] = {"level": b.level}
    for r in squeeze(bars, sc.squeeze_length, sc.squeeze_bb_mult, sc.squeeze_kc_mult)[0]:
        out["sqz"][(t(r.index), r.side, "")] = {}
    for x in smart_money(bars, sc.smc_swing).structures:
        out["smc"][(t(x.index), x.side, f"{'internal' if x.internal else 'swing'} {x.kind}")] = {
            "level": x.level, "pivot": x.pivot, "index": x.index, "internal": x.internal}
    return out


def long_way(bars: list[Bar], sc: ScanConfig) -> dict[str, set[tuple]]:
    """The same labels from claims.py's separate workings (Smart Money's are checked one by one instead)."""
    def t(i: int) -> int:
        return int(bars[i].t.timestamp())

    return {"qqe": {(t(i), side, "") for i, side in claims.qqe_labels([b.c for b in bars], sc).items()},
            "sr": {(t(i), side, kind) for i, (side, kind, _) in claims.level_break_labels(bars, sc).items()},
            "sqz": {(t(i), side, "") for i, side in claims.squeeze_labels(bars, sc).items()}}


def page_labels(view: dict) -> dict[str, dict[tuple, dict]]:
    """The labels a chart on the page shows, in the same form; an indicator that's off is left out."""
    out: dict[str, dict[tuple, dict]] = {}
    if "qqe" in view:
        out["qqe"] = {(x["t"], x["side"], ""): {"gap": x.get("gap")} for x in view["qqe"]}
    if "sr" in view:
        out["sr"] = {(x["t"], x["side"], x["kind"]): {"level": x["level"], "gap": x.get("gap")} for x in view["sr"]["breaks"]}
    if "sqz" in view:
        out["sqz"] = {(x["t"], x["side"], ""): {"gap": x.get("gap")} for x in view["sqz"]["signals"]}
    if "smc" in view:
        out["smc"] = {(x["t"], x["side"], f"{'internal' if x['internal'] else 'swing'} {x['kind']}"):
                      {"level": x["level"], "from": x["from"], "gap": x.get("gap")} for x in view["smc"]["structure"]}
    return out


def label_name(source: str, side: str, kind: str, gap: bool = False) -> str:
    way = side.capitalize()
    if source == "qqe":
        return f"QQE {way}"
    if source == "sqz":
        return f"SQZ {'Gap ' if gap else ''}{way}"
    if source == "sr":
        return f"S/R {'Bull' if side == 'long' else 'Bear'} Wick" if kind == "wick" else f"S/R {'Gap ' if gap else 'Break '}{'Up' if side == 'long' else 'Down'}"
    scale, what = kind.split(" ")
    return f"{what}{' (swing)' if scale == 'swing' else ''} {'up' if side == 'long' else 'down'}"


class Audit:
    def __init__(self, cfg: Config, settings: Callable[[], ScanConfig], candles: Candles, second: Candles | None, flow, journal: Journal,
                 now: Callable[[], datetime], daily: Callable[[list[str], datetime], dict[str, list[Bar]]] | None = None,
                 alert: Callable[[str, str], None] | None = None, state_dir: Path | None = None):
        self.cfg, self.settings, self.candles, self.second, self.flow = cfg, settings, candles, second, flow
        self.journal, self.now, self.fetch_daily, self.alert = journal, now, daily, alert
        self.state_dir = state_dir or cfg.log_dir
        self.day: date | None = None
        self.records: dict[str, dict] = {}
        self.daily: dict[str, list[Bar]] = {}
        self.started: datetime | None = None
        self.alerted = 0
        self.zones: dict[str, list[str]] = {}
        self.candle_checks: dict[str, dict] = {}
        self.errors: dict[str, str] = {}
        self.again: dict[tuple, list] = {}  # (source, symbol, candle) -> analyze's signals on it
        self.worked: dict[tuple, tuple] = {}  # (candle size, symbol) -> the labels its candles call for
        self.second_at: datetime | None = None
        self.pending_missed: dict[str, int] = {}

    # --- one look -------------------------------------------------------------------------------------

    def cycle(self) -> list[dict]:
        """Look for new signals, check what can be checked now, and write the results. Returns the new problems."""
        now = self.now().astimezone(self.cfg.schedule.tz)
        if self.day != now.date():
            self._open(now)
        sc = self.settings()
        state, minute = self._read("scan_state.json"), self._read("scan_chart_1m.json")
        events = [e for e in self.journal.events(now.date()) if e.get("event") in ("chart_signal", "live_cross")]
        views = {}
        if state.get("updated") and datetime.fromisoformat(state["updated"]).date() == now.date():
            views = {(sc.bar_minutes, s): v for s, v in (state.get("symbols") or {}).items()}
            if sc.bar_minutes != 1 and minute.get("updated") and datetime.fromisoformat(minute["updated"]).date() == now.date():
                views |= {(1, s): v for s, v in (minute.get("symbols") or {}).items()}
        shown = sorted({s for _, s in views})
        symbols = sorted(set(shown) | {e["symbol"] for e in events})
        if not symbols:
            self._write(now)
            return []
        ready = self._load(symbols, sc, now)
        before = {k: r["verdict"] for k, r in self.records.items()}
        self._collect(events, views, sc, now)
        if ready:
            for r in self.records.values():
                if r["verdict"] == "waiting":
                    self._check(r, sc, now)
            self._labels(views, sc, now)
            self._missed(events, shown, sc, now)
            self._after(now)
        if self.second is not None:
            self._second_source(sc, now)
        self._write(now)
        fresh = [r for k, r in self.records.items() if r["verdict"] in BAD and before.get(k) not in BAD]
        self._tell(fresh, now)
        return fresh

    def _open(self, now: datetime) -> None:
        self.day, self.started, self.alerted = now.date(), now, 0
        self.records, self.daily, self.again, self.zones, self.candle_checks = {}, {}, {}, {}, {}
        self.second_at, self.pending_missed = None, {}
        try:
            saved = json.loads(self._path(now.date()).read_text())
            self.records = {r["key"]: r for r in saved.get("records", [])}
            self.started = datetime.fromisoformat(saved["started"])
            self.alerted = saved.get("alerted", 0)
        except (OSError, ValueError, KeyError):
            pass

    def _path(self, day: date) -> Path:
        return self.cfg.log_dir / "audit" / f"{day.isoformat()}.json"

    def _read(self, name: str) -> dict:
        try:
            return json.loads((self.state_dir / name).read_text())
        except (OSError, ValueError):
            return {}

    def _load(self, symbols: list[str], sc: ScanConfig, now: datetime) -> bool:
        """Fresh candles; a failed download only means checking again next time."""
        try:
            self.candles.update(symbols, sc.bar_minutes, 2, True, now)
            self.candles.update(symbols, sc.bar_minutes, sc.history_sessions, False, now)
            self.candles.update(symbols, 1, 2, True, now)
            new = [s for s in symbols if s not in self.daily]
            if new and self.fetch_daily is not None:
                self.daily |= dict.fromkeys(new, []) | self.fetch_daily(new, now)
            self.errors.pop("candles", None)
            return True
        except Exception as e:
            self.errors["candles"] = repr(e)[:200]
            return False

    def _bars(self, source: Candles, symbol: str, tf: int, sc: ScanConfig, rth: bool, day: date) -> list[Bar]:
        """The candles a signal was worked out from: the scanner's last three sessions, extended hours included, or the
        regular-hours ones the chart's labels use."""
        if tf == 1 and sc.bar_minutes != 1:
            return [b for b in last_sessions(source.get(symbol, 1, 2, True, day)) if not rth or regular(b)]
        if not rth:
            return last_sessions(source.get(symbol, tf, 2, True, day))
        return source.get(symbol, tf, sc.history_sessions, False, day)

    # --- what there is to check -----------------------------------------------------------------------

    def _add(self, key: str, now: datetime, **facts) -> dict:
        r = {"key": key, "seen": now.isoformat(timespec="seconds"), "verdict": "waiting", "faults": [], "how": None,
             "second": None, "second_faults": [], "flow": None, "after": {}, "result": None} | facts
        self.records[key] = r
        return r

    def _collect(self, events: list[dict], views: dict, sc: ScanConfig, now: datetime) -> None:
        for e in events:
            if e["event"] == "live_cross":
                key = f"live|{e['symbol']}|{e['level']}|{e['side']}|{e['ts']}"
                if key not in self.records:
                    self._add(key, now, source="live", tf=0, symbol=e["symbol"], kind="live_cross", side=e["side"], price=e["price"],
                              name=f"LIVE {'above' if e['side'] == 'buy' else 'below'} {e['level']}", time=e["ts"],
                              level=e["level"], level_price=e["level_price"], detail=f"crossed the {e['level']} {e['level_price']:.2f}")
                continue
            key = f"scan|{e['symbol']}|{e['kind']}|{e.get('level') or ''}|{e['bar_time']}"
            if key in self.records and self.records[key]["verdict"] != "missed":
                continue  # (one found missing can still be logged: the scanner catches up after a pause)
            closed = datetime.fromisoformat(e["bar_time"]) + timedelta(minutes=sc.bar_minutes)
            self._add(key, now, source="scan", tf=sc.bar_minutes, symbol=e["symbol"], kind=e["kind"], side=e["side"], price=e["price"],
                      name=LABELS.get(e["kind"], e["kind"]), time=e["bar_time"], level=e.get("level"), detail=e.get("detail") or "",
                      weak=bool(e.get("weak")), flow_said=e.get("flow"),
                      late=round((datetime.fromisoformat(e["ts"]) - closed).total_seconds()))
        for (tf, sym), view in views.items():
            first = datetime.combine(now.date(), time(0), tzinfo=now.tzinfo).timestamp()
            for source, found in page_labels(view).items():
                for (t, side, kind), facts in found.items():
                    if t < first:
                        continue
                    key = f"{tf}|{sym}|{source}|{kind}|{side}|{t}"
                    r = self.records.get(key)
                    if r is None:
                        self._add(key, now, source=source, tf=tf, symbol=sym, kind=kind, side="buy" if side == "long" else "sell", price=None,
                                  name=label_name(source, side, kind, bool(facts.get("gap"))), t=t, label=[t, side, kind],
                                  time=datetime.fromtimestamp(t, now.tzinfo).isoformat(), level=facts.get("level"),
                                  pivot=facts.get("from"), detail="")
                    elif r["verdict"] in ("missed", "vanished"):
                        r["verdict"], r["faults"] = "waiting", []  # it's there after all

    # --- 1. is it right? ------------------------------------------------------------------------------

    def _check(self, r: dict, sc: ScanConfig, now: datetime) -> None:
        if r["source"] == "live":
            at = datetime.fromisoformat(r["time"])
            m = at.replace(second=0, microsecond=0)
            around = [b for b in self._bars(self.candles, r["symbol"], 1, sc, False, self.day)
                      if m - timedelta(minutes=2) <= b.t <= m + timedelta(minutes=1)]
            if now < m + timedelta(minutes=3):
                return
            f = claims.live_cross_faults(r["side"], r["price"], r["level_price"], around)
            r["verdict"], r["faults"], r["how"] = ("unknown", ["no 1-minute candles then"], None) if f is None else (
                "wrong" if f else "ok", f, "price")
        elif r["source"] == "scan":
            self._check_scan(r, sc, now)

    def _signals_at(self, which: str, source: Candles, symbol: str, at: datetime, sc: ScanConfig) -> tuple[list, list[Bar]]:
        bars = self._bars(source, symbol, sc.bar_minutes, sc, False, self.day)
        cut = [b for b in bars if b.t <= at]
        if not cut or cut[-1].t != at:
            return [], []
        key = (which, symbol, at)
        if key not in self.again:
            daily = [b for b in self.daily.get(symbol, [])]
            self.again[key] = analyze(symbol, cut, self.day, sc, daily)[0]
        return self.again[key], cut

    def _check_scan(self, r: dict, sc: ScanConfig, now: datetime) -> None:
        at = datetime.fromisoformat(r["time"])
        if r["kind"].startswith("of_"):
            return self._check_flow_signal(r, at, sc, now)
        found, cut = self._signals_at("first", self.candles, r["symbol"], at, sc)
        if not cut:
            if now > at + timedelta(minutes=sc.bar_minutes + 10):
                r["verdict"], r["faults"] = "wrong", ["the fresh candles have no such candle"]
            return
        same = [s for s in found if (s.kind, s.side, s.level) == (r["kind"], r["side"], r["level"])]
        r["close"] = cut[-1].c
        f = claims.faults(r["kind"], r["side"], r["price"], r["detail"], r["level"], cut, self.day, sc, self.daily.get(r["symbol"]))
        r["how"] = "again" if f is None else "both"
        hard = [x for x in f or [] if not x.startswith(claims.WORDING)]
        if hard:
            corrected = abs(cut[-1].c - r["price"]) > max(claims.CENT, 0.0003 * r["price"])
            r["verdict"] = "changed" if corrected else "wrong"
            r["faults"] = ([f"the candle was corrected afterwards (close {r['price']:.2f}, now {cut[-1].c:.2f})"] if corrected else []) + hard
        elif not same:
            r["verdict"] = "changed"
            r["faults"] = ["it doesn't come out again on the candles as they are now"]
        elif f:
            r["verdict"], r["faults"] = "wording", [x.removeprefix(claims.WORDING) for x in f]
        else:
            r["verdict"] = "ok"
        if r.get("flow_said"):
            self._check_flow_note(r, at)

    def _flow_bars(self, symbol: str, sc: ScanConfig):
        if self.flow is None:
            return None, []
        bars = self._bars(self.candles, symbol, sc.bar_minutes, sc, False, self.day)
        prev = [b for b in bars if b.t.date() < self.day and regular(b)]
        prev = [b for b in prev if b.t.date() == prev[-1].t.date()]
        prior = {"prior high": max(b.h for b in prev), "prior low": min(b.l for b in prev), "prior close": prev[-1].c} if prev else {}
        try:
            out = self.flow.signals(symbol, self.now(), prior)
            self.errors.pop(f"tape {symbol}", None)
            return out
        except Exception as e:
            self.errors[f"tape {symbol}"] = repr(e)[:200]
            return None, []

    def _check_flow_signal(self, r: dict, at: datetime, sc: ScanConfig, now: datetime) -> None:
        fbars, sigs = self._flow_bars(r["symbol"], sc)
        if not fbars or fbars[-1].t < at:
            if self.flow is None or now > at + timedelta(minutes=45):
                r["verdict"], r["faults"] = "unknown", ["no trades to check it against"]
            return
        bar = next((b for b in fbars if b.t == at), None)
        r["how"] = "again"
        if any((s.kind, s.side, s.bar_time) == (r["kind"], r["side"], at) for s in sigs):
            r["verdict"], r["close"] = "ok", bar.c if bar else r["price"]
        else:
            r["verdict"], r["faults"] = "changed", ["it doesn't come out again from the day's trades downloaded afresh"]

    def _check_flow_note(self, r: dict, at: datetime) -> None:
        """Whether the order-flow remark on a chart signal still reads the same from the trades downloaded afresh."""
        from .orderflow import check

        fbars, _ = self._flow_bars(r["symbol"], self.settings())
        chk = check(fbars, at, r["side"], self.cfg.orderflow.check_bars, self.cfg.orderflow.feed) if fbars else None
        if chk is not None:
            r["flow"] = "same" if chk.verdict == r["flow_said"] else f"now {chk.verdict}, said {r['flow_said']}"

    def _labels(self, views: dict, sc: ScanConfig, now: datetime) -> None:
        """The labels on the charts: right, still there, and none missing."""
        first = datetime.combine(now.date(), time(0), tzinfo=now.tzinfo).timestamp()
        self.zones = {}
        for (tf, sym), view in views.items():
            bars = self._bars(self.candles, sym, tf, sc, True, self.day)
            rows = view.get("bars") or []
            if not bars or not rows:
                continue
            last = rows[-1][0]  # the page's newest closed candle
            bars = [b for b in bars if b.t.timestamp() <= last]
            here = page_labels(view)
            stamp = (len(bars), bars[-1] if bars else None, sc.model_dump_json())
            if self.worked.get((tf, sym), (None,))[0] != stamp:  # worked out again only when there's a new candle
                self.worked[(tf, sym)] = (stamp, labels_on(bars, sc), long_way(bars, sc))
            _, own, other = self.worked[(tf, sym)]
            index = {int(b.t.timestamp()): i for i, b in enumerate(bars)}
            for source, shown in here.items():
                for label in shown:
                    r = self.records.get(f"{tf}|{sym}|{source}|{label[2]}|{label[1]}|{label[0]}")
                    if r is None or r["verdict"] != "waiting" or label[0] not in index:
                        continue
                    self._check_label(r, label, bars, index, own[source], other.get(source), sc)
                for label, facts in own[source].items():
                    if label[0] < first or label in shown:
                        continue
                    key = f"{tf}|{sym}|{source}|{label[2]}|{label[1]}|{label[0]}"
                    self.pending_missed[key] = self.pending_missed.get(key, 0) + 1
                    if self.pending_missed[key] < 3:
                        continue  # the page may be a moment behind
                    r = self.records.get(key)
                    if r is None:
                        r = self._add(key, now, source=source, tf=tf, symbol=sym, kind=label[2], side="buy" if label[1] == "long" else "sell",
                                      price=None, name=label_name(source, label[1], label[2]), t=label[0], label=list(label),
                                      time=datetime.fromtimestamp(label[0], now.tzinfo).isoformat(), level=facts.get("level"), detail="")
                        r["verdict"], r["faults"] = "missed", ["the candles call for this label, but the chart doesn't show it"]
            for r in self.records.values():  # labels that were there and aren't any more
                if r["tf"] == tf and r["symbol"] == sym and r["source"] in here and r["verdict"] in ("ok", "changed"):
                    if tuple(r["label"]) not in here[r["source"]] and r["t"] <= last and r["t"] >= rows[0][0]:
                        r["gone"] = r.get("gone", 0) + 1
                        if r["gone"] >= 3:
                            r["verdict"], r["faults"] = "vanished", ["it was on the chart and has disappeared"]
                    else:
                        r["gone"] = 0
            blocks = []
            for x in (view.get("smc") or {}).get("blocks") or []:
                if x["from"] in index:
                    blocks += claims.zone_faults(bars, index[x["from"]], x["top"], x["bottom"], x["side"])
            if blocks:
                self.zones[f"{sym} {tf}m"] = blocks

    def _check_label(self, r: dict, label: tuple, bars: list[Bar], index: dict, own: dict, other: set | None, sc: ScanConfig) -> None:
        i = index[label[0]]
        r["close"] = bars[i].c
        if r["source"] == "smc":
            fact = own.get(label)
            r["how"] = "both"
            if fact is None:
                r["verdict"], r["faults"] = "changed", ["it doesn't come out again on the candles as they are now"]
                return
            f = claims.structure_faults(bars, i, fact["pivot"], r["level"], label[1], INTERNAL if fact["internal"] else sc.smc_swing)
            r["verdict"], r["faults"] = ("wrong", f) if f else ("ok", [])
            return
        r["how"] = "both"
        if label in other:
            r["verdict"], r["faults"] = "ok", []
        elif label in own:
            r["verdict"], r["faults"] = "wrong", ["worked out separately from the rules, it isn't there"]
        else:
            r["verdict"], r["faults"] = "changed", ["it doesn't come out again on the candles as they are now"]
        if r["verdict"] == "ok" and r["level"] is not None and label in own and abs(own[label]["level"] - r["level"]) > claims.CENT:
            r["verdict"], r["faults"] = "wrong", [f"its level is {own[label]['level']:.2f}, not {r['level']:.2f}"]

    def _missed(self, events: list[dict], shown: list[str], sc: ScanConfig, now: datetime) -> None:
        """Signals the candles call for that the scanner never logged."""
        fired, awake = defaultdict(list), set()
        since: dict[str, datetime] = {}
        for e in events:
            if e["event"] == "chart_signal":
                at = datetime.fromisoformat(e["bar_time"])
                awake.add(at)
                fired[(e["symbol"], e.get("family", e["kind"]), e.get("level"))].append(at)
                since[e["symbol"]] = min(at, since.get(e["symbol"], at))
        for r in self.records.values():
            if r["verdict"] == "missed" and r["source"] == "scan":
                fired[(r["symbol"], r["family"], r["level"])].append(datetime.fromisoformat(r["time"]))
        step, wait = timedelta(minutes=sc.bar_minutes), timedelta(minutes=max(sc.cooldown_minutes, 1))
        for sym in shown:
            for b in self._bars(self.candles, sym, sc.bar_minutes, sc, False, self.day):
                closed = b.t + step
                looks = 3 if now - closed < timedelta(minutes=15) else 1
                if b.t.date() != self.day or sym not in since or b.t < since[sym] or now < closed + timedelta(minutes=2):
                    continue
                if not scan_hours(b.t, sc.sessions):
                    continue
                for s in self._signals_at("first", self.candles, sym, b.t, sc)[0]:
                    if any(b.t - wait < at <= b.t for at in fired[s.key]):
                        continue
                    key = f"scan|{sym}|{s.kind}|{s.level or ''}|{b.t.isoformat()}"
                    self.pending_missed[key] = self.pending_missed.get(key, 0) + 1
                    if key in self.records or self.pending_missed[key] < looks:
                        continue  # the scanner may be a moment behind
                    r = self._add(key, now, source="scan", tf=sc.bar_minutes, symbol=sym, kind=s.kind, side=s.side, price=s.price, name=s.label,
                                  time=b.t.isoformat(), level=s.level, detail=s.detail, weak=s.weak, flow_said=None, late=None, close=b.c,
                                  family=s.family)
                    fired[s.key].append(b.t)
                    r["verdict"], r["how"] = "missed", "again"
                    r["faults"] = ["the candles call for it, but the scanner didn't log it" if b.t in awake else
                                   "the scanner logged nothing for this candle: the computer was asleep or offline"]

    # --- 2. does a second source agree? -----------------------------------------------------------------

    def _second_source(self, sc: ScanConfig, now: datetime) -> None:
        due = [r for r in self.records.values() if r["second"] is None and self._closed(r) + SECOND_AFTER <= now]
        if self.second_at is not None and now - self.second_at < timedelta(minutes=5):
            return
        symbols = sorted({r["symbol"] for r in due})
        if not symbols:
            return
        self.second_at = now
        try:
            self.second.update(symbols, sc.bar_minutes, 2, True, now)
            self.second.update(symbols, sc.bar_minutes, sc.history_sessions, False, now)
            self.second.update(symbols, 1, 2, True, now)
            self.errors.pop("second source", None)
        except Exception as e:
            self.errors["second source"] = repr(e)[:200]
            return
        self._compare_candles(symbols, sc)
        worked: dict[tuple, tuple] = {}
        for r in due:
            if r["source"] == "live" or r["kind"].startswith("of_"):
                r["second"] = "none"  # a live cross and order flow have no full-market candle to compare with
                continue
            if r["verdict"] in ("waiting", "unknown"):
                continue
            at = datetime.fromisoformat(r["time"])
            if r["source"] == "scan":
                found, cut = self._signals_at("second", self.second, r["symbol"], at, sc)
                if not cut:
                    continue
                same = any((s.kind, s.side, s.level) == (r["kind"], r["side"], r["level"]) for s in found)
            else:
                key = (r["tf"], r["symbol"])
                if key not in worked:
                    bars = self._bars(self.second, r["symbol"], r["tf"], sc, True, self.day)
                    worked[key] = (labels_on(bars, sc) if bars else {}, {int(b.t.timestamp()) for b in bars})
                labels, times = worked[key]
                if r["t"] not in times:
                    continue
                same = tuple(r["label"]) in labels[r["source"]]
            r["second"] = "agrees" if same else "differs"
            r["second_faults"] = [] if same else self._candle_gap(r, at, sc) + ["on the full-market candles it isn't there: a close call"]

    def _closed(self, r: dict) -> datetime:
        return datetime.fromisoformat(r["time"]) + timedelta(minutes=r["tf"])

    def _pair(self, symbol: str, tf: int, sc: ScanConfig) -> list[tuple[Bar, Bar]]:
        mine = {b.t: b for b in self._bars(self.candles, symbol, tf, sc, True, self.day) if b.t.date() == self.day}
        return [(mine[b.t], b) for b in self._bars(self.second, symbol, tf, sc, True, self.day) if b.t in mine and b.t.date() == self.day]

    @staticmethod
    def _differs(a: Bar, b: Bar) -> bool:
        slack = max(0.02, 0.0005 * a.c)
        return any(abs(x - y) > slack for x, y in ((a.o, b.o), (a.h, b.h), (a.l, b.l), (a.c, b.c)))

    def _candle_gap(self, r: dict, at: datetime, sc: ScanConfig) -> list[str]:
        for a, b in self._pair(r["symbol"], r["tf"], sc):
            if a.t == at and self._differs(a, b):
                return [f"its candle is O {a.o:.2f} H {a.h:.2f} L {a.l:.2f} C {a.c:.2f} on Robinhood, "
                        f"O {b.o:.2f} H {b.h:.2f} L {b.l:.2f} C {b.c:.2f} on the full market"]
        return []

    def _compare_candles(self, symbols: list[str], sc: ScanConfig) -> None:
        for sym in symbols:
            for tf in {sc.bar_minutes, 1}:
                pairs = self._pair(sym, tf, sc)
                off = [a.t.strftime("%H:%M") for a, b in pairs if self._differs(a, b)]
                self.candle_checks[f"{sym} {tf}m"] = {"compared": len(pairs), "differ": off}

    # --- 3. what happened next? ---------------------------------------------------------------------------

    def _after(self, now: datetime) -> None:
        minute: dict[str, dict[datetime, Bar]] = {}
        for r in self.records.values():
            if r["result"] is not None or r["verdict"] in ("waiting", "unknown") or not r["side"]:
                continue
            start = self._closed(r)
            entry = r["price"] if r["price"] is not None else r.get("close")
            if entry is None or now < start + timedelta(minutes=HORIZONS[0]):
                continue
            rows = minute.setdefault(r["symbol"], {b.t: b for b in self.candles.get(r["symbol"], 1, 2, True, self.day)})
            way = 1 if r["side"] == "buy" else -1
            for h in HORIZONS:
                at = start + timedelta(minutes=h)
                if str(h) in r["after"] or now < at:
                    continue
                last = next((rows[t] for k in range(1, 4) if (t := (at - timedelta(minutes=k)).replace(second=0, microsecond=0)) in rows), None)
                if last is None:
                    if now > at + timedelta(minutes=10):
                        r["result"] = "none"  # the session ended, or nothing traded
                    continue
                r["after"][str(h)] = round(way * (last.c - entry) / entry * 100, 3)
            if str(HORIZONS[-1]) in r["after"]:
                move = r["after"][str(HORIZONS[-1])]
                r["result"] = "right" if move >= FLAT_PCT else "wrong" if move <= -FLAT_PCT else "flat"
                span = [b for t, b in rows.items() if start <= t < start + timedelta(minutes=HORIZONS[-1])]
                if span:
                    up, down = (max(b.h for b in span) - entry) / entry * 100, (min(b.l for b in span) - entry) / entry * 100
                    r["after"] |= {"best": round(max(way * up, way * down), 3), "worst": round(min(way * up, way * down), 3)}

    def drift(self) -> dict[str, dict]:
        """What any regular-hours candle's close did over the next 30 minutes today, by symbol: how often it rose, and the average."""
        out = {}
        for sym in {r["symbol"] for r in self.records.values()}:
            rows = [b for b in self.candles.get(sym, 1, 2, True, self.day) if regular(b) and b.t.date() == self.day]
            at = {b.t: b.c for b in rows}
            moves = [(at[t2] - c) / c * 100 for t, c in at.items() if (t2 := t + timedelta(minutes=HORIZONS[-1])) in at]
            if moves:
                out[sym] = {"up": sum(m >= FLAT_PCT for m in moves) / len(moves), "down": sum(m <= -FLAT_PCT for m in moves) / len(moves),
                            "avg": sum(moves) / len(moves)}
        return out

    # --- results --------------------------------------------------------------------------------------------

    def summary(self) -> dict:
        rows = list(self.records.values())
        drift = self.drift()
        counts = defaultdict(int)
        for r in rows:
            counts[r["verdict"]] += 1
            counts[f"second_{r['second'] or 'waiting'}"] += 1
            if (r.get("late") or 0) > 120:
                counts["late"] += 1
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for r in rows:
            groups[(r["source"], r["tf"])].append(r)
        table = []
        for (source, tf), rs in sorted(groups.items(), key=lambda kv: (list(SOURCES).index(kv[0][0]), -kv[0][1])):
            done = [r for r in rs if r["result"] in ("right", "wrong", "flat")]
            usual = [(drift[r["symbol"]]["up" if r["side"] == "buy" else "down"], drift[r["symbol"]]["avg"] * (1 if r["side"] == "buy" else -1))
                     for r in done if r["symbol"] in drift]
            table.append({"source": source, "name": SOURCES[source], "tf": tf, "n": len(rs), "ok": sum(r["verdict"] == "ok" for r in rs),
                          "bad": sum(r["verdict"] in BAD for r in rs), "wording": sum(r["verdict"] == "wording" for r in rs),
                          "changed": sum(r["verdict"] == "changed" for r in rs), "differs": sum(r["second"] == "differs" for r in rs),
                          "agrees": sum(r["second"] == "agrees" for r in rs), "done": len(done),
                          "right": sum(r["result"] == "right" for r in done), "wrong": sum(r["result"] == "wrong" for r in done),
                          "avg": round(sum(r["after"]["30"] for r in done) / len(done), 3) if done else None,
                          "usual_right": round(sum(u for u, _ in usual) / len(usual), 3) if usual else None,
                          "usual_avg": round(sum(a for _, a in usual) / len(usual), 3) if usual else None})
        return {"counts": dict(counts), "total": len(rows), "table": table, "zones": self.zones, "candles": self.candle_checks,
                "errors": self.errors}

    def _write(self, now: datetime) -> None:
        head = {"day": self.day.isoformat(), "updated": now.isoformat(timespec="seconds"), "started": self.started.isoformat(timespec="seconds"),
                "alerted": self.alerted} | self.summary()
        rows = sorted(self.records.values(), key=lambda r: r["time"], reverse=True)
        problems = [{k: r.get(k) for k in ("key", "symbol", "tf", "name", "time", "verdict", "faults")} for r in rows if r["verdict"] in BAD][:20]
        for path, data in ((self._path(self.day), head | {"records": rows}), (self.cfg.log_dir / "audit_state.json", head | {"problems": problems})):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data))
                os.replace(tmp, path)
            except OSError:
                pass

    def _tell(self, fresh: list[dict], now: datetime) -> None:
        """One message for this look's new problems, if they're about signals still worth knowing about."""
        recent = [r for r in fresh if now - self._closed(r) < timedelta(minutes=30) and not r.get("weak")]
        if not recent or self.alert is None or self.alerted >= ALERTS_PER_DAY:
            return
        self.alerted += 1
        text = "\n".join(line(r) for r in recent[:6]) + (f"\n(+{len(recent) - 6} more on the Audit tab)" if len(recent) > 6 else "")
        self.alert(f"AUDIT: {len(recent)} signal{'s' if len(recent) > 1 else ''} to distrust: "
                   f"{', '.join(sorted({r['symbol'] for r in recent}))}", text)


def line(r: dict) -> str:
    where = f" {r['tf']}m" if r["tf"] else ""
    return (f"{datetime.fromisoformat(r['time']):%H:%M} {r['symbol']}{where} {r['name']}: {r['verdict']}"
            + (f" ({'; '.join(r['faults'])})" if r["faults"] else ""))


def report(audit: Audit) -> str:
    s = audit.summary()
    c = s["counts"]
    out = [f"Audit of {audit.day}: {s['total']} signals.",
           f"  right {c.get('ok', 0)}, wrong {c.get('wrong', 0)}, missed {c.get('missed', 0)}, vanished {c.get('vanished', 0)}, "
           f"wording only {c.get('wording', 0)}, changed by a corrected candle {c.get('changed', 0)}, "
           f"no check {c.get('unknown', 0)}, waiting {c.get('waiting', 0)}",
           f"  full-market candles: agree {c.get('second_agrees', 0)}, differ {c.get('second_differs', 0)}, "
           f"not yet {c.get('second_waiting', 0)}", "",
           f"{'':18}{'n':>6}{'right':>7}{'wrong':>7}{'differ':>8}   after 30 min: went its way / any candle, average / any candle"]
    for g in s["table"]:
        row = f"{g['name']:12}{str(g['tf']) + 'm' if g['tf'] else '':>6}{g['n']:>6}{g['ok']:>7}{g['bad']:>7}{g['differs']:>8}"
        if g["done"]:
            row += f"   {g['right'] / g['done']:.0%} of {g['done']}"
            if g["usual_right"] is not None:
                row += f" / {g['usual_right']:.0%}, {g['avg']:+.3f}% / {g['usual_avg']:+.3f}%"
        out.append(row)
    bad = [r for r in audit.records.values() if r["verdict"] in BAD]
    if bad:
        out += ["", "Problems:"] + ["  " + line(r) for r in sorted(bad, key=lambda r: r["time"])]
    out += [f"  {k}: {v}" for k, v in s["errors"].items()]
    return "\n".join(out)


def watch(audit: Audit, print_fn: Callable[[str], None] = print, sleep: Callable[[float], None] = time_.sleep) -> None:
    """A look every minute while the scanner's sessions run, and for 45 minutes after, so the last signals get their checks."""
    while True:
        now = audit.now().astimezone(audit.cfg.schedule.tz)
        sessions = audit.settings().sessions
        if scan_hours(now, sessions) or scan_hours(now - timedelta(minutes=45), sessions):
            try:
                before = len(audit.records)
                bad = audit.cycle()
                if bad or len(audit.records) != before:
                    c = audit.summary()["counts"]
                    print_fn(f"{now:%H:%M:%S} {len(audit.records)} signals: {c.get('ok', 0)} right, "
                             f"{sum(c.get(k, 0) for k in BAD)} wrong or missed" + "".join(f"\n  {line(r)}" for r in bad))
                for k, v in audit.errors.items():
                    print_fn(f"{now:%H:%M:%S} {k}: {v}")
            except Exception as e:  # nothing here may stop the watch
                print_fn(f"{now:%H:%M:%S} audit failed: {e!r}")
        now = audit.now().astimezone(audit.cfg.schedule.tz)
        sleep(max(5.0, 60 - (now.second - 25) % 60))  # 25 seconds past the minute: the scanner has seen the candle
