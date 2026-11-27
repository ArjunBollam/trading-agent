"""Position guard: watches each of your Robinhood positions (from the scanner's state) on 1-minute candles against the
lines drawn on its symbol, and says when one breaks with you or against you. Information only: it places nothing.

On each newly closed 1-minute candle, for every line:
- a close through a floor line (for calls or shares held long: a horizontal or rising line below price; mirrored for puts
  or a short): "against", and if it was the nearest one ("the opposing line"), "exit": the video's rule says the trade
  is wrong once the opposing line breaks;
- a close back under a falling line (over a rising one, for a bearish trade): "failed", a break that didn't hold;
- a close through a line with the position: "with";
- a high (low for a bearish position) within 0.10 of the nearest horizontal level ahead: "target", where the video
  takes profit a few cents before.
Each says which candle, its close and the line's price there.

Each pass also gives every position a verdict by the same rule: "hold" while the last close is on the trade's side of
its floor line, "exit" from an EXIT signal until a candle closes back over that line, "no_floor" with no line under the
trade. While it's "hold", a check-in ("hold" event) goes out every 15 minutes of regular hours, with the floor, the
next level, the P&L and, for options expiring today, the time to the close and where price is against the strike."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .features import Bar
from .trendlines import Line, line_from_json

TARGET_NEAR = 0.10
CHECKIN_SECONDS = 900  # a HOLD check-in every 15 minutes, on the quarter hour
WITH_PUSH_SECONDS = 300  # at most one "with you" push per position per 5 minutes (a choppy line flips every candle)


@dataclass(frozen=True)
class Position:
    key: str
    symbol: str
    label: str
    bullish: bool
    mark: float | None
    pnl: float | None
    pnl_pct: float | None = None
    strike: float | None = None
    expiration: date | None = None
    call: bool | None = None  # None for shares


def _day(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def positions(state: dict) -> list[Position]:
    out = []
    for h in state.get("holdings") or []:
        qty = float(h.get("qty") or 0)
        if qty:
            out.append(Position(f"{h['symbol']}:shares", h["symbol"], f"{'long' if qty > 0 else 'short'} {abs(qty):g} {h['symbol']}",
                                qty > 0, h.get("price"), h.get("pnl"), h.get("pnl_pct")))
    for o in state.get("options") or []:
        qty = float(o.get("qty") or 0)
        if not qty:
            continue
        call = str(o.get("right", "")).lower().startswith("c")
        label = f"{abs(qty):g}× {o['symbol']} {float(o['strike']):g} {o.get('right', '')} {str(o.get('expiration', ''))[5:]}"
        out.append(Position(f"{o['symbol']}:{o.get('strike')}:{o.get('right')}:{o.get('expiration')}", o["symbol"],
                            ("short " if qty < 0 else "") + label, call == (qty > 0), o.get("mark"), o.get("pnl"),
                            o.get("pnl_pct"), float(o["strike"]), _day(o.get("expiration")), call))
    return out


def _hm(t: datetime) -> str:
    return t.strftime("%H:%M")


def _holds_for(line: Line, bullish: bool) -> bool:
    """Whether a line can be the trade's floor (its opposing line): for calls or shares held long, a horizontal line or a
    rising one (support); for puts or a short, a horizontal or falling one. A falling line over a long trade is
    resistance: price going back under it is a failed break, not the trade's floor giving way."""
    if line.kind != "trend":
        return True
    return line.p2 >= line.p1 if bullish else line.p2 <= line.p1


def check(pos: Position, bars: list[Bar], lines: list[Line]) -> tuple[dict, list[dict]]:
    """The position's status now (its opposing line and the next level ahead) and what the last closed candle did."""
    status = {"position": pos.label, "symbol": pos.symbol, "bullish": pos.bullish, "mark": pos.mark, "pnl": pos.pnl,
              "against": None, "ahead": None, "price": bars[-1].c if bars else None}
    if len(bars) < 2:
        return status, []
    prev, last = bars[-2], bars[-1]
    sign = 1 if pos.bullish else -1  # +1: the floor is below, targets above
    priced = [(ln, ln.price_at(prev.t), ln.price_at(last.t)) for ln in lines]
    priced = [(ln, a, b) for ln, a, b in priced if a is not None and b is not None]
    floors_before = [(ln, a) for ln, a, _ in priced if _holds_for(ln, pos.bullish) and sign * (prev.c - a) >= 0]
    opposing = max(floors_before, key=lambda x: sign * x[1])[0] if floors_before else None
    floors_now = [(ln, b) for ln, _, b in priced if _holds_for(ln, pos.bullish) and sign * (last.c - b) >= 0]
    # levels ahead of price that no close in the last 15 minutes went past: a floor that just broke is resistance now,
    # not a target for the trade
    recent = bars[-16:]
    levels_ahead = [(ln, b) for ln, a, b in priced if ln.kind != "trend" and sign * (b - last.c) > 0
                    and all(sign * (ln.price_at(x.t) - x.c) > 0 for x in recent if ln.price_at(x.t) is not None)]
    recent_ranges = [x.h - x.l for x in bars[-15:]]
    status["typical"] = round(sum(recent_ranges) / len(recent_ranges), 2)  # an average 1-minute candle, high to low
    status["t"] = int(last.t.timestamp())
    if floors_now:
        ln, b = max(floors_now, key=lambda x: sign * x[1])
        status["against"] = {"name": ln.called, "id": ln.id, "price": round(b, 2), "distance": round(abs(last.c - b), 2)}
    if levels_ahead:
        ln, b = min(levels_ahead, key=lambda x: sign * x[1])
        status["ahead"] = {"name": ln.called, "id": ln.id, "price": round(b, 2), "distance": round(abs(b - last.c), 2)}
    events = []
    word, other = ("below", "above") if pos.bullish else ("above", "below")
    side = "under" if pos.bullish else "over"
    for ln, a, b in priced:
        went_against = sign * (prev.c - a) >= 0 and sign * (last.c - b) < 0
        went_with = sign * (prev.c - a) <= 0 and sign * (last.c - b) > 0
        t = int(last.t.timestamp())
        if went_against and _holds_for(ln, pos.bullish):
            if opposing is not None and opposing.id == ln.id:
                events.append({"kind": "exit", "line": ln.id, "t": t,
                               "text": f"EXIT signal for your {pos.label}: the {_hm(last.t)} candle closed at {last.c:.2f}, {word} "
                                       f"{ln.called} ({b:.2f} there), the nearest line {side} the trade. By the video's rule the "
                                       "trade is wrong once that line breaks."})
            else:
                events.append({"kind": "against", "line": ln.id, "t": t,
                               "text": f"Against your {pos.label}: the {_hm(last.t)} candle closed at {last.c:.2f}, {word} "
                                       f"{ln.called} ({b:.2f} there)."})
        elif went_against:
            events.append({"kind": "failed", "line": ln.id, "t": t,
                           "text": f"Failed break for your {pos.label}: the {_hm(last.t)} candle closed at {last.c:.2f}, back "
                                   f"{word} {ln.called} ({b:.2f} there)."})
        elif went_with:
            events.append({"kind": "with", "line": ln.id, "t": t,
                           "text": f"With your {pos.label}: the {_hm(last.t)} candle closed at {last.c:.2f}, {other} "
                                   f"{ln.called} ({b:.2f} there)."})
    if levels_ahead:
        ln, b = min(levels_ahead, key=lambda x: sign * x[1])
        reach = last.h if pos.bullish else last.l
        gap = (b - reach) if pos.bullish else (reach - b)
        if gap <= TARGET_NEAR:
            how = (f"reached {ln.called} ({b:.2f})" if gap <= 0 else f"came within {gap:.2f} of {ln.called} ({b:.2f})")
            events.append({"kind": "target", "line": ln.id, "t": int(last.t.timestamp()),
                           "text": f"Target zone for your {pos.label}: the {_hm(last.t)} candle's {'high' if pos.bullish else 'low'} "
                                   f"{reach:.2f} {how}; the video takes profit a few cents before a level like this."})
    return status, events


def _left(span: timedelta) -> str:
    minutes = max(0, int(span.total_seconds() // 60))
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def expiry_note(pos: Position, price: float, now: datetime, close_at: datetime | None) -> str:
    """For an option that expires today: the time to today's close and where price is against the strike."""
    if pos.strike is None or pos.expiration != now.date():
        return ""
    when = (f"the market closes at {close_at:%H:%M} ET ({_left(close_at - now)} left)" if close_at and close_at > now
            else "the market closes today")
    otm = price < pos.strike if pos.call else price > pos.strike
    where = (f"{pos.symbol} is {abs(price - pos.strike):.2f} {'below' if price < pos.strike else 'above'} the {pos.strike:g} "
             f"strike ({'out of' if otm else 'in'} the money)")
    return f" These expire today: {when}, and {where}."


def verdict(pos: Position, status: dict, broken: dict | None, lines: list[Line], now: datetime,
            close_at: datetime | None) -> dict:
    """What the rule says about the trade on its last closed candle: hold, exit (stands until a close back over the broken
    line) or no floor. `broken` is the EXIT this position is still under, if any."""
    price, t = status.get("price"), status.get("t")
    if price is None or t is None:
        return {"kind": "none", "short": "no candles yet", "text": ""}
    at = _hm(datetime.fromtimestamp(t, now.tzinfo))
    word, other = ("below", "above") if pos.bullish else ("above", "below")
    side = "under" if pos.bullish else "over"
    money = ""
    if pos.pnl is not None:
        money = f" {'Up' if pos.pnl >= 0 else 'Down'} ${abs(pos.pnl):,.2f}" + (
            f" ({pos.pnl_pct:+.1f}%)" if pos.pnl_pct is not None else "") + " at Robinhood's last mark."
    expiry = expiry_note(pos, price, now, close_at)
    if broken:
        ln = next((x for x in lines if x.id == broken["line"]), None)
        there = ln.price_at(datetime.fromtimestamp(t, now.tzinfo)) if ln else None
        name = ln.called if ln else broken.get("name", "the line")
        since = _hm(datetime.fromtimestamp(broken["t"], now.tzinfo))
        return {"kind": "exit", "short": f"EXIT stands since {since}: {word} {name}",
                "text": f"EXIT stands for your {pos.label}: the {since} candle closed {word} {name} and no 1-minute candle has "
                        f"closed back {other} it since (last close {price:.2f} at {at}"
                        + (f", the line {there:.2f})." if there is not None else ").") + money + expiry}
    floor, ahead = status.get("against"), status.get("ahead")
    if not floor:
        return {"kind": "no_floor", "short": f"no line {side} the trade",
                "text": f"No line {side} your {pos.label}, so the rule has nothing to hold against. Draw one, or turn on "
                        "auto lines, to get hold and exit calls." + money + expiry}
    typical = status.get("typical") or 0
    near = floor["distance"] < typical
    text = (f"HOLD by the video's rule: the {at} candle closed at {price:.2f}, {floor['distance']:.2f} {other} "
            f"{_with_price(floor['name'], floor['price'])}, the line {side} your {pos.label}. The rule says the trade stays right "
            f"while 1-minute candles close {other} that line.")
    if near:
        text += f" Close to it: that's less than one average 1-minute candle ({typical:.2f})."
    if ahead:
        text += (f" Next level: {_with_price(ahead['name'], ahead['price'])}, {ahead['distance']:.2f} away; the video takes "
                 "profit a few cents before a level like this.")
    return {"kind": "hold", "near": near, "short": f"HOLD: {floor['distance']:.2f} {other} {floor['name']}",
            "text": text + money + expiry}


def _with_price(name: str, price: float) -> str:
    """A line's name with its price, unless the name already says it ("Today's low 720.50")."""
    return name if f"{price:.2f}" in name else f"{name} ({price:.2f})"


def regular_now(now: datetime, close_at: datetime | None) -> bool:
    """Regular hours, 9:30 to today's close (13:00 on half days), when check-ins go out."""
    end = close_at.time() if close_at and close_at.date() == now.date() else time(16)
    return now.weekday() < 5 and time(9, 30) <= now.time() < end


class Guard:
    """logs/guard.json (each position's status and the latest events) and logs/guard_log.jsonl (every event)."""

    def __init__(self, folder: Path):
        self.path = Path(folder) / "guard.json"
        self.log_path = Path(folder) / "guard_log.jsonl"

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {"updated": None, "positions": [], "events": [], "seen": []}

    def run(self, state: dict, candles, drawings, tz, now: datetime, send=None, close_at: datetime | None = None,
            note=None) -> list[dict]:
        """One pass: `candles(symbol)` gives 1-minute candles, `drawings(symbol)` the saved lines, `close_at` today's
        close from the exchange calendar (None: unknown, 16:00 assumed for check-ins), `note(symbol, position)` words
        added to a HOLD check-in (Claude's latest read, paperbot/coach.py). Returns new events."""
        saved = self.load()
        seen = set(saved.get("seen") or [])
        broken = dict(saved.get("broken") or {})  # position -> the EXIT it's still under: {"line", "name", "t"}
        checkins = dict(saved.get("checkins") or {})  # position -> the quarter hour of its last HOLD check-in
        pushed = dict(saved.get("pushed") or {})  # position -> when its last "with you" push went out
        held = set()
        statuses, new = [], []
        for pos in positions(state):
            held.add(pos.key)
            try:
                bars = [b for b in candles(pos.symbol) if b.t + timedelta(minutes=1, seconds=20) <= now]  # settled closes only
            except Exception as e:  # no candles this pass: say so, try again next time
                statuses.append({"position": pos.label, "symbol": pos.symbol, "error": str(e)[:200]})
                continue
            lines = []
            for d in drawings(pos.symbol):
                try:
                    lines.append(line_from_json(d, tz))
                except ValueError:
                    continue
            status, events = check(pos, bars, lines)
            # the EXIT stands from the first floor that broke until a close back over that line (or the line is deleted)
            for e in events:
                if e["kind"] == "exit" and pos.key not in broken:
                    broken[pos.key] = {"line": e["line"], "name": next((x.called for x in lines if x.id == e["line"]), ""),
                                       "t": e["t"]}
            memo = broken.get(pos.key)
            if memo and bars:
                ln = next((x for x in lines if x.id == memo["line"]), None)
                there = ln.price_at(bars[-1].t) if ln else None
                sign = 1 if pos.bullish else -1
                if there is None or sign * (bars[-1].c - there) > 0:
                    broken.pop(pos.key)
                    for e in events:
                        if e["kind"] == "with" and e["line"] == memo["line"]:
                            e["text"] += f" The EXIT from {_hm(datetime.fromtimestamp(memo['t'], tz))} no longer stands by the rule."
            status["verdict"] = verdict(pos, status, broken.get(pos.key), lines, now, close_at)
            status["expires_today"] = pos.expiration == now.date() if pos.strike is not None else False
            status["close"] = f"{close_at:%H:%M}" if close_at else None
            statuses.append(status)
            # a HOLD check-in every quarter hour of regular hours, on a fresh candle, until an option's last day closes
            slot = int(now.timestamp()) // CHECKIN_SECONDS
            fresh = bool(bars) and now - bars[-1].t <= timedelta(minutes=3)
            if (status["verdict"]["kind"] == "hold" and fresh and regular_now(now, close_at)
                    and checkins.get(pos.key) != slot and not (pos.expiration and pos.expiration < now.date())):
                checkins[pos.key] = slot
                extra = note(pos.symbol, pos.label) if note else ""
                events.append({"kind": "hold", "line": status["against"]["id"], "t": status["t"],
                               "text": status["verdict"]["text"] + (extra or "")})
            withs, holds = [], []
            for e in events:
                key = f"{pos.key}|{e['line']}|{e['kind']}|{e['t']}"
                if e["kind"] == "target":  # once per line per 10 minutes
                    key = f"{pos.key}|{e['line']}|target|{e['t'] // 600}"
                if e["kind"] == "hold":
                    key = f"{pos.key}|hold|{slot}"
                if key in seen:
                    continue
                seen.add(key)
                e |= {"at": now.isoformat(), "symbol": pos.symbol, "position": pos.label}
                new.append(e)
                with self.log_path.open("a") as f:
                    f.write(json.dumps(e) + "\n")
                if not send:
                    continue
                if e["kind"] in ("exit", "against", "target"):
                    title = {"exit": "EXIT signal", "against": "Against you", "target": "Target zone"}[e["kind"]]
                    send(f"{title}: {pos.symbol}", e["text"], "high" if e["kind"] == "exit" else "default")
                elif e["kind"] in ("with", "hold"):
                    (withs if e["kind"] == "with" else holds).append(e)
            # breaks in your favour: one push for all the lines a candle crossed, at most every 5 minutes; then the check-in
            if send and withs and now.timestamp() - pushed.get(pos.key, 0) >= WITH_PUSH_SECONDS:
                pushed[pos.key] = now.timestamp()
                more = f" (and {len(withs) - 1} more line{'s' if len(withs) > 2 else ''})" if len(withs) > 1 else ""
                send(f"With you: {pos.symbol}", withs[0]["text"] + more, "default")
            for e in holds if send else []:
                send(f"HOLD: {pos.label}" + (" (close to the floor)" if status["verdict"].get("near") else ""), e["text"], "default")
        # positions you no longer hold: forget their EXITs and check-ins
        broken = {k: v for k, v in broken.items() if k in held}
        checkins = {k: v for k, v in checkins.items() if k in held}
        pushed = {k: v for k, v in pushed.items() if k in held}
        events = (new[::-1] + (saved.get("events") or []))[:60]
        out = {"updated": now.isoformat(), "positions": statuses, "events": events, "seen": sorted(seen)[-2000:],
               "broken": broken, "checkins": checkins, "pushed": pushed}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(out))
        os.replace(tmp, self.path)
        return new
