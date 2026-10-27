"""Round-trip trades and performance stats, rebuilt from the broker's fills.

Fills are the source of truth: they include broker stops and flattens that paperbot
didn't place itself in that moment. Buys are matched to sells first-in, first-out.
Alpaca charges no options commission, but small regulatory fees aren't counted here.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .models import OptionSymbol, Order

# Below this many trades, any win rate or expectancy is mostly noise.
MIN_TRADES_TO_JUDGE = 30


@dataclass(frozen=True)
class ClosedTrade:
    symbol: str
    qty: int
    entry_price: float
    exit_price: float
    opened_at: datetime
    closed_at: datetime
    exit_client_order_id: str

    @property
    def multiplier(self) -> int:
        return 100 if OptionSymbol.parse(self.symbol) else 1

    @property
    def pnl(self) -> float:
        return (self.exit_price - self.entry_price) * self.qty * self.multiplier

    @property
    def pnl_pct(self) -> float:
        return (self.exit_price - self.entry_price) / self.entry_price * 100 if self.entry_price else 0.0

    @property
    def hold_minutes(self) -> float:
        return (self.closed_at - self.opened_at).total_seconds() / 60

    @property
    def exit_kind(self) -> str:
        cid = self.exit_client_order_id
        if cid.startswith("pb-stop-"):
            return "broker stop"
        if cid.startswith("pb-exit-"):
            return "exit order"
        return "flatten/other"


def closed_trades(orders: list[Order]) -> list[ClosedTrade]:
    fills = sorted(
        (o for o in orders if o.filled_qty > 0 and o.filled_at and o.filled_avg_price is not None),
        key=lambda o: o.filled_at,
    )
    lots: dict[str, deque[list]] = defaultdict(deque)  # symbol -> [qty, price, time]
    trades = []
    for o in fills:
        if o.side == "buy":
            lots[o.symbol].append([o.filled_qty, o.filled_avg_price, o.filled_at])
            continue
        remaining = o.filled_qty
        book = lots[o.symbol]
        while remaining and book:  # sells of positions bought before the lookback window are skipped
            lot = book[0]
            take = min(lot[0], remaining)
            trades.append(ClosedTrade(o.symbol, take, lot[1], o.filled_avg_price, lot[2], o.filled_at,
                                      o.client_order_id))
            lot[0] -= take
            remaining -= take
            if lot[0] == 0:
                book.popleft()
    return trades


def closed_on(trades: list[ClosedTrade], day: date, tz: ZoneInfo) -> list[ClosedTrade]:
    return [t for t in trades if t.closed_at.astimezone(tz).date() == day]


def loss_streak(trades: list[ClosedTrade]) -> int:
    """Losing trades in a row, counting back from the most recent."""
    n = 0
    for t in sorted(trades, key=lambda t: t.closed_at, reverse=True):
        if t.pnl >= 0:
            break
        n += 1
    return n


@dataclass(frozen=True)
class Stats:
    n: int
    wins: int
    total_pnl: float
    avg_win: float
    avg_loss: float
    expectancy: float
    profit_factor: float | None
    max_loss_streak: int
    best: float
    worst: float

    @property
    def win_rate(self) -> float:
        return self.wins / self.n * 100 if self.n else 0.0


def stats(trades: list[ClosedTrade]) -> Stats:
    pnls = [t.pnl for t in sorted(trades, key=lambda t: t.closed_at)]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    streak = longest = 0
    for p in pnls:
        streak = streak + 1 if p < 0 else 0
        longest = max(longest, streak)
    return Stats(
        n=len(pnls),
        wins=len(wins),
        total_pnl=sum(pnls),
        avg_win=sum(wins) / len(wins) if wins else 0.0,
        avg_loss=sum(losses) / len(losses) if losses else 0.0,
        expectancy=sum(pnls) / len(pnls) if pnls else 0.0,
        profit_factor=sum(wins) / -sum(losses) if losses else None,
        max_loss_streak=longest,
        best=max(pnls, default=0.0),
        worst=min(pnls, default=0.0),
    )


def tokens_used(llm_turns: list[dict]) -> int:
    """Total AI tokens from journal llm_turn events (Anthropic and OpenAI-style usage both count)."""
    total = 0
    for e in llm_turns:
        u = e.get("usage") or {}
        total += (u.get("input_tokens") or u.get("prompt_tokens") or 0) + (u.get("output_tokens") or u.get("completion_tokens") or 0)
    return total


@dataclass(frozen=True)
class Execution:
    entries_sent: int
    entries_filled: int
    entry_slippage: float | None  # average $ per share paid above the mid when the order went out
    exit_slippage: float | None  # average $ per share received below the mid

    @property
    def fill_rate(self) -> float | None:
        return self.entries_filled / self.entries_sent * 100 if self.entries_sent else None


def execution(submitted: list[dict], fills: list[dict]) -> Execution:
    """From journal events: order_submitted (decision price) and fill (what actually happened)."""
    entries = [e for e in submitted if e.get("kind") == "entry"]
    filled_ids = {f["client_order_id"] for f in fills}
    slips = {"buy": [], "sell": []}
    for f in fills:
        if f.get("slippage") is not None:
            slips[f["side"]].append(f["slippage"])
    avg = lambda xs: sum(xs) / len(xs) if xs else None  # noqa: E731
    return Execution(len(entries), sum(1 for e in entries if e["client_order_id"] in filled_ids),
                     avg(slips["buy"]), avg(slips["sell"]))


def execution_line(x: Execution) -> str:
    if not x.entries_sent:
        return "Execution: no entries sent"
    parts = [f"Execution: {x.entries_filled}/{x.entries_sent} entries filled ({x.fill_rate:.0f}%)"]
    if x.entry_slippage is not None:
        parts.append(f"entry slippage {x.entry_slippage:+.3f}/share vs mid")
    if x.exit_slippage is not None:
        parts.append(f"exit slippage {x.exit_slippage:+.3f}/share vs mid")
    return ", ".join(parts)


@dataclass(frozen=True)
class MonteCarlo:
    runs: int
    median_max_drawdown: float
    p95_max_drawdown: float
    p_drawdown_over: float  # chance the max drawdown exceeds `threshold`
    threshold: float
    p_end_below_start: float


def monte_carlo(trades: list[ClosedTrade], threshold: float, runs: int = 5000, seed: int = 7) -> MonteCarlo | None:
    """Resample the trade P&Ls with replacement and rebuild the equity path each time.
    Assumes trades are independent and that the future looks like the past; closed trades only."""
    import random

    pnls = [t.pnl for t in trades]
    if len(pnls) < 10:
        return None
    rng = random.Random(seed)
    drawdowns, endings = [], []
    for _ in range(runs):
        equity = peak = worst = 0.0
        for p in rng.choices(pnls, k=len(pnls)):
            equity += p
            peak = max(peak, equity)
            worst = max(worst, peak - equity)
        drawdowns.append(worst)
        endings.append(equity)
    drawdowns.sort()
    return MonteCarlo(
        runs=runs,
        median_max_drawdown=drawdowns[runs // 2],
        p95_max_drawdown=drawdowns[int(runs * 0.95)],
        p_drawdown_over=sum(d > threshold for d in drawdowns) / runs * 100,
        threshold=threshold,
        p_end_below_start=sum(e < 0 for e in endings) / runs * 100,
    )


def excursion_summary(trades: list[ClosedTrade], excursions: list[dict]) -> str | None:
    """How far winners dipped and losers rallied before closing (MAE/MFE, % of entry)."""
    by_symbol: dict[str, list[dict]] = {}
    for e in excursions:
        by_symbol.setdefault(e["symbol"], []).append(e)
    win_mae, lose_mfe = [], []
    for t in trades:
        match = by_symbol.get(t.symbol)
        if not match:
            continue
        e = match.pop(0)
        (win_mae if t.pnl > 0 else lose_mfe).append(e["mae_pct"] if t.pnl > 0 else e["mfe_pct"])
    if len(win_mae) + len(lose_mfe) < 5:
        return None

    def pct(xs: list[float], q: float) -> float:
        xs = sorted(xs)
        return xs[min(len(xs) - 1, int(q * len(xs)))]
    parts = []
    if win_mae:
        parts.append(f"winners' worst dip: median {pct(win_mae, 0.5):+.0f}%, deepest 10% {pct(win_mae, 0.1):+.0f}%")
    if lose_mfe:
        parts.append(f"losers' best rally: median {pct(lose_mfe, 0.5):+.0f}%")
    return "Excursions (MAE/MFE): " + "; ".join(parts) + ". A stop well beyond winners' deepest dips is looser than it needs to be."


def rejection_summary(rejections: list[dict], top: int = 5) -> str | None:
    """Why proposals were rejected, grouped. Many rejections of one kind are prompt-tuning feedback."""
    import re
    from collections import Counter

    keys = Counter(re.sub(r"[-+]?\$?[\d,.]+%?", "#", e.get("why", "")).strip() for e in rejections
                   if e.get("kind") == "entry" or e.get("reason") not in ("protective stop",))
    if not keys:
        return None
    rows = [f"  {n:>3} x {k}" for k, n in keys.most_common(top)]
    return "Most common rejections:\n" + "\n".join(rows)


@dataclass(frozen=True)
class EdgeCheck:
    sharpe_per_trade: float
    psr: float  # probability the true per-trade Sharpe is above zero
    min_trades_95: float | None  # trades needed for 95% confidence the edge is real, at this Sharpe


def edge_check(trades: list[ClosedTrade]) -> EdgeCheck | None:
    """Probabilistic Sharpe ratio and minimum track record length (Bailey & López de Prado),
    on per-trade returns, adjusting for skew and fat tails."""
    import math

    r = [t.pnl_pct for t in trades]
    n = len(r)
    if n < 10:
        return None
    mu = sum(r) / n
    sd = (sum((x - mu) ** 2 for x in r) / (n - 1)) ** 0.5
    if sd == 0:
        return None
    sr = mu / sd
    skew = sum(((x - mu) / sd) ** 3 for x in r) / n
    kurt = sum(((x - mu) / sd) ** 4 for x in r) / n
    var_term = 1 - skew * sr + (kurt - 1) / 4 * sr ** 2
    z = sr * math.sqrt(n - 1) / math.sqrt(max(var_term, 1e-9))
    psr = 0.5 * (1 + math.erf(z / math.sqrt(2)))
    min_trades = 1 + var_term * (1.645 / sr) ** 2 if sr > 0 else None
    return EdgeCheck(sr, psr * 100, min_trades)


def one_line(s: Stats) -> str:
    if not s.n:
        return "no closed trades"
    return f"{s.n} closed, {s.win_rate:.0f}% won, P&L {s.total_pnl:+,.0f}"


def format_report(trades: list[ClosedTrade], reasons: dict[str, str], days: int, tz: ZoneInfo,
                  risk_threshold: float = 5000) -> str:
    s = stats(trades)
    lines = [f"Closed trades, last {days} days: {s.n}"]
    if not s.n:
        return lines[0]
    pf = f"{s.profit_factor:.2f}" if s.profit_factor is not None else "n/a (no losses)"
    lines += [
        f"Total P&L {s.total_pnl:+,.2f}   win rate {s.win_rate:.0f}%   expectancy {s.expectancy:+,.2f}/trade",
        f"Avg win {s.avg_win:+,.2f}   avg loss {s.avg_loss:+,.2f}   profit factor {pf}",
        f"Best {s.best:+,.2f}   worst {s.worst:+,.2f}   longest losing streak {s.max_loss_streak}",
    ]
    if s.n < MIN_TRADES_TO_JUDGE:
        lines.append(f"Only {s.n} trades: too few to tell skill from luck (aim for {MIN_TRADES_TO_JUDGE}+).")
    edge = edge_check(trades)
    if edge is not None:
        need = (f"about {edge.min_trades_95:,.0f} trades would be needed to be 95% sure at this rate"
                if edge.min_trades_95 is not None else "no positive edge so far")
        lines.append(f"Edge check: per-trade Sharpe {edge.sharpe_per_trade:+.2f}, {edge.psr:.0f}% probability the true "
                     f"edge is above zero; {need}.")
    mc = monte_carlo(trades, threshold=risk_threshold)
    if mc is not None:
        lines.append(
            f"Monte Carlo ({mc.runs:,} resamples of these trades): median max drawdown {mc.median_max_drawdown:,.0f}, "
            f"95th percentile {mc.p95_max_drawdown:,.0f}; {mc.p_drawdown_over:.0f}% chance of a drawdown over "
            f"{mc.threshold:,.0f}; {mc.p_end_below_start:.0f}% chance of ending negative.")
    lines.append("")
    lines.append(f"{'closed (ET)':16} {'symbol':20} {'qty':>3} {'entry':>7} {'exit':>7} {'P&L':>9} {'held':>7}  exit")
    for t in sorted(trades, key=lambda t: t.closed_at):
        why = reasons.get(t.exit_client_order_id) or t.exit_kind
        lines.append(
            f"{t.closed_at.astimezone(tz):%m-%d %H:%M}      {t.symbol:20} {t.qty:>3} {t.entry_price:>7.2f} "
            f"{t.exit_price:>7.2f} {t.pnl:>+9.2f} {t.hold_minutes:>5.0f}m  {why}"
        )
    return "\n".join(lines)
