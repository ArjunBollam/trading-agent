"""Read-only Robinhood access over Robinhood's MCP server, for `paperbot scan`.

This is a real-money account, so the client can call only the tools in READ_ONLY_TOOLS:
accounts, stock and option positions, option contract details and price bars. Any other tool
name (placing, cancelling or exercising anything) is refused in code before a request is made,
and a test checks the list.

Sign-in is OAuth in your browser, once. The tokens are kept in TOKEN_PATH (mode 600) and
refreshed by the MCP SDK.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

from .features import Bar

READ_ONLY_TOOLS = frozenset({"get_accounts", "get_equity_positions", "get_equity_historicals",
                             "get_option_positions", "get_option_instruments", "get_equity_quotes",
                             "get_option_quotes"})
TOKEN_PATH = Path.home() / ".config" / "paperbot" / "robinhood-oauth.json"
INTERVALS = {1: "minute", 5: "5minute", 10: "10minute", 30: "30minute"}
# Bars per request: a bigger reply can pass the MCP client's 1 MB message limit (one bar is ~350 bytes
# on the wire). Symbols are split into as many calls as that takes, up to the server's 10 a call.
MAX_BARS_PER_CALL = 2000


def symbols_per_call(bars_each: int) -> int:
    return max(1, min(10, MAX_BARS_PER_CALL // max(bars_each, 1)))


class ReadOnlyViolation(RuntimeError):
    pass


class RobinhoodError(RuntimeError):
    pass


@dataclass(frozen=True)
class Holding:
    symbol: str
    qty: float  # negative for a short
    avg_cost: float | None


@dataclass(frozen=True)
class OptionHolding:
    symbol: str  # the underlying
    right: str  # "call" or "put"
    strike: float
    expiration: date
    qty: float  # contracts, negative for a short
    avg_price: float | None  # per share, like the quote
    option_id: str = ""  # the contract's instrument id, for its quote
    multiplier: float = 100.0  # shares per contract

    @property
    def direction(self) -> int:
        """+1 if it gains when the underlying rises (long call, short put), -1 if it gains when it falls."""
        return (1 if self.right == "call" else -1) * (1 if self.qty > 0 else -1)

    @property
    def label(self) -> str:
        return (f"{abs(self.qty):g} {'short ' if self.qty < 0 else ''}{self.symbol} {self.strike:g} {self.right}"
                f"{'s' if abs(self.qty) != 1 else ''} expiring {self.expiration:%m/%d}")


@dataclass
class Snapshot:
    holdings: list[Holding]
    bars: dict[str, list[Bar]]  # intraday, oldest first, interpolated bars dropped
    daily: dict[str, list[Bar]]  # empty when not requested
    options: list[OptionHolding] = field(default_factory=list)


@dataclass
class Live:
    """Positions and live prices between bar closes: last trade per symbol, mark per option contract."""
    holdings: list[Holding]
    options: list[OptionHolding]
    prices: dict[str, float]
    marks: dict[str, float]  # by option_id


class MarketSource(Protocol):
    def snapshot(self, account: str, symbols: list[str], bar_minutes: int, intraday_start: datetime,
                 daily_start: datetime | None, extended: bool = False) -> Snapshot: ...

    def live(self, account: str, symbols: list[str],
             positions: tuple[list[Holding], list[OptionHolding]] | None = None) -> Live: ...

    def chart_bars(self, symbols: list[str], bar_minutes: int, start: datetime, extended: bool = False) -> dict[str, list[Bar]]: ...


def mask(account: str) -> str:
    return "••" + account[-4:]


def parse_bars(results: list[dict]) -> dict[str, list[Bar]]:
    out: dict[str, list[Bar]] = {}
    for r in results:
        rows = [b for b in r.get("bars") or [] if not b.get("interpolated")]
        out[r["symbol"]] = sorted(
            (Bar(datetime.fromisoformat(b["begins_at"].replace("Z", "+00:00")), float(b["open_price"]),
                 float(b["high_price"]), float(b["low_price"]), float(b["close_price"]), float(b["volume"] or 0))
             for b in rows), key=lambda b: b.t)
    return out


def parse_holdings(positions: list[dict]) -> list[Holding]:
    out = []
    for p in positions:
        qty = float(p["quantity"])
        if p.get("type") == "short" and qty > 0:
            qty = -qty
        if qty:
            avg = p.get("average_buy_price")
            out.append(Holding(p["symbol"], qty, float(avg) if avg else None))
    return out


def parse_option_holdings(positions: list[dict], instruments: dict[str, dict]) -> list[OptionHolding]:
    """Positions don't say call or put, or the strike; those come from the contract (instruments, by id).
    average_price is per contract, so it's divided by the multiplier to match a per-share quote."""
    out = []
    for p in positions:
        qty, inst = float(p["quantity"]), instruments.get(p["option_id"])
        if not qty or inst is None:
            continue
        if p.get("type") == "short" and qty > 0:
            qty = -qty
        mult = float(p.get("trade_value_multiplier") or inst.get("trade_value_multiplier") or 100)
        avg = p.get("average_price")
        out.append(OptionHolding(p["chain_symbol"], inst["type"], float(inst["strike_price"]),
                                 date.fromisoformat(p.get("expiration_date") or inst["expiration_date"]), qty,
                                 abs(float(avg)) / mult if avg else None, p["option_id"], mult))
    return out


def parse_quotes(equity: list[dict], options: list[dict]) -> tuple[dict[str, float], dict[str, float]]:
    """Last trade per symbol (in the regular session or pre-market and after hours, whichever printed
    last) and mark per option contract; entries without a price are left out."""
    prices = {}
    for r in equity:
        q = r.get("quote") or {}
        trades = [(q.get(at) or "", float(q[p])) for p, at in (("last_trade_price", "venue_last_trade_time"),
                                                                ("last_non_reg_trade_price", "venue_last_non_reg_trade_time"))
                  if q.get(p)]
        if trades:
            prices[q["symbol"]] = max(trades)[1]  # RFC 3339 UTC times compare as text
    marks = {}
    for r in options:
        q = r.get("quote") or {}
        if q.get("mark_price"):
            marks[q["instrument_id"]] = float(q["mark_price"])
    return prices, marks


class _FileTokenStorage:
    def __init__(self, path: Path):
        self.path = path

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def _write(self, key: str, value: dict) -> None:
        data = self._read()
        data[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        t = self._read().get("tokens")
        return OAuthToken.model_validate(t) if t else None

    async def set_tokens(self, tokens) -> None:
        self._write("tokens", tokens.model_dump(mode="json", exclude_none=True))

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull

        c = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(c) if c else None

    async def set_client_info(self, client_info) -> None:
        self._write("client_info", client_info.model_dump(mode="json", exclude_none=True))


class _BrowserLogin:
    """Opens the sign-in page and catches the redirect on 127.0.0.1. The listener is bound before
    the browser opens, so a fast redirect can't arrive before anything is listening."""

    def __init__(self, port: int, open_browser: Callable[[str], Any] = webbrowser.open, timeout: float = 300):
        self.port = port
        self.open_browser = open_browser
        self.timeout = timeout
        self.server: HTTPServer | None = None
        self.result: dict[str, str] = {}

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}/callback"

    async def redirect(self, url: str) -> None:
        login = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                if "code" not in q and "error" not in q:
                    self.send_response(404)
                    self.end_headers()
                    return
                login.result = q
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"paperbot: Robinhood sign-in finished. You can close this tab.")

            def log_message(self, *args):
                pass

        self.result = {}
        self.server = HTTPServer(("127.0.0.1", self.port), Handler)
        self.server.timeout = 1
        print(f"Opening Robinhood sign-in in your browser. If it doesn't open, visit:\n{url}")
        self.open_browser(url)

    async def callback(self):
        from mcp.client.auth import AuthorizationCodeResult, OAuthFlowError

        if self.server is None:
            raise OAuthFlowError("sign-in page was never opened")

        def wait() -> None:
            deadline = time.monotonic() + self.timeout
            with self.server:
                while not self.result and time.monotonic() < deadline:
                    self.server.handle_request()

        await asyncio.to_thread(wait)
        self.server = None
        if "error" in self.result:
            raise OAuthFlowError(f"Robinhood sign-in failed: {self.result['error']}")
        if "code" not in self.result:
            raise OAuthFlowError("Robinhood sign-in timed out")
        return AuthorizationCodeResult(code=self.result["code"], state=self.result.get("state"),
                                       iss=self.result.get("iss"))


class RobinhoodMCP:
    """One connection per snapshot: positions, then bars for everything held plus the watchlist."""

    def __init__(self, url: str, callback_port: int = 33418, token_path: Path = TOKEN_PATH,
                 open_browser: Callable[[str], Any] = webbrowser.open):
        if not url:
            raise ValueError("ROBINHOOD_MCP_URL is not set (see .env.example)")
        self.url = url
        self.token_path = token_path
        self.login = _BrowserLogin(callback_port, open_browser)

    def _auth(self):
        from mcp.client.auth import OAuthClientProvider
        from mcp.shared.auth import OAuthClientMetadata

        meta = OAuthClientMetadata(client_name="paperbot", redirect_uris=[self.login.redirect_uri],
                                   grant_types=["authorization_code", "refresh_token"], response_types=["code"],
                                   token_endpoint_auth_method="none")
        return OAuthClientProvider(self.url, meta, _FileTokenStorage(self.token_path),
                                   redirect_handler=self.login.redirect, callback_handler=self.login.callback)

    async def _with_client(self, work: Callable[[Callable], Any]):
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        try:
            async with httpx2.AsyncClient(auth=self._auth(), timeout=60) as http:
                async with Client(streamable_http_client(self.url, http_client=http)) as client:
                    async def call(name: str, args: dict) -> Any:
                        return await _call(client, name, args)
                    return await work(call)
        except BaseExceptionGroup as eg:  # the SDK's task groups wrap errors; surface the real one
            while isinstance(eg, BaseExceptionGroup) and eg.exceptions:
                eg = eg.exceptions[0]
            raise eg from None

    def accounts(self) -> list[dict]:
        async def work(call):
            return (await call("get_accounts", {}))["accounts"]
        return asyncio.run(self._with_client(work))

    def snapshot(self, account: str, symbols: list[str], bar_minutes: int, intraday_start: datetime,
                 daily_start: datetime | None, extended: bool = False) -> Snapshot:
        """With `extended`, the intraday bars include pre-market and after-hours trading; daily bars
        are always the regular session's."""
        async def work(call):
            holdings, options = await _positions(call, account)
            wanted = list(dict.fromkeys([h.symbol for h in holdings] + [o.symbol for o in options]
                                        + [s.upper() for s in symbols]))
            bars: dict[str, list[Bar]] = {}
            daily: dict[str, list[Bar]] = {}
            now = datetime.now(UTC)
            days = (now - intraday_start).days + 1  # calendar days, so weekends make this an overestimate
            n = symbols_per_call(days * (16 * 60 if extended else 390) // bar_minutes)
            for i in range(0, len(wanted), n):
                data = await call("get_equity_historicals", {
                    "symbols": wanted[i:i + n], "interval": INTERVALS[bar_minutes], "start_time": _rfc3339(intraday_start),
                    "bounds": "extended" if extended else "regular"})
                bars |= parse_bars(data.get("results") or [])
            if daily_start is not None:
                n = symbols_per_call((now - daily_start).days * 5 // 7 + 1)
                for i in range(0, len(wanted), n):
                    data = await call("get_equity_historicals", {
                        "symbols": wanted[i:i + n], "interval": "day", "start_time": _rfc3339(daily_start)})
                    daily |= parse_bars(data.get("results") or [])
            return Snapshot(holdings, bars, daily, options)
        return asyncio.run(self._with_client(work))

    def chart_bars(self, symbols: list[str], bar_minutes: int, start: datetime, extended: bool = False) -> dict[str, list[Bar]]:
        """Just the candles since `start`, for a chart. Read a day at a time, so a reply stays under the size limit."""
        async def work(call):
            out: dict[str, list[Bar]] = {s.upper(): [] for s in symbols}
            names, t, now = list(out), start, datetime.now(start.tzinfo)
            while t < now:
                end = min((t + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0), now)  # to midnight
                if t.weekday() < 5:
                    n = symbols_per_call(min(960 if extended else 390, int((end - t).total_seconds() // 60) + 1) // bar_minutes)
                    for i in range(0, len(names), n):
                        data = await call("get_equity_historicals", {
                            "symbols": names[i:i + n], "interval": INTERVALS[bar_minutes], "start_time": _rfc3339(t),
                            "bounds": "extended" if extended else "regular"} | ({"end_time": _rfc3339(end)} if end < now else {}))
                        for sym, rows in parse_bars(data.get("results") or []).items():
                            out.setdefault(sym, []).extend(rows)
                t = end
            return out
        return asyncio.run(self._with_client(work))

    def live(self, account: str, symbols: list[str],
             positions: tuple[list[Holding], list[OptionHolding]] | None = None) -> Live:
        """Live prices, and positions unless `positions` (from an earlier call) is passed to reuse."""
        async def work(call):
            holdings, options = positions or await _positions(call, account)
            wanted = list(dict.fromkeys([s.upper() for s in symbols] + [h.symbol for h in holdings]
                                        + [o.symbol for o in options]))
            equity, quoted = [], []
            if wanted:
                equity = (await call("get_equity_quotes", {"symbols": wanted})).get("results") or []
            ids = [o.option_id for o in options if o.option_id]
            if ids:
                quoted = (await call("get_option_quotes", {"instrument_ids": ids})).get("results") or []
            return Live(holdings, options, *parse_quotes(equity, quoted))
        return asyncio.run(self._with_client(work))


async def _pages(call, name: str, args: dict, key: str) -> list[dict]:
    rows, cursor = [], None
    while True:
        data = await call(name, {**args, **({"cursor": cursor} if cursor else {})})
        rows += data.get(key) or []
        cursor = data.get("next")
        if not cursor:
            return rows


async def _positions(call, account: str) -> tuple[list[Holding], list[OptionHolding]]:
    holdings = parse_holdings(await _pages(call, "get_equity_positions", {"account_number": account}, "positions"))
    held_options = await _pages(call, "get_option_positions", {"account_number": account, "nonzero": True}, "positions")
    instruments = {}
    if held_options:
        ids = ",".join(dict.fromkeys(p["option_id"] for p in held_options))
        instruments = {i["id"]: i for i in await _pages(call, "get_option_instruments", {"ids": ids}, "instruments")}
    return holdings, parse_option_holdings(held_options, instruments)


async def _call(client, name: str, args: dict) -> Any:
    if name not in READ_ONLY_TOOLS:
        raise ReadOnlyViolation(f"{name} is not a read-only tool; paperbot never trades on Robinhood")
    result = await client.call_tool(name, args)
    text = "".join(getattr(c, "text", "") for c in result.content)
    if result.is_error:
        raise RobinhoodError(f"{name}: {text[:300]}")
    try:
        body = json.loads(text)
    except ValueError:
        raise RobinhoodError(f"{name}: not JSON: {text[:300]}") from None
    return body.get("data", body)


def _rfc3339(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
