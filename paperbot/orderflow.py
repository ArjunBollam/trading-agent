"""Order flow: which side was aggressive inside each candle, read from the trade tape.

Three sources, as this module uses them:

  Fabio Valentini, "The Only Orderflow Guide You'll Ever Need": market state first (in balance, or
    out of balance and trending), then location (the session's volume profile: point of control,
    value area, low-volume nodes), then aggression at that location. Two models: trend continuation
    on a pullback, and a failed breakout back into value that targets the point of control.
  Axia Futures, "How To Start Using Footprint Charts": the footprint (volume at each price, split
    into buyers and sellers), delta, diagonal imbalances, stacked imbalances and exhaustion.
  Bookmap, "Layering and Absorption": absorption is repeated aggressive execution at a level
    without the price moving. Don't act on it alone: wait for delta and CVD to turn and for price
    to leave the level.

What this can and can't see:
  - Aggressor side comes from the tick rule: a trade above the last different price counts as
    bought at the offer, one below it as sold at the bid. Footprint platforms use the quote at
    the moment of each trade, which this can't afford over Alpaca's REST API.
  - Alpaca's free plan gives live trades from IEX only, a few percent of the volume. That's a
    sample of the tape. The full tape (SIP) needs a paid plan to use live; it is free 15 minutes
    late, which is enough for `paperbot orderflow` studies. orderflow.feed picks one.
  - No order book. Layering (stacked resting orders on Bookmap's heatmap) needs depth-of-book
    data, which Alpaca doesn't offer for stocks, so it isn't detected. Absorption is inferred from
    the tape: heavy aggression into a level that doesn't break.

Information for the AI and alerts. Nothing here places an order.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

SESSION_OPEN, SESSION_CLOSE = time(9, 30), time(16, 0)
OPENING_RANGE_END = time(9, 45)

# Sale conditions that aren't someone crossing the spread: auction and official open/close prints,
# average-price and derivatively priced trades, out-of-sequence and late reports, cash and next-day
# settlement, contingent trades.
NOT_AGGRESSIVE = frozenset({"B", "C", "H", "M", "N", "O", "P", "Q", "R", "T", "U", "V", "W", "Z", "4", "6", "7", "9"})

# Footprint row heights to choose from: about 0.6 basis points of price (SPY near 750: 0.05).
LADDER = (0.01, 0.02, 0.05, 0.1, 0.25, 0.5, 1.0)

LABELS = {
    "of_absorption_bull": "Absorption at support (order flow)",
    "of_absorption_bear": "Absorption at resistance (order flow)",
    "of_absorption_long": "Absorption confirmed: long",
    "of_absorption_short": "Absorption confirmed: short",
    "of_stacked_buy": "Stacked buy imbalances",
    "of_stacked_sell": "Stacked sell imbalances",
    "of_divergence_bull": "Delta divergence at a low",
    "of_divergence_bear": "Delta divergence at a high",
    "of_exhaustion_low": "Selling exhaustion at the low",
    "of_exhaustion_high": "Buying exhaustion at the high",
    "of_trend_long": "Trend continuation long (order flow)",
    "of_trend_short": "Trend continuation short (order flow)",
    "of_failed_breakdown": "Failed breakdown back into value",
    "of_failed_breakout": "Failed breakout back into value",
}
# Signals with an entry, a stop and a target. The rest are context: something to watch.
SETUPS = frozenset({"of_absorption_long", "of_absorption_short", "of_trend_long", "of_trend_short",
                    "of_failed_breakdown", "of_failed_breakout"})


def auto_step(price: float) -> float:
    want = price * 0.00006
    return next((s for s in LADDER if s >= want), LADDER[-1])


@dataclass(frozen=True)
class Trade:
    t: datetime
    price: float
    size: float
    side: int = 0  # +1: bought at the offer (buyer aggressive), -1: sold at the bid, 0: not known


def trade_from_row(row: dict) -> Trade | None:
    """An Alpaca trade row ({"t", "p", "s", "c", ...}), or None for a print that isn't aggression."""
    if NOT_AGGRESSIVE & set(row.get("c") or ()):
        return None
    t = row["t"] if isinstance(row["t"], datetime) else datetime.fromisoformat(str(row["t"]).replace("Z", "+00:00"))
    size = float(row["s"])
    return Trade(t, float(row["p"]), size) if size > 0 else None


@dataclass
class FlowBar:
    """One candle's footprint: OHLC plus volume at each price row, split into sellers and buyers."""

    t: datetime  # bar start, market time
    step: float
    o: float = 0.0
    h: float = -math.inf
    l: float = math.inf  # noqa: E741
    c: float = 0.0
    buy: float = 0.0
    sell: float = 0.0
    other: float = 0.0  # side unknown: the first prints of the session
    pv: float = 0.0  # price x volume, for VWAP
    cvd: float = 0.0  # the session's cumulative delta at this bar's close
    rows: dict[int, list[float]] = field(default_factory=dict)  # round(price / step) -> [sold at bid, bought at offer]

    @property
    def volume(self) -> float:
        return self.buy + self.sell + self.other

    @property
    def delta(self) -> float:
        return self.buy - self.sell

    @property
    def range(self) -> float:
        return self.h - self.l

    def add(self, tr: Trade) -> None:
        if self.h == -math.inf:
            self.o = tr.price
        self.h, self.l, self.c = max(self.h, tr.price), min(self.l, tr.price), tr.price
        self.pv += tr.price * tr.size
        row = self.rows.setdefault(round(tr.price / self.step), [0.0, 0.0])
        if tr.side > 0:
            self.buy += tr.size
            row[1] += tr.size
        elif tr.side < 0:
            self.sell += tr.size
            row[0] += tr.size
        else:
            self.other += tr.size

    def row(self, price: float) -> tuple[float, float]:
        """(sold at bid, bought at offer) at the row holding `price`."""
        s, b = self.rows.get(round(price / self.step), (0.0, 0.0))
        return s, b


class Tape:
    """One session's regular-hours trades for one symbol, signed with the tick rule and folded into
    footprint bars as they arrive. Feed it in time order; it can be topped up any number of times."""

    def __init__(self, day: date, bar_minutes: int, tz: ZoneInfo, step: float | None = None):
        self.day, self.bar_minutes, self.tz, self.step = day, bar_minutes, tz, step
        self.open = datetime.combine(day, SESSION_OPEN, tzinfo=tz)
        self.close = datetime.combine(day, SESSION_CLOSE, tzinfo=tz)
        self.bars: list[FlowBar] = []
        self.last_t: datetime | None = None  # the newest trade seen, in or out of session
        self.trades = 0
        self._price: float | None = None
        self._side = 0
        self._cvd = 0.0

    def add(self, rows: Iterable[dict | Trade]) -> int:
        n = 0
        for r in rows:
            tr = r if isinstance(r, Trade) else trade_from_row(r)
            if tr is None:
                continue
            self.last_t = tr.t if self.last_t is None else max(self.last_t, tr.t)
            local = tr.t.astimezone(self.tz)
            if not self.open <= local < self.close:
                continue
            side = tr.side
            if side == 0:  # tick rule; an unchanged price keeps the last side
                side = self._side if self._price is None or tr.price == self._price else (1 if tr.price > self._price else -1)
            self._price = tr.price
            self._side = side or self._side
            if self.step is None:
                self.step = auto_step(tr.price)
            start = self.open + timedelta(minutes=(local - self.open).seconds // 60 // self.bar_minutes * self.bar_minutes)
            if not self.bars or start > self.bars[-1].t:
                self.bars.append(FlowBar(start, self.step))
            bar = self.bars[-1]
            bar.add(Trade(tr.t, tr.price, tr.size, side))
            self._cvd += tr.size * side
            bar.cvd = self._cvd
            n += 1
        self.trades += n
        return n

    def closed(self, now: datetime) -> list[FlowBar]:
        span = timedelta(minutes=self.bar_minutes)
        return [b for b in self.bars if b.t + span <= now]


@dataclass(frozen=True)
class Profile:
    """Where the session traded most: the point of control, the value area (70% of volume around it)
    and low-volume nodes, the thin prices price tends to move through quickly."""

    poc: float
    vah: float
    val: float
    lvns: tuple[float, ...]


def volume_profile(bars: list[FlowBar], row: float, value_share: float = 0.7) -> Profile | None:
    hist: dict[int, float] = {}
    for b in bars:
        for k, (s, bought) in b.rows.items():
            key = round(k * b.step / row)
            hist[key] = hist.get(key, 0.0) + s + bought
    if not hist:
        return None
    lo, hi = min(hist), max(hist)
    vols = [hist.get(k, 0.0) for k in range(lo, hi + 1)]
    total = sum(vols)
    if total <= 0:
        return None
    poc = max(range(len(vols)), key=vols.__getitem__)
    a = b = poc
    inside = vols[poc]
    while inside < value_share * total and (a > 0 or b < len(vols) - 1):
        up = vols[b + 1] if b < len(vols) - 1 else -1.0
        down = vols[a - 1] if a > 0 else -1.0
        if up >= down:
            b, inside = b + 1, inside + up
        else:
            a, inside = a - 1, inside + down
    smooth = [sum(vols[max(0, i - 1):i + 2]) / len(vols[max(0, i - 1):i + 2]) for i in range(len(vols))]
    mean = total / len(vols)
    lvns = tuple(round((lo + i) * row, 4) for i in range(1, len(vols) - 1)
                 if smooth[i] < 0.5 * mean and smooth[i] <= smooth[i - 1] and smooth[i] <= smooth[i + 1])
    return Profile(round((lo + poc) * row, 4), round((lo + b) * row, 4), round((lo + a) * row, 4), lvns)


@dataclass(frozen=True)
class Settings:
    delta_multiple: float = 1.5  # heavy aggression: |delta| at least this many times the recent average
    imbalance_ratio: float = 2.5  # diagonal imbalance; Axia uses 300% on futures, stocks are deeper
    stacked_levels: int = 3  # consecutive imbalanced rows for a stack
    confirm_bars: int = 3  # bars an absorption has to be confirmed in
    lookback: int = 20  # bars that set "normal" delta, range and volume
    min_bars: int = 6  # no signals until this many bars of the session exist


@dataclass(frozen=True)
class FlowSignal:
    kind: str  # a key of LABELS
    side: str  # buy | sell
    bar_time: datetime
    price: float
    detail: str
    level: str | None = None
    weak: bool = False  # not at a key level: logged and shown, not alerted
    entry: float | None = None
    stop: float | None = None
    target: float | None = None

    @property
    def label(self) -> str:
        return LABELS[self.kind]

    @property
    def setup(self) -> bool:
        return self.kind in SETUPS


def _num(x: float) -> str:
    return f"{x:+,.0f}"


def _px(x: float) -> str:
    return f"{x:.2f}"


def key_levels(bars: list[FlowBar], prior: dict[str, float], vwap: float | None, profile: Profile | None) -> list[tuple[str, float]]:
    """Named prices from before this bar: the prior session's high, low and close, VWAP, the
    opening range once complete, and the profile's point of control, value area and thin nodes."""
    out = [(name, p) for name, p in prior.items() if p]
    if vwap:
        out.append(("VWAP", vwap))
    or_bars = [b for b in bars if b.t.time() < OPENING_RANGE_END]
    if or_bars and len(or_bars) < len(bars):  # complete once a bar after 9:45 exists
        out += [("opening range high", max(b.h for b in or_bars)), ("opening range low", min(b.l for b in or_bars))]
    if profile:
        out += [("point of control", profile.poc), ("value area high", profile.vah), ("value area low", profile.val)]
        out += [("low-volume node", p) for p in profile.lvns]
    return out


def _near(levels: list[tuple[str, float]], price: float, tol: float) -> tuple[str, float] | None:
    best = min(levels, key=lambda lv: abs(lv[1] - price), default=None)
    return best if best is not None and abs(best[1] - price) <= tol else None


def _next_level(levels: list[tuple[str, float]], price: float, up: bool, beyond: float) -> tuple[str, float] | None:
    ahead = [lv for lv in levels if (lv[1] > price + beyond if up else lv[1] < price - beyond)]
    return min(ahead, key=lambda lv: abs(lv[1] - price), default=None)


def _stack(bar: FlowBar, buyers: bool, ratio: float, need: int) -> tuple[float, float] | None:
    """The longest run of diagonal imbalances, Axia style: a buy imbalance compares buying at a row
    with selling one row below; a sell imbalance, selling at a row with buying one row above."""
    if not bar.rows:
        return None
    mean_row = (bar.buy + bar.sell) / len(bar.rows)
    best: tuple[int, int] | None = None
    run_start = None
    keys = range(min(bar.rows), max(bar.rows) + 2)
    for k in keys:
        s, b = bar.rows.get(k, (0.0, 0.0))
        if buyers:
            opposite = bar.rows.get(k - 1, (0.0, 0.0))[0]
            hit = b >= mean_row and b >= ratio * opposite
        else:
            opposite = bar.rows.get(k + 1, (0.0, 0.0))[1]
            hit = s >= mean_row and s >= ratio * opposite
        if hit:
            run_start = k if run_start is None else run_start
            if best is None or k - run_start > best[1] - best[0]:
                best = (run_start, k)
        else:
            run_start = None
    if best and best[1] - best[0] + 1 >= need:
        return best[0] * bar.step, best[1] * bar.step
    return None


def analyze(bars: list[FlowBar], prior: dict[str, float] | None = None, s: Settings = Settings()) -> list[FlowSignal]:
    """Signals on every bar, each judged only on the bars before it (no look-ahead), oldest first.
    prior: levels known before the session, e.g. {"prior high": 755.2, "prior low": 748.9}."""
    prior = prior or {}
    out: list[FlowSignal] = []
    pending: list[tuple[int, str, float, str | None, bool]] = []  # absorption awaiting confirmation
    fired: dict[str, int] = {}  # context kind -> bar index it last fired on
    pv = vol = 0.0
    for i, b in enumerate(bars):
        before = bars[:i]
        vwap = pv / vol if vol else None
        pv, vol = pv + b.pv, vol + b.volume
        hist = before[-s.lookback:]
        if len(hist) < s.min_bars or b.volume <= 0:
            continue
        step = b.step
        avg_delta = sum(abs(h.delta) for h in hist) / len(hist) or 1.0
        avg_range = sum(h.range for h in hist) / len(hist) or step
        tol = max(2 * step, 0.25 * avg_range)
        profile = volume_profile(before, step * 5)
        levels = key_levels(before, prior, vwap, profile)
        major = [lv for lv in levels if lv[0] != "low-volume node"]  # thin nodes only matter to the trend model
        heavy_sell = b.delta <= -s.delta_multiple * avg_delta and b.sell >= 0.55 * (b.buy + b.sell)
        heavy_buy = b.delta >= s.delta_multiple * avg_delta and b.buy >= 0.55 * (b.buy + b.sell)
        found: list[FlowSignal] = []

        def add(kind, side, detail, level=None, weak=False, entry=None, stop=None, target=None):
            if kind not in SETUPS:  # context signals: once per 6 bars, so a slow grind doesn't repeat one every bar
                if i - fired.get(kind, -99) < 6:
                    return
                fired[kind] = i
            found.append(FlowSignal(kind, side, b.t, b.c, detail, level, weak,
                                    None if entry is None else round(entry, 2), None if stop is None else round(stop, 2),
                                    None if target is None else round(target, 2)))

        def plan(entry: float, stop: float, up: bool) -> tuple[float, str]:
            lv = _next_level(levels, entry, up, abs(entry - stop) * 0.5)
            if lv:
                return lv[1], lv[0]
            return entry + (2 if up else -2) * abs(entry - stop), "2R"

        # Confirmation of an earlier absorption (Bookmap: don't enter on absorption alone).
        still = []
        for (j, side, lvl, name, weak) in pending:
            a = bars[j]
            if side == "buy" and b.c < lvl - tol or side == "sell" and b.c > lvl + tol:
                continue  # the level broke: the absorption failed
            if side == "buy" and b.delta > 0 and b.c > a.h and b.cvd > a.cvd:
                stop = min(a.l, lvl) - step
                target, tname = plan(b.c, stop, True)
                add("of_absorption_long", "buy",
                    f"The {a.t:%H:%M} absorption at {_px(lvl)} held: buyers took over (delta {_num(b.delta)}, CVD rising) "
                    f"and price closed above that bar's high. Entry {_px(b.c)}, stop {_px(stop)}, target {_px(target)} ({tname}).",
                    name, weak, b.c, stop, target)
                continue
            if side == "sell" and b.delta < 0 and b.c < a.l and b.cvd < a.cvd:
                stop = max(a.h, lvl) + step
                target, tname = plan(b.c, stop, False)
                add("of_absorption_short", "sell",
                    f"The {a.t:%H:%M} absorption at {_px(lvl)} held: sellers took over (delta {_num(b.delta)}, CVD falling) "
                    f"and price closed below that bar's low. Entry {_px(b.c)}, stop {_px(stop)}, target {_px(target)} ({tname}).",
                    name, weak, b.c, stop, target)
                continue
            if i - j < s.confirm_bars:
                still.append((j, side, lvl, name, weak))
        pending = still

        # Absorption: heavy selling (or buying), most of the bar's volume traded near its low (high), and
        # the bar still closed in its upper (lower) half: someone passive soaked up the aggression.
        poc_row = max(b.rows, key=lambda k: sum(b.rows[k])) * step if b.rows else b.c
        if heavy_sell and poc_row <= b.l + 0.4 * b.range and b.c >= b.l + 0.5 * b.range:
            lv = _near(major, b.l, tol)
            lvl, name = (lv[1], lv[0]) if lv else (b.l, None)
            add("of_absorption_bull", "buy",
                f"Sellers hit the bid hard (delta {_num(b.delta)}, {abs(b.delta) / avg_delta:.1f}x normal) and most volume traded "
                f"near the low {_px(b.l)}" + (f", at the {name} {_px(lvl)}" if name else "")
                + ", yet the bar closed in its upper half. Wait for buyers to take over.", name, lv is None)
            pending.append((i, "buy", lvl, name, lv is None))
        if heavy_buy and poc_row >= b.h - 0.4 * b.range and b.c <= b.h - 0.5 * b.range:
            lv = _near(major, b.h, tol)
            lvl, name = (lv[1], lv[0]) if lv else (b.h, None)
            add("of_absorption_bear", "sell",
                f"Buyers lifted the offer hard (delta {_num(b.delta)}, {abs(b.delta) / avg_delta:.1f}x normal) and most volume traded "
                f"near the high {_px(b.h)}" + (f", at the {name} {_px(lvl)}" if name else "")
                + ", yet the bar closed in its lower half. Wait for sellers to take over.", name, lv is None)
            pending.append((i, "sell", lvl, name, lv is None))

        # Stacked imbalances (Axia): initiative buying or selling through several prices in a row.
        if b.c > b.o and b.delta > 0 and (zone := _stack(b, True, s.imbalance_ratio, s.stacked_levels)):
            add("of_stacked_buy", "buy",
                f"Buyers outbid sellers {s.imbalance_ratio:g}:1 or more on {round((zone[1] - zone[0]) / step) + 1} prices in a row, "
                f"{_px(zone[0])}-{_px(zone[1])}. That zone tends to act as support while price stays above it.",
                f"stack {_px(zone[0])}", _near(major, zone[0], tol) is None)
        if b.c < b.o and b.delta < 0 and (zone := _stack(b, False, s.imbalance_ratio, s.stacked_levels)):
            add("of_stacked_sell", "sell",
                f"Sellers outhit buyers {s.imbalance_ratio:g}:1 or more on {round((zone[1] - zone[0]) / step) + 1} prices in a row, "
                f"{_px(zone[0])}-{_px(zone[1])}. That zone tends to act as resistance while price stays below it.",
                f"stack {_px(zone[1])}", _near(major, zone[1], tol) is None)

        # Divergence (price makes a higher high, CVD a lower high) and exhaustion, at a new swing high or low.
        recent = hist[-6:]
        swing_hi, swing_lo = max(h.h for h in recent), min(h.l for h in recent)
        earlier = hist[-12:-2]
        if earlier and b.h > swing_hi:
            peak = max(earlier, key=lambda h: h.h)
            if b.h > peak.h and b.cvd < peak.cvd and b.delta < 0:
                add("of_divergence_bear", "sell",
                    f"A higher high at {_px(b.h)} (above {_px(peak.h)} at {peak.t:%H:%M}) with a lower CVD ({_num(b.cvd)} vs "
                    f"{_num(peak.cvd)}) and negative delta: buyers didn't drive the new high.", None, _near(major, b.h, tol) is None)
        if b.h > swing_hi:
            top = sum(b.row(b.h))
            if b.range >= avg_range and top <= 0.25 * (b.volume / max(len(b.rows), 1)) and b.c <= b.l + 0.5 * b.range:
                add("of_exhaustion_high", "sell",
                    f"Almost nothing traded at the high {_px(b.h)} ({top:,.0f} shares) and the bar closed in its lower half: "
                    "buyers ran out at the top.", None, _near(major, b.h, tol) is None)
        if earlier and b.l < swing_lo:
            trough = min(earlier, key=lambda h: h.l)
            if b.l < trough.l and b.cvd > trough.cvd and b.delta > 0:
                add("of_divergence_bull", "buy",
                    f"A lower low at {_px(b.l)} (below {_px(trough.l)} at {trough.t:%H:%M}) with a higher CVD ({_num(b.cvd)} vs "
                    f"{_num(trough.cvd)}) and positive delta: sellers didn't drive the new low.", None, _near(major, b.l, tol) is None)
        if b.l < swing_lo:
            bottom = sum(b.row(b.l))
            if b.range >= avg_range and bottom <= 0.25 * (b.volume / max(len(b.rows), 1)) and b.c >= b.h - 0.5 * b.range:
                add("of_exhaustion_low", "buy",
                    f"Almost nothing traded at the low {_px(b.l)} ({bottom:,.0f} shares) and the bar closed in its upper half: "
                    "sellers ran out at the bottom.", None, _near(major, b.l, tol) is None)

        # Fabio Valentini's two models, from the profile of the session so far.
        if profile and vwap:
            session_hi, session_lo = max(h.h for h in before), min(h.l for h in before)
            # Trend: out of balance, a pullback into VWAP, the value edge or a thin node, then aggression.
            if b.c > profile.vah and b.c > vwap and heavy_buy:
                support = [lv for lv in levels if lv[0] in ("VWAP", "value area high", "low-volume node") and lv[1] < b.c]
                lv = _near(support, b.l, tol)
                if lv and b.l <= lv[1] + tol:
                    stop = lv[1] - tol
                    target = session_hi if session_hi - b.c >= b.c - stop else b.c + 2 * (b.c - stop)
                    add("of_trend_long", "buy",
                        f"Out of balance above value ({_px(profile.vah)}): a pullback to the {lv[0]} {_px(lv[1])} met aggressive "
                        f"buying (delta {_num(b.delta)}). Entry {_px(b.c)}, stop {_px(stop)}, target {_px(target)}.",
                        lv[0], False, b.c, stop, target)
            if b.c < profile.val and b.c < vwap and heavy_sell:
                resist = [lv for lv in levels if lv[0] in ("VWAP", "value area low", "low-volume node") and lv[1] > b.c]
                lv = _near(resist, b.h, tol)
                if lv and b.h >= lv[1] - tol:
                    stop = lv[1] + tol
                    target = session_lo if b.c - session_lo >= stop - b.c else b.c - 2 * (stop - b.c)
                    add("of_trend_short", "sell",
                        f"Out of balance below value ({_px(profile.val)}): a pullback to the {lv[0]} {_px(lv[1])} met aggressive "
                        f"selling (delta {_num(b.delta)}). Entry {_px(b.c)}, stop {_px(stop)}, target {_px(target)}.",
                        lv[0], False, b.c, stop, target)
            # Failed breakout: price left balance, came straight back in with aggression: target the point of control.
            edges_up = [lv for lv in levels if lv[0] in ("value area high", "opening range high", "prior high")]
            edges_dn = [lv for lv in levels if lv[0] in ("value area low", "opening range low", "prior low")]
            last2 = before[-2:]
            for name, edge in edges_up:
                poked = max(h.h for h in last2)
                if poked > edge + tol and b.c < edge and heavy_sell and profile.poc < b.c:
                    stop = max(poked, b.h) + step
                    add("of_failed_breakout", "sell",
                        f"Price broke above the {name} {_px(edge)} to {_px(poked)} and came straight back inside with aggressive "
                        f"selling (delta {_num(b.delta)}). Entry {_px(b.c)}, stop {_px(stop)}, target the point of control {_px(profile.poc)}.",
                        name, False, b.c, stop, profile.poc)
                    break
            for name, edge in edges_dn:
                poked = min(h.l for h in last2)
                if poked < edge - tol and b.c > edge and heavy_buy and profile.poc > b.c:
                    stop = min(poked, b.l) - step
                    add("of_failed_breakdown", "buy",
                        f"Price broke below the {name} {_px(edge)} to {_px(poked)} and came straight back inside with aggressive "
                        f"buying (delta {_num(b.delta)}). Entry {_px(b.c)}, stop {_px(stop)}, target the point of control {_px(profile.poc)}.",
                        name, False, b.c, stop, profile.poc)
                    break
        out += found
    return out


def summary(bars: list[FlowBar], signals: list[FlowSignal], feed: str) -> dict:
    """A compact read of today's order flow for the AI prompt."""
    if not bars:
        return {}
    pv, vol = sum(b.pv for b in bars), sum(b.volume for b in bars)
    profile = volume_profile(bars, bars[-1].step * 5)
    last = bars[-1]
    out: dict = {
        "source": f"{feed.upper()} trades, {last.t:%H:%M} bar of {len(bars)}",
        "cvd": round(last.cvd),
        "delta_last_3_bars": [round(b.delta) for b in bars[-3:]],
        "buy_share_last_6_bars_pct": round(100 * sum(b.buy for b in bars[-6:]) / max(sum(b.buy + b.sell for b in bars[-6:]), 1)),
    }
    if vol:
        out["vwap"] = round(pv / vol, 2)
    if profile:
        state = "above value" if last.c > profile.vah else "below value" if last.c < profile.val else "inside value"
        out.update({"poc": profile.poc, "value_area": [profile.val, profile.vah], "state": state})
    recent = [sg for sg in signals if not sg.weak][-4:]
    if recent:
        out["recent_signals"] = [
            {"at": f"{sg.bar_time:%H:%M}", "signal": sg.label, "side": sg.side}
            | ({"entry": sg.entry, "stop": sg.stop, "target": sg.target} if sg.setup else {})
            for sg in recent]
    return out


class AlpacaTrades:
    """Trades for one symbol and window from Alpaca's market data API, as raw rows (light on memory).
    feed "iex" works live on the free plan; "sip" (the whole market) live needs a paid plan."""

    def __init__(self, api_key: str, secret_key: str, feed: str = "iex"):
        from alpaca.data.historical.stock import StockHistoricalDataClient

        if not api_key or not secret_key:
            raise ValueError("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set")
        self._client = StockHistoricalDataClient(api_key, secret_key, raw_data=True)
        self.feed = feed

    def prior_day(self, symbol: str, day: date, tz: ZoneInfo) -> dict[str, float]:
        """The high, low and close of the last session before `day`."""
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        end = datetime.combine(day, time(0), tzinfo=tz)
        data = self._client.get_stock_bars(StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
                                                            start=end - timedelta(days=10), end=end, feed=DataFeed(self.feed)))
        rows = data.get(symbol, []) if isinstance(data, dict) else []
        if not rows:
            return {}
        r = rows[-1]
        return {"prior high": float(r["h"]), "prior low": float(r["l"]), "prior close": float(r["c"])}

    def trades(self, symbol: str, start: datetime, end: datetime) -> list[dict]:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockTradesRequest

        data = self._client.get_stock_trades(StockTradesRequest(symbol_or_symbols=symbol, start=start, end=end,
                                                                feed=DataFeed(self.feed)))
        return list(data.get(symbol, [])) if isinstance(data, dict) else []


class FlowTracker:
    """Today's tape for each symbol, topped up with only the new trades on each call."""

    def __init__(self, source, bar_minutes: int, tz: ZoneInfo, settings: Settings = Settings(),
                 chunk: timedelta = timedelta(minutes=30)):
        self.source, self.bar_minutes, self.tz, self.settings, self.chunk = source, bar_minutes, tz, settings, chunk
        self.tapes: dict[str, Tape] = {}

    def update(self, symbol: str, now: datetime) -> Tape:
        now = now.astimezone(self.tz)
        tape = self.tapes.get(symbol)
        if tape is None or tape.day != now.date():
            tape = self.tapes[symbol] = Tape(now.date(), self.bar_minutes, self.tz)
        start = tape.open if tape.last_t is None else max(tape.open, tape.last_t + timedelta(microseconds=1))
        end = min(now, tape.close)
        while start < end:  # in chunks, so a first fetch late in the day isn't one huge response
            stop = min(start + self.chunk, end)
            tape.add(self.source.trades(symbol, start, stop))
            start = stop if tape.last_t is None else max(stop, tape.last_t + timedelta(microseconds=1))
        return tape

    def signals(self, symbol: str, now: datetime, prior: dict[str, float] | None = None) -> tuple[list[FlowBar], list[FlowSignal]]:
        bars = self.update(symbol, now).closed(now.astimezone(self.tz))
        return bars, analyze(bars, prior, self.settings)


def evaluate(signal: FlowSignal, after: list[FlowBar]) -> dict:
    """What happened next, for studies: a setup's result in R (stop or target first, else the
    session's last close), or for a context signal the move over the next 3 and 6 bars."""
    direction = 1 if signal.side == "buy" else -1
    if signal.setup and signal.entry and signal.stop and signal.target:
        risk = abs(signal.entry - signal.stop)
        for b in after:
            hit_stop = b.l <= signal.stop if direction > 0 else b.h >= signal.stop
            hit_target = b.h >= signal.target if direction > 0 else b.l <= signal.target
            if hit_stop:  # same-bar ties count as a loss: the bar's order of events is unknown
                return {"result": "stop", "r": -1.0}
            if hit_target:
                return {"result": "target", "r": round(abs(signal.target - signal.entry) / risk, 2)}
        if after:
            return {"result": "close", "r": round((after[-1].c - signal.entry) * direction / risk, 2)}
        return {"result": "open", "r": None}
    out = {}
    for n in (3, 6):
        if len(after) >= n:
            out[f"move_{n}_bars_pct"] = round((after[n - 1].c - signal.price) / signal.price * 100 * direction, 3)
    return out


def build_tape(source, symbol: str, day: date, bar_minutes: int, tz: ZoneInfo,
               progress: Callable[[str], None] | None = None) -> Tape:
    """A whole session, fetched half an hour at a time so the full tape fits in memory."""
    tape = Tape(day, bar_minutes, tz)
    start = tape.open
    while start < tape.close:
        stop = min(start + timedelta(minutes=30), tape.close)
        tape.add(source.trades(symbol, start, stop))
        if progress:
            progress(f"{symbol} {stop:%H:%M} {tape.trades:,} trades")
        start = stop
    return tape
