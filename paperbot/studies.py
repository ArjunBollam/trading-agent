"""Indicators for the Charts tab, each worked out the way TradingView's built-in of the same name is (same defaults,
same smoothing), on whatever candles the chart shows. Any indicator that takes a source can take another indicator's
line instead of price: an indicator on an indicator, such as a 9-period average of RSI.

`compute(bars, specs)` takes the chart's candles and a list of {"id", "type", "inputs"} and returns every line,
aligned with the candles (None while an indicator is still warming up)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .features import Bar

Series = list  # list[float | None], one value per candle

PRICE_SOURCES = ("close", "open", "high", "low", "hl2", "hlc3", "ohlc4", "hlcc4", "volume")
MAX_STUDIES = 50


# ---------- building blocks (None while not enough data) ----------

def _first(xs: Series) -> int:
    return next((i for i, x in enumerate(xs) if x is not None), len(xs))


def sma(xs: Series, n: int) -> Series:
    out: Series = [None] * len(xs)
    run, count = 0.0, 0
    for i, x in enumerate(xs):
        if x is None:
            run, count = 0.0, 0
            continue
        run += x
        count += 1
        if count > n:
            run -= xs[i - n]
            count = n
        if count == n:
            out[i] = run / n
    return out


def ema(xs: Series, n: int) -> Series:
    """Exponential average, alpha = 2 / (n + 1), started from the first n values' simple average (as the scanner's
    EMA signals are), so it shows nothing until it has n values."""
    out: Series = [None] * len(xs)
    start = _first(xs)
    if len(xs) - start < n or None in xs[start:start + n]:
        return out
    a, prev = 2 / (n + 1), sum(xs[start:start + n]) / n
    out[start + n - 1] = prev
    for i in range(start + n, len(xs)):
        if xs[i] is None:
            continue
        prev = a * xs[i] + (1 - a) * prev
        out[i] = prev
    return out


def rma(xs: Series, n: int) -> Series:
    """Wilder's average (Pine's ta.rma): starts from the n-period simple average."""
    out: Series = [None] * len(xs)
    start = _first(xs)
    if len(xs) - start < n or None in xs[start:start + n]:
        return out
    prev = sum(xs[start:start + n]) / n
    out[start + n - 1] = prev
    for i in range(start + n, len(xs)):
        if xs[i] is None:
            continue
        prev = (prev * (n - 1) + xs[i]) / n
        out[i] = prev
    return out


def wma(xs: Series, n: int) -> Series:
    out: Series = [None] * len(xs)
    total = n * (n + 1) / 2
    for i in range(n - 1, len(xs)):
        w = xs[i - n + 1:i + 1]
        if None not in w:
            out[i] = sum(x * (k + 1) for k, x in enumerate(w)) / total
    return out


def _combine(f, *series: Series) -> Series:
    return [None if any(s[i] is None for s in series) else f(*(s[i] for s in series)) for i in range(len(series[0]))]


def stdev(xs: Series, n: int) -> Series:
    """Population standard deviation over n, like Pine's ta.stdev."""
    mean = sma(xs, n)
    out: Series = [None] * len(xs)
    for i in range(len(xs)):
        if mean[i] is not None:
            out[i] = math.sqrt(max(sum((x - mean[i]) ** 2 for x in xs[i - n + 1:i + 1]) / n, 0.0))
    return out


def highest(xs: Series, n: int) -> Series:
    return [None if i < n - 1 or None in xs[i - n + 1:i + 1] else max(xs[i - n + 1:i + 1]) for i in range(len(xs))]


def lowest(xs: Series, n: int) -> Series:
    return [None if i < n - 1 or None in xs[i - n + 1:i + 1] else min(xs[i - n + 1:i + 1]) for i in range(len(xs))]


def change(xs: Series, n: int = 1) -> Series:
    return [None if i < n or xs[i] is None or xs[i - n] is None else xs[i] - xs[i - n] for i in range(len(xs))]


def linreg(xs: Series, n: int) -> Series:
    """The least-squares line's value at the last of each n candles."""
    out: Series = [None] * len(xs)
    sx = n * (n - 1) / 2
    sxx = (n - 1) * n * (2 * n - 1) / 6
    for i in range(n - 1, len(xs)):
        w = xs[i - n + 1:i + 1]
        if None in w:
            continue
        sy = sum(w)
        sxy = sum(k * y for k, y in enumerate(w))
        d = n * sxx - sx * sx
        slope = (n * sxy - sx * sy) / d if d else 0.0
        out[i] = (sy - slope * sx) / n + slope * (n - 1)
    return out


def true_range(bars: list[Bar]) -> Series:
    return [b.h - b.l if i == 0 else max(b.h - b.l, abs(b.h - bars[i - 1].c), abs(b.l - bars[i - 1].c)) for i, b in enumerate(bars)]


def rsi(xs: Series, n: int) -> Series:
    d = change(xs)
    up = rma([None if x is None else max(x, 0.0) for x in d], n)
    down = rma([None if x is None else max(-x, 0.0) for x in d], n)
    return _combine(lambda u, v: 100.0 if v == 0 else 0.0 if u == 0 else 100 - 100 / (1 + u / v), up, down)


def stoch(src: Series, hi: Series, lo: Series, n: int) -> Series:
    h, low = highest(hi, n), lowest(lo, n)
    return _combine(lambda c, a, b: 0.0 if a == b else 100 * (c - b) / (a - b), src, h, low)


# ---------- the catalog ----------

@dataclass(frozen=True)
class Input:
    key: str
    name: str
    default: object
    kind: str = "int"  # int | float | source
    lo: float = 1
    hi: float = 500


@dataclass(frozen=True)
class Output:
    key: str
    name: str
    color: str
    kind: str = "line"  # line | hist | dots | area


@dataclass(frozen=True)
class Study:
    type: str
    name: str
    overlay: bool  # drawn on the price (or on the line it's applied to) rather than in its own pane
    inputs: tuple[Input, ...]
    outputs: tuple[Output, ...]
    levels: tuple[float, ...] = ()
    group: str = "Trend"
    about: str = ""
    run: object = field(default=None, repr=False, compare=False)


def _src(default: str = "close") -> Input:
    return Input("source", "Source", default, "source")


def _len(n: int, key: str = "length", name: str = "Length") -> Input:
    return Input(key, name, n)


CATALOG: dict[str, Study] = {}


def study(type_: str, name: str, overlay: bool, inputs: list[Input], outputs: list[Output], levels=(), group="Trend", about=""):
    def wrap(f):
        CATALOG[type_] = Study(type_, name, overlay, tuple(inputs), tuple(outputs), tuple(levels), group, about, f)
        return f
    return wrap


B, R, G, P, O, Y, W, C = "#2962ff", "#f23645", "#089981", "#9c27b0", "#ff9800", "#fbc02d", "#d1d4dc", "#00bcd4"


@study("sma", "Moving Average Simple", True, [_len(9), _src()], [Output("ma", "MA", B)], about="The average close of the last n candles.")
def _sma(c, s, i):
    return {"ma": sma(s(i["source"]), i["length"])}


@study("ema", "Moving Average Exponential", True, [_len(9), _src()], [Output("ma", "EMA", B)], about="An average that weighs recent candles more.")
def _ema(c, s, i):
    return {"ma": ema(s(i["source"]), i["length"])}


@study("wma", "Moving Average Weighted", True, [_len(9), _src()], [Output("ma", "WMA", B)])
def _wma(c, s, i):
    return {"ma": wma(s(i["source"]), i["length"])}


@study("hma", "Hull Moving Average", True, [_len(9), _src()], [Output("ma", "HMA", O)], about="A fast average with little lag.")
def _hma(c, s, i):
    x, n = s(i["source"]), i["length"]
    raw = _combine(lambda a, b: 2 * a - b, wma(x, max(n // 2, 1)), wma(x, n))
    return {"ma": wma(raw, max(int(math.sqrt(n)), 1))}


@study("smma", "Smoothed Moving Average", True, [_len(7), _src()], [Output("ma", "SMMA", P)])
def _smma(c, s, i):
    return {"ma": rma(s(i["source"]), i["length"])}


@study("dema", "Double EMA", True, [_len(9), _src()], [Output("ma", "DEMA", C)])
def _dema(c, s, i):
    e = ema(s(i["source"]), i["length"])
    return {"ma": _combine(lambda a, b: 2 * a - b, e, ema(e, i["length"]))}


@study("tema", "Triple EMA", True, [_len(9), _src()], [Output("ma", "TEMA", Y)])
def _tema(c, s, i):
    e1 = ema(s(i["source"]), i["length"])
    e2 = ema(e1, i["length"])
    return {"ma": _combine(lambda a, b, d: 3 * a - 3 * b + d, e1, e2, ema(e2, i["length"]))}


@study("vwma", "Volume Weighted Moving Average", True, [_len(20), _src()], [Output("ma", "VWMA", P)])
def _vwma(c, s, i):
    x, v = s(i["source"]), s("volume")
    num, den = sma(_combine(lambda a, b: a * b, x, v), i["length"]), sma(v, i["length"])
    return {"ma": _combine(lambda a, b: None if b == 0 else a / b, num, den)}


@study("lsma", "Least Squares Moving Average", True, [_len(25), _src()], [Output("ma", "LSMA", B)])
def _lsma(c, s, i):
    return {"ma": linreg(s(i["source"]), i["length"])}


@study("vwap", "VWAP", True, [_src("hlc3"), Input("mult", "Bands multiplier", 1.0, "float", 0, 10)],
       [Output("vwap", "VWAP", B), Output("upper", "Upper band", G), Output("lower", "Lower band", G)],
       about="The session's volume-weighted average price, from each session's first candle, with standard deviation bands.")
def _vwap(c, s, i):
    x, v = s(i["source"]), s("volume")
    out, up, lo = [None] * len(c), [None] * len(c), [None] * len(c)
    pv = vv = pv2 = 0.0
    for k, b in enumerate(c):
        if k == 0 or b.t.date() != c[k - 1].t.date():
            pv = vv = pv2 = 0.0
        if x[k] is None:
            continue
        pv += x[k] * v[k]
        vv += v[k]
        pv2 += x[k] * x[k] * v[k]
        if vv > 0:
            mean = pv / vv
            dev = math.sqrt(max(pv2 / vv - mean * mean, 0.0))
            out[k] = mean
            if i["mult"]:
                up[k], lo[k] = mean + i["mult"] * dev, mean - i["mult"] * dev
    return {"vwap": out, "upper": up, "lower": lo}


@study("bb", "Bollinger Bands", True, [_len(20), _src(), Input("mult", "StdDev", 2.0, "float", 0.001, 50)],
       [Output("basis", "Basis", O), Output("upper", "Upper", B), Output("lower", "Lower", B)], group="Volatility",
       about="A 20-candle average with lines two standard deviations above and below.")
def _bb(c, s, i):
    x = s(i["source"])
    basis, dev = sma(x, i["length"]), stdev(x, i["length"])
    return {"basis": basis, "upper": _combine(lambda a, d: a + i["mult"] * d, basis, dev),
            "lower": _combine(lambda a, d: a - i["mult"] * d, basis, dev)}


@study("kc", "Keltner Channels", True, [_len(20), _src(), Input("mult", "Multiplier", 2.0, "float", 0.001, 50), _len(10, "atr", "ATR length")],
       [Output("basis", "Basis", B), Output("upper", "Upper", B), Output("lower", "Lower", B)], group="Volatility")
def _kc(c, s, i):
    basis = ema(s(i["source"]), i["length"])
    a = rma(true_range(c), i["atr"])
    return {"basis": basis, "upper": _combine(lambda m, r: m + i["mult"] * r, basis, a),
            "lower": _combine(lambda m, r: m - i["mult"] * r, basis, a)}


@study("dc", "Donchian Channels", True, [_len(20)], [Output("basis", "Basis", O), Output("upper", "Upper", B), Output("lower", "Lower", B)],
       group="Volatility", about="The highest high and lowest low of the last n candles.")
def _dc(c, s, i):
    up, lo = highest(s("high"), i["length"]), lowest(s("low"), i["length"])
    return {"basis": _combine(lambda a, b: (a + b) / 2, up, lo), "upper": up, "lower": lo}


@study("env", "Envelope", True, [_len(20), _src(), Input("percent", "Percent", 10.0, "float", 0.01, 100)],
       [Output("basis", "Basis", O), Output("upper", "Upper", B), Output("lower", "Lower", B)], group="Volatility")
def _env(c, s, i):
    basis = sma(s(i["source"]), i["length"])
    k = i["percent"] / 100
    return {"basis": basis, "upper": [None if x is None else x * (1 + k) for x in basis],
            "lower": [None if x is None else x * (1 - k) for x in basis]}


@study("psar", "Parabolic SAR", True, [Input("start", "Start", 0.02, "float", 0.001, 1), Input("step", "Increment", 0.02, "float", 0.001, 1),
                                       Input("max", "Maximum", 0.2, "float", 0.01, 1)], [Output("sar", "SAR", B, "dots")])
def _psar(c, s, i):
    out: Series = [None] * len(c)
    if len(c) < 2:
        return {"sar": out}
    up = c[1].c >= c[0].c
    af, ep = i["start"], (c[0].h if up else c[0].l)
    sar = c[0].l if up else c[0].h
    for k in range(1, len(c)):
        b = c[k]
        sar = sar + af * (ep - sar)
        if up:
            sar = min(sar, c[k - 1].l, c[k - 2].l if k > 1 else c[k - 1].l)
            if b.l < sar:
                up, sar, ep, af = False, ep, b.l, i["start"]
            elif b.h > ep:
                ep, af = b.h, min(af + i["step"], i["max"])
        else:
            sar = max(sar, c[k - 1].h, c[k - 2].h if k > 1 else c[k - 1].h)
            if b.h > sar:
                up, sar, ep, af = True, ep, b.h, i["start"]
            elif b.l < ep:
                ep, af = b.l, min(af + i["step"], i["max"])
        out[k] = sar
    return {"sar": out}


@study("supertrend", "Supertrend", True, [_len(10, "atr", "ATR length"), Input("factor", "Factor", 3.0, "float", 0.1, 50)],
       [Output("up", "Up trend", G), Output("down", "Down trend", R)], about="An ATR trailing line: green under price in an up trend, red above in a down trend.")
def _supertrend(c, s, i):
    a = rma(true_range(c), i["atr"])
    up_line, down_line = [None] * len(c), [None] * len(c)
    lower = upper = None
    trend = 1
    for k, b in enumerate(c):
        if a[k] is None:
            continue
        mid = (b.h + b.l) / 2
        lo, hi = mid - i["factor"] * a[k], mid + i["factor"] * a[k]
        prev_c = c[k - 1].c if k else b.c
        lower = lo if lower is None or lo > lower or prev_c < lower else lower
        upper = hi if upper is None or hi < upper or prev_c > upper else upper
        if trend == -1 and b.c > upper:
            trend = 1
        elif trend == 1 and b.c < lower:
            trend = -1
        if trend == 1:
            up_line[k] = lower
        else:
            down_line[k] = upper
    return {"up": up_line, "down": down_line}


@study("rsi", "Relative Strength Index", False, [_len(14), _src()], [Output("rsi", "RSI", P)], (70, 50, 30), "Momentum",
       "Wilder's RSI: average gain against average loss, 0 to 100.")
def _rsi(c, s, i):
    return {"rsi": rsi(s(i["source"]), i["length"])}


@study("macd", "MACD", False, [_len(12, "fast", "Fast length"), _len(26, "slow", "Slow length"), _src(), _len(9, "signal", "Signal smoothing")],
       [Output("hist", "Histogram", "#26a69a", "hist"), Output("macd", "MACD", B), Output("signal", "Signal", O)], (0,), "Momentum")
def _macd(c, s, i):
    x = s(i["source"])
    m = _combine(lambda a, b: a - b, ema(x, i["fast"]), ema(x, i["slow"]))
    sig = ema(m, i["signal"])
    return {"macd": m, "signal": sig, "hist": _combine(lambda a, b: a - b, m, sig)}


@study("stoch", "Stochastic", False, [_len(14, "k", "%K length"), _len(1, "smooth", "%K smoothing"), _len(3, "d", "%D smoothing")],
       [Output("k", "%K", B), Output("d", "%D", O)], (80, 20), "Momentum")
def _stoch(c, s, i):
    k = sma(stoch(s("close"), s("high"), s("low"), i["k"]), i["smooth"])
    return {"k": k, "d": sma(k, i["d"])}


@study("stochrsi", "Stochastic RSI", False, [_len(3, "k", "K"), _len(3, "d", "D"), _len(14, "rsi", "RSI length"), _len(14, "stoch", "Stochastic length"), _src()],
       [Output("k", "K", B), Output("d", "D", O)], (80, 20), "Momentum")
def _stochrsi(c, s, i):
    r = rsi(s(i["source"]), i["rsi"])
    k = sma(stoch(r, r, r, i["stoch"]), i["k"])
    return {"k": k, "d": sma(k, i["d"])}


@study("cci", "Commodity Channel Index", False, [_len(20), _src("hlc3")], [Output("cci", "CCI", B)], (100, 0, -100), "Momentum")
def _cci(c, s, i):
    x, n = s(i["source"]), i["length"]
    m = sma(x, n)
    out: Series = [None] * len(c)
    for k in range(len(c)):
        if m[k] is None:
            continue
        dev = sum(abs(v - m[k]) for v in x[k - n + 1:k + 1]) / n
        out[k] = 0.0 if dev == 0 else (x[k] - m[k]) / (0.015 * dev)
    return {"cci": out}


@study("mfi", "Money Flow Index", False, [_len(14), _src("hlc3")], [Output("mfi", "MFI", P)], (80, 20), "Volume")
def _mfi(c, s, i):
    x, v, n = s(i["source"]), s("volume"), i["length"]
    out: Series = [None] * len(c)
    for k in range(n, len(c)):
        if None in x[k - n:k + 1]:
            continue
        pos = sum(v[j] * x[j] for j in range(k - n + 1, k + 1) if x[j] > x[j - 1])
        neg = sum(v[j] * x[j] for j in range(k - n + 1, k + 1) if x[j] < x[j - 1])
        out[k] = 100.0 if neg == 0 else 100 - 100 / (1 + pos / neg)
    return {"mfi": out}


@study("willr", "Williams %R", False, [_len(14), _src()], [Output("r", "%R", P)], (-20, -50, -80), "Momentum")
def _willr(c, s, i):
    st = stoch(s(i["source"]), s("high"), s("low"), i["length"])
    return {"r": [None if x is None else x - 100 for x in st]}


@study("roc", "Rate Of Change", False, [_len(9), _src()], [Output("roc", "ROC", B)], (0,), "Momentum")
def _roc(c, s, i):
    x, n = s(i["source"]), i["length"]
    return {"roc": [None if k < n or x[k] is None or not x[k - n] else 100 * (x[k] - x[k - n]) / x[k - n] for k in range(len(x))]}


@study("mom", "Momentum", False, [_len(10), _src()], [Output("mom", "Momentum", B)], (0,), "Momentum")
def _mom(c, s, i):
    return {"mom": change(s(i["source"]), i["length"])}


@study("cmo", "Chande Momentum Oscillator", False, [_len(9), _src()], [Output("cmo", "CMO", B)], (50, 0, -50), "Momentum")
def _cmo(c, s, i):
    d, n = change(s(i["source"])), i["length"]
    out: Series = [None] * len(c)
    for k in range(n, len(c)):
        w = d[k - n + 1:k + 1]
        if None in w:
            continue
        up, dn = sum(x for x in w if x > 0), -sum(x for x in w if x < 0)
        out[k] = 0.0 if up + dn == 0 else 100 * (up - dn) / (up + dn)
    return {"cmo": out}


@study("atr", "Average True Range", False, [_len(14)], [Output("atr", "ATR", R)], (), "Volatility", "Wilder's average of each candle's true range.")
def _atr(c, s, i):
    return {"atr": rma(true_range(c), i["length"])}


@study("adx", "Directional Movement Index", False, [_len(14, "di", "DI length"), _len(14, "adx", "ADX smoothing")],
       [Output("adx", "ADX", R), Output("plus", "+DI", B), Output("minus", "-DI", O)], (25,), "Trend")
def _adx(c, s, i):
    up = [None] + [max(b.h - a.h, 0.0) if b.h - a.h > a.l - b.l else 0.0 for a, b in zip(c, c[1:])]
    dn = [None] + [max(a.l - b.l, 0.0) if a.l - b.l > b.h - a.h else 0.0 for a, b in zip(c, c[1:])]
    tr = rma(true_range(c), i["di"])
    plus = _combine(lambda u, t: 0.0 if t == 0 else 100 * u / t, rma(up, i["di"]), tr)
    minus = _combine(lambda d, t: 0.0 if t == 0 else 100 * d / t, rma(dn, i["di"]), tr)
    dx = _combine(lambda p, m: 0.0 if p + m == 0 else 100 * abs(p - m) / (p + m), plus, minus)
    return {"adx": rma(dx, i["adx"]), "plus": plus, "minus": minus}


@study("aroon", "Aroon", False, [_len(14)], [Output("up", "Aroon Up", O), Output("down", "Aroon Down", B)], (), "Trend")
def _aroon(c, s, i):
    n = i["length"]
    up, dn = [None] * len(c), [None] * len(c)
    for k in range(n, len(c)):
        w = c[k - n:k + 1]
        hi = max(range(len(w)), key=lambda j: (w[j].h, j))
        lo = max(range(len(w)), key=lambda j: (-w[j].l, j))
        up[k], dn[k] = 100 * hi / n, 100 * lo / n
    return {"up": up, "down": dn}


@study("obv", "On Balance Volume", False, [], [Output("obv", "OBV", B)], (), "Volume", "Volume added on up closes and taken away on down closes.")
def _obv(c, s, i):
    out, total = [], 0.0
    for k, b in enumerate(c):
        if k:
            total += b.v if b.c > c[k - 1].c else -b.v if b.c < c[k - 1].c else 0.0
        out.append(total)
    return {"obv": out}


def _adl(c: list[Bar]) -> Series:
    out, total = [], 0.0
    for b in c:
        total += 0.0 if b.h == b.l else ((b.c - b.l) - (b.h - b.c)) / (b.h - b.l) * b.v
        out.append(total)
    return out


@study("cmf", "Chaikin Money Flow", False, [_len(20)], [Output("cmf", "CMF", G)], (0,), "Volume")
def _cmf(c, s, i):
    mfv = [0.0 if b.h == b.l else ((b.c - b.l) - (b.h - b.c)) / (b.h - b.l) * b.v for b in c]
    num, den = sma(mfv, i["length"]), sma([b.v for b in c], i["length"])
    return {"cmf": _combine(lambda a, d: 0.0 if d == 0 else a / d, num, den)}


@study("chaikin", "Chaikin Oscillator", False, [_len(3, "fast", "Fast"), _len(10, "slow", "Slow")], [Output("osc", "Chaikin", R)], (0,), "Volume")
def _chaikin(c, s, i):
    a = _adl(c)
    return {"osc": _combine(lambda f, sl: f - sl, ema(a, i["fast"]), ema(a, i["slow"]))}


@study("efi", "Elder Force Index", False, [_len(13)], [Output("efi", "EFI", R)], (0,), "Volume")
def _efi(c, s, i):
    return {"efi": ema([None] + [(b.c - a.c) * b.v for a, b in zip(c, c[1:])], i["length"])}


@study("ao", "Awesome Oscillator", False, [], [Output("ao", "AO", "#26a69a", "hist")], (0,), "Momentum")
def _ao(c, s, i):
    x = s("hl2")
    return {"ao": _combine(lambda a, b: a - b, sma(x, 5), sma(x, 34))}


@study("trix", "TRIX", False, [_len(18), _src()], [Output("trix", "TRIX", R)], (0,), "Momentum")
def _trix(c, s, i):
    x = [None if v is None or v <= 0 else math.log(v) for v in s(i["source"])]
    e = ema(ema(ema(x, i["length"]), i["length"]), i["length"])
    return {"trix": [None if v is None else 10000 * v for v in change(e)]}


@study("tsi", "True Strength Index", False, [_len(25, "long", "Long length"), _len(13, "short", "Short length"), _len(13, "signal", "Signal length"), _src()],
       [Output("tsi", "TSI", B), Output("signal", "Signal", R)], (0,), "Momentum")
def _tsi(c, s, i):
    d = change(s(i["source"]))
    num = ema(ema(d, i["long"]), i["short"])
    den = ema(ema([None if x is None else abs(x) for x in d], i["long"]), i["short"])
    t = _combine(lambda a, b: 0.0 if b == 0 else 100 * a / b, num, den)
    return {"tsi": t, "signal": ema(t, i["signal"])}


@study("uo", "Ultimate Oscillator", False, [_len(7, "fast", "Fast"), _len(14, "middle", "Middle"), _len(28, "slow", "Slow")],
       [Output("uo", "UO", R)], (70, 30), "Momentum")
def _uo(c, s, i):
    bp = [None] + [b.c - min(b.l, a.c) for a, b in zip(c, c[1:])]
    tr = [None] + [max(b.h, a.c) - min(b.l, a.c) for a, b in zip(c, c[1:])]

    def avg(n):
        return _combine(lambda x, y: 0.0 if y == 0 else x / y, sma(bp, n), sma(tr, n))

    return {"uo": _combine(lambda f, m, sl: 100 * (4 * f + 2 * m + sl) / 7, avg(i["fast"]), avg(i["middle"]), avg(i["slow"]))}


@study("vol", "Volume", False, [_len(20, "ma", "MA length")], [Output("volume", "Volume", "#26a69a", "hist"), Output("ma", "Volume MA", B)], (), "Volume")
def _vol(c, s, i):
    v = s("volume")
    return {"volume": v, "ma": sma(v, i["ma"])}


@study("stdev", "Standard Deviation", False, [_len(20), _src()], [Output("sd", "StdDev", B)], (), "Volatility")
def _stdev(c, s, i):
    return {"sd": stdev(s(i["source"]), i["length"])}


@study("bbw", "Bollinger BandWidth", False, [_len(20), _src(), Input("mult", "StdDev", 2.0, "float", 0.001, 50)], [Output("bbw", "BBW", B)], (), "Volatility")
def _bbw(c, s, i):
    x = s(i["source"])
    basis, dev = sma(x, i["length"]), stdev(x, i["length"])
    return {"bbw": _combine(lambda m, d: 0.0 if m == 0 else 2 * i["mult"] * d / m, basis, dev)}


@study("bbpct", "Bollinger Bands %B", False, [_len(20), _src(), Input("mult", "StdDev", 2.0, "float", 0.001, 50)], [Output("pb", "%B", B)], (1, 0), "Volatility")
def _bbpct(c, s, i):
    x = s(i["source"])
    basis, dev = sma(x, i["length"]), stdev(x, i["length"])
    return {"pb": _combine(lambda v, m, d: 0.0 if d == 0 else (v - (m - i["mult"] * d)) / (2 * i["mult"] * d), x, basis, dev)}


# ---------- running a chart's indicators ----------

def price_series(bars: list[Bar], name: str) -> Series:
    f = {"close": lambda b: b.c, "open": lambda b: b.o, "high": lambda b: b.h, "low": lambda b: b.l,
         "hl2": lambda b: (b.h + b.l) / 2, "hlc3": lambda b: (b.h + b.l + b.c) / 3, "ohlc4": lambda b: (b.o + b.h + b.l + b.c) / 4,
         "hlcc4": lambda b: (b.h + b.l + 2 * b.c) / 4, "volume": lambda b: b.v}[name]
    return [f(b) for b in bars]


def catalog() -> list[dict]:
    return [{"type": s.type, "name": s.name, "overlay": s.overlay, "group": s.group, "about": s.about, "levels": list(s.levels),
             "inputs": [{"key": x.key, "name": x.name, "default": x.default, "kind": x.kind, "min": x.lo, "max": x.hi} for x in s.inputs],
             "outputs": [{"key": o.key, "name": o.name, "color": o.color, "kind": o.kind} for o in s.outputs]}
            for s in CATALOG.values()]


def _inputs(st: Study, given: dict) -> dict:
    out = {}
    for x in st.inputs:
        v = given.get(x.key, x.default)
        if x.kind == "source":
            v = str(v)
        else:
            try:
                v = int(v) if x.kind == "int" else float(v)
            except (TypeError, ValueError):
                raise ValueError(f"{st.name}: {x.name} must be a number") from None
            if not x.lo <= v <= x.hi:
                raise ValueError(f"{st.name}: {x.name} must be {x.lo:g} to {x.hi:g}")
        out[x.key] = v
    return out


def short_name(st: Study, inputs: dict, source_names: dict[str, str]) -> str:
    """"RSI 14", "EMA 9 on RSI 14": what TradingView's legend shows."""
    head = {"sma": "SMA", "ema": "EMA", "wma": "WMA", "hma": "HMA", "smma": "SMMA", "dema": "DEMA", "tema": "TEMA", "vwma": "VWMA",
            "lsma": "LSMA", "bb": "BB", "kc": "KC", "dc": "DC", "env": "Env", "psar": "SAR", "rsi": "RSI", "macd": "MACD",
            "stoch": "Stoch", "stochrsi": "Stoch RSI", "cci": "CCI", "mfi": "MFI", "willr": "%R", "roc": "ROC", "mom": "Mom",
            "cmo": "CMO", "atr": "ATR", "adx": "DMI", "aroon": "Aroon", "obv": "OBV", "cmf": "CMF", "chaikin": "Chaikin Osc",
            "efi": "EFI", "ao": "AO", "trix": "TRIX", "tsi": "TSI", "uo": "UO", "vol": "Vol", "stdev": "StdDev", "bbw": "BBW",
            "bbpct": "%B", "vwap": "VWAP", "supertrend": "Supertrend"}.get(st.type, st.name)
    nums = [f"{inputs[x.key]:g}" for x in st.inputs if x.kind != "source"]
    src = inputs.get("source")
    on = f" on {source_names[src]}" if src and src in source_names else (f" {src}" if src and src not in ("close", "hlc3") else "")
    return " ".join([head, *nums]) + on


def compute(bars: list[Bar], specs: list[dict]) -> list[dict]:
    """Each indicator in order; one can use an earlier one's line as its source ("<id>:<output>")."""
    if len(specs) > MAX_STUDIES:
        raise ValueError(f"at most {MAX_STUDIES} indicators on a chart")
    lines: dict[str, Series] = {}
    names: dict[str, str] = {}
    pane_of: dict[str, str] = {}
    cache: dict[str, Series] = {}
    out = []

    def series(name: str) -> Series:
        if name in PRICE_SOURCES:
            if name not in cache:
                cache[name] = price_series(bars, name)
            return cache[name]
        if name in lines:
            return lines[name]
        raise ValueError(f"unknown source {name!r}: pick a price or an indicator above this one")

    for spec in specs:
        st = CATALOG.get(spec.get("type"))
        if st is None:
            raise ValueError(f"unknown indicator {spec.get('type')!r}")
        sid = str(spec.get("id") or f"s{len(out) + 1}")
        inputs = _inputs(st, spec.get("inputs") or {})
        values = st.run(bars, series, inputs)
        src = inputs.get("source")
        name = short_name(st, inputs, names)
        if st.overlay:
            pane = pane_of.get(src.split(":")[0], "price") if src and ":" in src else "price"
        else:
            pane = sid
        for o in st.outputs:
            lines[f"{sid}:{o.key}"] = values[o.key]
            names[f"{sid}:{o.key}"] = name if len(st.outputs) == 1 else f"{name} {o.name}"
        pane_of[sid] = pane
        out.append({"id": sid, "type": st.type, "name": name, "pane": pane, "levels": list(st.levels), "inputs": inputs,
                    "outputs": [{"key": o.key, "name": o.name, "color": o.color, "kind": o.kind,
                                 "values": [None if v is None or not math.isfinite(v) else round(v, 6) for v in values[o.key]]}
                                for o in st.outputs]})
    return out
