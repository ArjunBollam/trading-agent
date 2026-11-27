"""Keeps 1-minute trend lines drawn as the day goes on, the way the first ones were drawn by hand: a falling line from a
swing high through the latest lower swing high (resistance), and a rising line from a swing low through the latest
higher swing low (support).

Each minute, on a symbol's settled 1-minute candles (the last three hours):
- swing points are candles with a higher high (lower low) than the 3 candles on each side;
- the down line runs from the earliest swing high, in the 45 minutes before the latest lower swing high, with no candle
  closing above the line between the two (the longest such line, so it starts at the top of the current leg, not at a
  pre-market high hours back), 8 minutes long at least; the up line likewise under swing lows;
- if the latest swing high isn't lower than one before it (a new high), or no line through it is still unbroken, the
  swing high before it is tried, and so on, three back at most. Only lines no close has gone through yet are drawn.

The lines are saved with your drawn ones (logs/drawings.json, ids starting "auto1m-"), so they get the chart's Break
and Retest marks, can carry alerts, and the position guard watches them. A line is redrawn through each new lower high
(higher low) until a candle closes through it; from then on it stays as it was, so its break stays on the chart, and its
first point is used up: the next line starts from a later swing point (no fans of lines from one point). The newest 3
on each side are kept, none older than 6 hours. A line you move or delete is yours: it's never redrawn or put back, nor
another from the same first point. Information only: nothing here places an order."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .features import Bar
from .trendlines import Drawings, market_clock, swings

PREFIX = "auto1m-"
WINDOW = 180  # candles looked at: the last three hours of 1-minute candles
SWING = 3  # candles each side of a swing point
MIN_GAP = 8  # minutes at least between a line's two points
LEG = timedelta(minutes=45)  # a line starts at most this long before its latest swing point: the top of the current leg
TOUCH_ATR = 0.15  # a swing point this close to the line (in average candle ranges) touches it
KEEP = 3  # lines kept on each side
MAX_AGE = timedelta(hours=6)
COLORS = {"down": "#e040fb", "up": "#00bcd4"}
CLOCK = "24h"

lock = threading.Lock()  # the page saves drawings too: one read-modify-write at a time in this process


@dataclass(frozen=True)
class Fit:
    side: str  # "down": resistance through swing highs; "up": support through swing lows
    a: Bar  # the first swing point
    pa: float
    b: Bar  # the latest swing point it's drawn through
    pb: float
    touches: int


def _price(bar: Bar, side: str) -> float:
    return bar.h if side == "down" else bar.l


def _at(t: datetime, a: Bar, pa: float, b: Bar, pb: float) -> float:
    ca, cb, c = (market_clock(x, CLOCK) for x in (a.t, b.t, t))
    return pa + (pb - pa) * (c - ca) / (cb - ca)


def current_lines(bars: list[Bar], used: dict[str, set[int]] | None = None) -> dict[str, Fit]:
    """The latest down line and up line on these 1-minute candles (settled ones only), if there's one each side, not
    starting from a point in `used` ({"down": {epoch seconds}, "up": ...}: first points of lines already broken)."""
    used = used or {}
    bars = bars[-WINDOW:]
    if len(bars) < 2 * SWING + MIN_GAP:
        return {}
    ranges = [b.h - b.l for b in bars]
    tol = TOUCH_ATR * sum(ranges) / len(ranges)
    highs, lows = swings(bars, SWING)
    out = {}
    for side, piv in (("down", highs), ("up", lows)):
        sign = 1 if side == "down" else -1  # +1: price stays under the line
        for z in range(len(piv) - 1, max(len(piv) - 4, 0) - 1, -1):  # the latest swing point, then up to 3 before it
            j = piv[z]
            pb = _price(bars[j], side)
            fit = None
            for i in piv[:z]:  # earliest first: the longest valid line wins
                if int(bars[i].t.timestamp()) in used.get(side, ()):
                    continue
                pa = _price(bars[i], side)
                if sign * (pa - pb) < tol or not timedelta(minutes=MIN_GAP) <= bars[j].t - bars[i].t <= LEG:
                    continue  # it has to fall (rise, for support) by more than a touch's width, over 8 to 45 minutes
                if any(sign * (bars[k].c - _at(bars[k].t, bars[i], pa, bars[j], pb)) > 0 for k in range(i + 1, j)):
                    continue  # a candle closed through it between its points
                if any(sign * (bars[k].c - _at(bars[k].t, bars[i], pa, bars[j], pb)) > 0 for k in range(j + 1, len(bars))):
                    continue  # already broken: too late to be a line to watch
                touches = sum(1 for k in piv if i <= k <= j and abs(_price(bars[k], side) - _at(bars[k].t, bars[i], pa, bars[j], pb)) <= tol)
                fit = Fit(side, bars[i], pa, bars[j], pb, touches)
                break
            if fit:
                out[side] = fit
                break
    return out


def _drawing(fit: Fit, did: str, tz) -> dict:
    word = "down" if fit.side == "down" else "up"
    return {"id": did, "kind": "trend", "t1": int(fit.a.t.timestamp()), "p1": round(fit.pa, 2),
            "t2": int(fit.b.t.timestamp()), "p2": round(fit.pb, 2), "clock": CLOCK, "extend": True,
            "name": f"1m {word} line from {fit.a.t.astimezone(tz):%H:%M} ({fit.touches} touches)", "color": COLORS[fit.side],
            "tf": "1m"}


def line_id(side: str, t1: int, t2: int) -> str:
    """"auto1m-d" or "auto1m-u", the first point's time and the minutes to the second, in hex: 20 characters, inside
    the 24 a saved line's id may have."""
    return f"{PREFIX}{side[0]}{t1:x}-{max(0, (t2 - t1) // 60):x}"


def _parse_id(did: str) -> tuple[str | None, int]:
    """The side and first point's time from a keeper line's id (line_id)."""
    try:
        head = did[len(PREFIX):].split("-")[0]
        return {"d": "down", "u": "up"}[head[0]], int(head[1:], 16)
    except (KeyError, IndexError, ValueError):
        return None, 0


def _coords(d: dict) -> list:
    return [int(d["t1"]), round(float(d["p1"]), 2), int(d["t2"]), round(float(d["p2"]), 2)]


def _closed_through(d: dict, side: str, bars: list[Bar], tz) -> bool:
    """Whether a candle after the line's second point closed through it."""
    a = Bar(datetime.fromtimestamp(d["t1"], tz), 0, 0, 0, 0, 0)
    b = Bar(datetime.fromtimestamp(d["t2"], tz), 0, 0, 0, 0, 0)
    sign = 1 if side == "down" else -1
    return any(sign * (x.c - _at(x.t, a, d["p1"], b, d["p2"])) > 0 for x in bars if x.t > b.t)


def merge_page_save(current: list[dict], posted: list[dict], seen: dict | None) -> list[dict]:
    """The page saves all of a symbol's lines at once, from its copy, which can be up to 10 seconds old. `seen` is the
    keeper's lines as the page last loaded them ({id: drawing}): a keeper line the page didn't change keeps its newest
    version here, one the page never saw is kept, and one you moved or deleted is taken as you left it. Without `seen`
    (an older page) the page's list is taken as it is."""
    if seen is None:
        return posted
    seen = {k: _coords(v) for k, v in seen.items() if isinstance(v, dict) and str(k).startswith(PREFIX)}
    now = {d["id"]: d for d in current if str(d.get("id", "")).startswith(PREFIX)}
    out = []
    for d in posted:
        did = str(d.get("id", ""))
        if not did.startswith(PREFIX):
            out.append(d)
        elif did in seen and _coords(d) == seen[did]:  # untouched on the page: the keeper's newest, if it still has it
            if did in now:
                out.append(now[did])
        else:
            out.append(d)  # you moved it (or it's new to the keeper)
    posted_ids = {str(d.get("id", "")) for d in posted}
    out += [d for did, d in now.items() if did not in posted_ids and did not in seen]  # drawn since the page last looked
    return out


class LineKeeper:
    """logs/linekeeper.json remembers which lines it drew (and where), so a line you moved or deleted is left alone."""

    def __init__(self, folder: Path):
        self.path = Path(folder) / "linekeeper.json"
        self.drawings = Drawings(Path(folder) / "drawings.json")

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, state: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1))
        os.replace(tmp, self.path)

    def run(self, symbol: str, bars: list[Bar], tz, now: datetime) -> list[str]:
        """One pass on a symbol's 1-minute candles. Returns what changed, in words."""
        bars = [b for b in bars if b.t + timedelta(minutes=1, seconds=20) <= now]  # settled closes only
        said: list[str] = []
        with lock:
            state = self.load()
            mine: dict = state.setdefault(symbol, {}).setdefault("lines", {})  # id -> {"side", "at": coords we wrote}
            gone: list = state[symbol].setdefault("gone", [])  # ids of lines you deleted or moved
            drawn = self.drawings.get(symbol)
            by_id = {d["id"]: dict(d) for d in drawn}  # copies: `drawn` stays as saved, to see what changed
            # what you did to them since: deleted ones stay deleted, moved ones are yours; either way their first point
            # starts no other line
            for did in list(mine):
                if did not in by_id or _coords(by_id[did]) != mine[did]["at"]:
                    gone.append(did)
                    del mine[did]
            # first points used up, kept for 6 hours: those of lines that broke, and of lines you deleted
            kept_used = state[symbol].setdefault("used", {"down": [], "up": []})
            for did, m in mine.items():
                if _closed_through(by_id[did], m["side"], bars, tz) and int(by_id[did]["t1"]) not in kept_used[m["side"]]:
                    kept_used[m["side"]].append(int(by_id[did]["t1"]))
            for side in kept_used:
                kept_used[side] = [t for t in kept_used[side] if now.timestamp() - t < MAX_AGE.total_seconds() * 2]
            used = {side: set(ts) for side, ts in kept_used.items()}
            for g in gone:
                side, t1 = _parse_id(g)
                if side:
                    used[side].add(t1)
            fits = current_lines(bars, used)
            for side in ("down", "up"):
                fit = fits.get(side)
                if fit is None:
                    continue
                ours = [did for did in mine if mine[did]["side"] == side]
                active = ours[-1] if ours else None
                fresh = _drawing(fit, "", tz)
                if active and by_id[active]["t1"] == fresh["t1"] and not _closed_through(by_id[active], side, bars, tz):
                    if _coords(by_id[active]) != _coords(fresh):  # redrawn through the new swing point
                        by_id[active].update({k: fresh[k] for k in ("p1", "t2", "p2", "name")})
                        mine[active]["at"] = _coords(by_id[active])
                        said.append(f"{symbol}: redrew {fresh['name']}")
                    continue
                if active and _coords(by_id[active]) == _coords(fresh):
                    continue
                did = line_id(side, fresh["t1"], fresh["t2"])
                if did in by_id:
                    continue
                for old in [x for x in mine if mine[x]["side"] == side]:
                    # one never broken has no break on the chart to keep: the new line replaces it
                    o = by_id[old]
                    if o["t1"] == fresh["t1"] or not _closed_through(o, side, bars, tz):
                        by_id.pop(old)
                        del mine[old]
                fresh["id"] = did
                by_id[did] = fresh
                mine[did] = {"side": side, "at": _coords(fresh)}
                said.append(f"{symbol}: drew {fresh['name']}")
            # the newest few each side, none older than 6 hours
            for side in ("down", "up"):
                ours = [did for did in mine if mine[did]["side"] == side]
                for did in ours[:-KEEP] + [x for x in ours[-KEEP:] if now.timestamp() - by_id[x]["t2"] > MAX_AGE.total_seconds()]:
                    by_id.pop(did, None)
                    mine.pop(did, None)
            state[symbol]["gone"] = gone[-200:]
            out = [by_id[d["id"]] for d in drawn if d["id"] in by_id] + [d for k, d in by_id.items() if k not in {x["id"] for x in drawn}]
            if out != drawn:
                self.drawings.save(symbol, out, tz)
            self._write(state)
        return said
