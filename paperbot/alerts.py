"""Alerts you set on the page, like TradingView's: on a price, on any indicator, or on several conditions together, for
one symbol or every symbol on a watchlist. They don't expire unless you give them an end date. Checked every 15
seconds from 4:00 to 20:00 on weekdays; a match shows on the page and, if you want, on your phone.

Each condition compares two things on the alert's candles: price, volume, a number, or an indicator line. "Once per
bar close" looks only at finished candles; "once per bar" and "only once" also look at the candle still forming (its
price is the latest trade), so they can fire on a cross that the candle's close later takes back."""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path

from zoneinfo import ZoneInfo

from . import charting as ch
from .features import Bar
from .studies import CATALOG, compute
from .trendlines import Line, line_from_json, track

ET = ZoneInfo("America/New_York")

OPS = {"crosses_above": "crosses above", "crosses_below": "crosses below", "crosses": "crosses", "above": "is above",
       "below": "is below"}
FIRED = {"crosses_above": "crossed above", "crosses_below": "crossed below", "crosses": "crossed", "above": "is above",
         "below": "is below"}  # the words when it has happened
# a drawn line's break and retest (paperbot/trendlines.py), on closed candles: the left side is the line
RETESTS = {"retest": "breaks and is retested, holding", "retest_up": "breaks up and is retested, holding",
           "retest_down": "breaks down and is retested, holding"}
TRIGGERS = {"once": "Only once", "bar": "Once per bar", "close": "Once per bar close"}
TICKER = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
MAX_ALERTS = 1000
# a candle counts as closed this long after its minute ends: the data source's last trades of the minute can arrive a few
# seconds late, and a close judged before then can differ from the final one
SETTLE = timedelta(seconds=20)


def _operand(x) -> dict:
    if not isinstance(x, dict):
        raise ValueError("each side of a condition must be price, volume, a number, an indicator or a drawn line")
    kind = x.get("kind")
    if kind == "line":
        d = x.get("line") or {}
        sym = str(x.get("symbol") or "").upper()
        if not TICKER.match(sym):
            raise ValueError("a drawn line belongs to one symbol")
        line = line_from_json(d, ET)
        return {"kind": "line", "symbol": sym, "line": {"id": line.id, "kind": line.kind, "t1": int(line.t1.timestamp()),
                "p1": line.p1, "t2": int(line.t2.timestamp()), "p2": line.p2, "clock": line.clock, "extend": line.extend,
                "name": line.name}}
    if kind in ("price", "volume"):
        return {"kind": kind}
    if kind == "value":
        try:
            return {"kind": "value", "value": float(x.get("value"))}
        except (TypeError, ValueError):
            raise ValueError("a number is missing in a condition") from None
    if kind == "study":
        st = CATALOG.get(x.get("type"))
        if st is None:
            raise ValueError(f"unknown indicator {x.get('type')!r}")
        out = x.get("output") or st.outputs[0].key
        if out not in {o.key for o in st.outputs}:
            raise ValueError(f"{st.name} has no line {out!r}")
        inputs = {k: v for k, v in (x.get("inputs") or {}).items() if not (k == "source" and ":" in str(v))}
        compute([Bar(datetime(2000, 1, 3, 10), 1, 1, 1, 1, 1)], [{"type": st.type, "inputs": inputs}])  # checks the inputs
        return {"kind": "study", "type": st.type, "inputs": inputs, "output": out}
    raise ValueError(f"unknown kind {kind!r}")


def validate(d: dict, watchlists: dict[str, list[str]]) -> dict:
    """An alert from the page, checked, with everything it needs filled in."""
    name = str(d.get("name") or "").strip()[:80]
    symbols = [str(s).strip().upper() for s in (d.get("symbols") or []) if str(s).strip()]
    watchlist = str(d.get("watchlist") or "").strip() or None
    if watchlist and watchlist not in watchlists:
        raise ValueError(f"there's no watchlist {watchlist!r}")
    if not symbols and not watchlist:
        raise ValueError("pick a symbol or a watchlist")
    bad = [s for s in symbols if not TICKER.match(s)]
    if bad:
        raise ValueError(f"not a ticker: {', '.join(bad)}")
    tf = ch.parse_interval(str(d.get("tf") or "1m"))
    if tf.unit not in "mD":
        raise ValueError("alerts run on minute, hour or daily candles")
    conds = d.get("conditions") or []
    if not isinstance(conds, list) or not 1 <= len(conds) <= 5:
        raise ValueError("an alert needs 1 to 5 conditions")
    conditions = []
    for c in conds:
        op = c.get("op") if isinstance(c, dict) else None
        if op not in OPS and op not in RETESTS:
            raise ValueError(f"unknown comparison {op!r}")
        left = _operand(c.get("left"))
        if op in RETESTS:
            if left["kind"] != "line":
                raise ValueError("a break and retest is watched on a drawn line")
            right = {"kind": "value", "value": 0.0}
        else:
            right = _operand(c.get("right"))
        if left["kind"] == right["kind"] == "value":
            raise ValueError("a condition can't compare two numbers")
        conditions.append({"left": left, "op": op, "right": right})
    lines = {x["symbol"] for c in conditions for x in (c["left"], c["right"]) if x["kind"] == "line"}
    if lines and (watchlist or set(symbols) != lines):
        raise ValueError("an alert on a drawn line is for that line's symbol only")
    trigger = d.get("trigger") or "once"
    if trigger not in TRIGGERS:
        raise ValueError(f"unknown trigger {trigger!r}")
    expires = d.get("expires") or None
    if expires:
        try:
            expires = datetime.fromisoformat(str(expires)).isoformat()
        except ValueError:
            raise ValueError("the end date isn't a date") from None
    night = d.get("session") == "24h" or any(x["kind"] == "line" and x["line"]["clock"] == "24h"
                                             for c in conditions for x in (c["left"], c["right"]))
    return {"name": name, "symbols": symbols, "watchlist": watchlist, "tf": tf.label, "conditions": conditions,
            "match": "any" if d.get("match") == "any" else "all", "trigger": trigger, "expires": expires,
            "phone": bool(d.get("phone", True)), "note": str(d.get("note") or "")[:300], "session": "24h" if night else "ext"}


def operand_name(x: dict) -> str:
    if x["kind"] == "line":
        return line_from_json(x["line"], ET).called
    if x["kind"] == "value":
        return f"{x['value']:g}"
    if x["kind"] in ("price", "volume"):
        return x["kind"].capitalize()
    st = CATALOG[x["type"]]
    full = {**{i.key: i.default for i in st.inputs}, **x["inputs"]}
    nums = " ".join(f"{full[i.key]:g}" for i in st.inputs if i.kind != "source")
    if st.type == "vwap" and x["output"] == "vwap":
        nums = ""  # the multiplier is only for the bands
    line = next(o.name for o in st.outputs if o.key == x["output"])
    tail = f" {line}" if len(st.outputs) > 1 and line != st.name else ""
    return f"{st.name} {nums}".strip() + tail


def describe(alert: dict, fired: bool = False) -> str:
    words = FIRED if fired else OPS
    parts = [f"{operand_name(c['left'])[0].upper()}{operand_name(c['left'])[1:]} {RETESTS[c['op']]}" if c["op"] in RETESTS else
             f"{operand_name(c['left'])} {words[c['op']]} {operand_name(c['right'])}" for c in alert["conditions"]]
    night = ", around the clock" if alert.get("session") == "24h" else ""
    return (" and " if alert["match"] == "all" else " or ").join(parts) + f" ({alert['tf']}{night})"


class AlertStore:
    """logs/alerts.json (the alerts) and logs/alerts_log.jsonl (each time one fired)."""

    def __init__(self, folder: Path):
        self.path = Path(folder) / "alerts.json"
        self.log_path = Path(folder) / "alerts_log.jsonl"
        self._lock = threading.RLock()

    def load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def save(self, alerts: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(alerts, indent=1))
        os.replace(tmp, self.path)

    def add(self, alert: dict, now: datetime) -> dict:
        with self._lock:
            alerts = self.load()
            if len(alerts) >= MAX_ALERTS:
                raise ValueError(f"at most {MAX_ALERTS} alerts")
            a = {"id": secrets.token_hex(4), **alert, "status": "active", "created": now.isoformat(), "fired": {}, "count": 0}
            alerts.append(a)
            self.save(alerts)
            return a

    def update(self, aid: str, change: dict, now: datetime) -> dict:
        with self._lock:
            alerts = self.load()
            a = next((x for x in alerts if x["id"] == aid), None)
            if a is None:
                raise KeyError(aid)
            if "status" in change and change["status"] in ("active", "paused"):
                a["status"] = change["status"]
                if change["status"] == "active":
                    a["fired"] = {}  # start again, like re-enabling a triggered alert on TradingView
            if "alert" in change:
                a.update(change["alert"])
            self.save(alerts)
            return a

    def remove(self, aid: str) -> None:
        with self._lock:
            self.save([x for x in self.load() if x["id"] != aid])

    def events(self, limit: int = 200) -> list[dict]:
        try:
            lines = self.log_path.read_text().splitlines()[-limit:]
        except OSError:
            return []
        out = []
        for line in reversed(lines):
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def log(self, event: dict) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as f:
            f.write(json.dumps(event) + "\n")


def warm_sessions(tf: ch.Interval) -> int:
    return 2 if tf.unit == "m" and tf.size <= 1 else 3 if tf.unit == "m" and tf.size <= 5 else 10 if tf.unit == "m" and tf.size <= 30 else 30


@dataclass
class Match:
    symbol: str
    bar: int  # the candle's time (epoch)
    price: float
    text: str


def candles(market, symbol: str, tf: ch.Interval, now: datetime, night: bool = False) -> tuple[list[Bar], bool]:
    """The alert's candles, and whether the last one is still forming. `night`: around the clock, from Robinhood's
    24-hour market (5-minute candles and up)."""
    if tf.unit == "D":
        bars = ch.resample_days(market.daily(symbol, 1), "D", int(tf.size))
        return bars, bool(bars) and bars[-1].t.date() == now.date() and now.time() < time(16)
    if night:
        step = 5 if tf.size % 5 == 0 else 1
        bars = ch.resample_minutes(market.night(symbol, min(warm_sessions(tf), 2) if step == 1 else warm_sessions(tf), step),
                                   int(tf.size), True, around_the_clock=True)
        return bars, bool(bars) and bars[-1].t + timedelta(minutes=tf.size) + SETTLE > now
    minutes, _ = market.minutes(symbol, warm_sessions(tf))
    bars = ch.resample_minutes(minutes, int(tf.size), True)
    return bars, bool(bars) and bars[-1].t + timedelta(minutes=tf.size) + SETTLE > now


def drawn_line(x: dict, resolve=None) -> Line:
    """The line as it is now on the chart (it may have been moved since the alert was set), or as it was then."""
    now = resolve(x["symbol"], x["line"]["id"]) if resolve else None
    return line_from_json(now or x["line"], ET)


def series(x: dict, bars: list[Bar], resolve=None) -> list[float | None]:
    if x["kind"] == "line":
        line = drawn_line(x, resolve)
        return [line.price_at(b.t) for b in bars]
    if x["kind"] == "price":
        return [b.c for b in bars]
    if x["kind"] == "volume":
        return [b.v for b in bars]
    if x["kind"] == "value":
        return [x["value"]] * len(bars)
    got = compute(bars, [{"id": "x", "type": x["type"], "inputs": x["inputs"]}])[0]
    return next(o["values"] for o in got["outputs"] if o["key"] == x["output"])


def holds(op: str, left: list, right: list, i: int) -> bool:
    if i < 0 or left[i] is None or right[i] is None:
        return False
    if op == "above":
        return left[i] > right[i]
    if op == "below":
        return left[i] < right[i]
    if i < 1 or left[i - 1] is None or right[i - 1] is None:
        return False
    up = left[i - 1] <= right[i - 1] and left[i] > right[i]
    down = left[i - 1] >= right[i - 1] and left[i] < right[i]
    return up if op == "crosses_above" else down if op == "crosses_below" else up or down


def check(alert: dict, symbol: str, market, now: datetime, resolve=None) -> Match | None:
    tf = ch.parse_interval(alert["tf"])
    bars, forming = candles(market, symbol, tf, now, alert.get("session") == "24h")
    retests = any(c["op"] in RETESTS for c in alert["conditions"])
    if (alert["trigger"] == "close" or retests) and forming:
        bars = bars[:-1]  # a break and retest is judged on closed candles only
    if len(bars) < 2:
        return None
    i = len(bars) - 1
    results, said = [], []
    for c in alert["conditions"]:
        if c["op"] in RETESTS:
            stages = track(drawn_line(c["left"], resolve), bars)
            last = stages[-1] if stages else None
            ok = (last is not None and last.stage == "confirmed" and last.index == i
                  and c["op"] in ("retest", f"retest_{last.side}"))
            results.append(ok)
            if ok:
                said.append(last.text)
        else:
            left, right = series(c["left"], bars, resolve), series(c["right"], bars, resolve)
            results.append(holds(c["op"], left, right, i))
    ok = all(results) if alert["match"] == "all" else any(results)
    if not ok:
        return None
    return Match(symbol, int(bars[i].t.timestamp()), bars[i].c, " ".join(said) or describe(alert, fired=True))


def evaluate(store: AlertStore, market, watchlists: dict[str, list[str]], now: datetime, send=None, resolve=None) -> list[dict]:
    """One pass over every active alert; returns what fired (already logged and sent)."""
    fired = []
    with store._lock:
        alerts = store.load()
        changed = False
        for a in alerts:
            if a.get("status") != "active":
                continue
            if a.get("expires") and now >= datetime.fromisoformat(a["expires"]).astimezone(now.tzinfo):
                a["status"], changed = "expired", True
                continue
            symbols = list(dict.fromkeys(a.get("symbols", []) + watchlists.get(a.get("watchlist") or "", [])))
            for sym in symbols:
                try:
                    m = check(a, sym, market, now, resolve)
                except Exception as e:  # one symbol's data problem mustn't stop the others
                    a["error"] = f"{sym}: {e}"[:200]
                    changed = True
                    continue
                if m is None or a["fired"].get(sym) == m.bar:
                    continue
                a["fired"][sym] = m.bar
                a["count"] = a.get("count", 0) + 1
                a["last"] = now.isoformat()
                a.pop("error", None)
                changed = True
                event = {"at": now.isoformat(), "alert": a["id"], "name": a.get("name") or describe(a), "symbol": sym,
                         "price": round(m.price, 4), "tf": a["tf"],
                         "message": f"{sym} {m.price:.2f}: {m.text}" + (f"{'' if m.text.endswith('.') else '.'} {a['note']}" if a.get("note") else "")}
                store.log(event)
                fired.append(event)
                if send and a.get("phone"):
                    send(f"Alert: {sym}" + (f" ({a['name']})" if a.get("name") else ""), event["message"])
                if a["trigger"] == "once":
                    a["status"] = "triggered"
                    break
        if changed:
            store.save(alerts)
    return fired


def checking_hours(now: datetime, overnight: bool = False) -> bool:
    """4:00 to 20:00 on weekdays; with `overnight`, all of the 24-hour market's week (Sunday 20:00 to Friday 20:00)."""
    if overnight:
        wd, t = now.weekday(), now.time()
        return wd in (0, 1, 2, 3) or (wd == 4 and t < time(20)) or (wd == 6 and t >= time(20))
    return now.weekday() < 5 and time(4, 0) <= now.time() < time(20, 0)
