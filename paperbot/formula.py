"""Charts of a formula of symbols, such as SPY/QQQ, (AAPL+MSFT)/2 or NVDA-2*AMD, as TradingView draws them: the
formula is worked out on each candle's open, high, low and close separately, on the candles every symbol has."""

from __future__ import annotations

import re
from collections.abc import Callable

from .features import Bar

_TOKEN = re.compile(r"\s*(?:(\d+\.?\d*|\.\d+)|([A-Za-z][A-Za-z0-9.]{0,9})|(\S))")


def is_formula(text: str) -> bool:
    return bool(re.search(r"[-+*/()]|\d", text.strip())) and not re.fullmatch(r"[A-Za-z][A-Za-z0-9.]*", text.strip())


def parse(text: str) -> tuple[Callable[[dict[str, float]], float], list[str]]:
    """The formula as a function of each symbol's price, and the symbols it uses."""
    tokens = []
    pos = 0
    text = text.strip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            break
        pos = m.end()
        if m.group(1):
            tokens.append(("num", float(m.group(1))))
        elif m.group(2):
            tokens.append(("sym", m.group(2).upper()))
        elif m.group(3) in "+-*/()":
            tokens.append(("op", m.group(3)))
        else:
            raise ValueError(f"{m.group(3)!r} can't be in a formula: use symbols, numbers, + - * / and brackets")
    symbols: list[str] = []
    i = 0

    def peek():
        return tokens[i] if i < len(tokens) else (None, None)

    def take():
        nonlocal i
        i += 1
        return tokens[i - 1]

    def expr():
        f = term()
        while peek() in (("op", "+"), ("op", "-")):
            op = take()[1]
            g, h = f, term()
            f = (lambda a, b: lambda p: a(p) + b(p))(g, h) if op == "+" else (lambda a, b: lambda p: a(p) - b(p))(g, h)
        return f

    def term():
        f = factor()
        while peek() in (("op", "*"), ("op", "/")):
            op = take()[1]
            g, h = f, factor()
            f = (lambda a, b: lambda p: a(p) * b(p))(g, h) if op == "*" else (lambda a, b: lambda p: a(p) / b(p))(g, h)
        return f

    def factor():
        kind, val = peek()
        if (kind, val) == ("op", "-"):
            take()
            g = factor()
            return lambda p: -g(p)
        if (kind, val) == ("op", "("):
            take()
            f = expr()
            if peek() != ("op", ")"):
                raise ValueError("a bracket isn't closed")
            take()
            return f
        if kind == "num":
            take()
            return lambda p, v=val: v
        if kind == "sym":
            take()
            if val not in symbols:
                symbols.append(val)
            return lambda p, s=val: p[s]
        raise ValueError("the formula isn't complete")

    if not tokens:
        raise ValueError("an empty formula")
    f = expr()
    if i != len(tokens):
        raise ValueError("the formula has something extra at the end")
    if not symbols:
        raise ValueError("a formula needs at least one symbol")
    if len(symbols) > 8:
        raise ValueError("at most 8 symbols in a formula")
    return f, symbols


def combine(text: str, bars: dict[str, list[Bar]]) -> list[Bar]:
    """The formula's candles, on the times every symbol has a candle. Volume only when it's one symbol."""
    f, symbols = parse(text)
    by = {s: {b.t: b for b in bars.get(s, [])} for s in symbols}
    times = sorted(set.intersection(*(set(m) for m in by.values()))) if by else []
    out = []
    for t in times:
        try:
            vals = [f({s: getattr(by[s][t], k) for s in symbols}) for k in "ohlc"]
        except ZeroDivisionError:
            continue
        o, h, low, c = vals
        out.append(Bar(t, o, max(vals), min(vals), c, by[symbols[0]][t].v if len(symbols) == 1 else 0.0))
    return out
