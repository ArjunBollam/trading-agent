"""The web page's server: a JSON API over what the command line does, plus the page itself.

It reads what the other processes write (the scanner's state file, the journal, the kill-switch files)
and changes things only through the same files and commands the command line uses, so the page, the
command line and an AI tool all see one state. It never talks to Robinhood itself, except by running
the read-only `paperbot scan` command when asked. The Charts tab and the alerts read market data (candles and trades,
read-only) from Alpaca, and overnight (24-hour market) candles from Robinhood with the scanner's sign-in, borrowed
read-only: it never signs in or renews it.

Local only: it listens on 127.0.0.1, rejects other Host headers (DNS rebinding), and every change
(POST) needs the token printed into the page at start, which another website can't read or send.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from starlette.concurrency import run_in_threadpool

from .. import alerts as alerting
from .. import chartdata, explain, formations, outlook
from ..config import Config, ScanConfig
from ..features import Bar
from ..journal import Journal
from ..livelist import LiveList
from ..overrides import NOT_HERE, Overrides
from ..patterns import LABELS
from ..risk import KillSwitch
from ..scanner import market_hours, next_scan_start, scan_hours, session_at
from ..studies import catalog as study_catalog
from ..trendlines import TICKER as LINE_TICKER
from ..guard import Guard
from ..trendlines import Drawings
from ..watchlists import Watchlists

STATIC = Path(__file__).parent / "static"


class VersionedStaticFiles(StaticFiles):
    """Cache assets with a page-generated version URL for a year."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200 and any(part.startswith(b"v=") for part in scope.get("query_string", b"").split(b"&")):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Commands the page can run, as `python -m paperbot <args>`. Nothing else can be run.
COMMANDS = {
    "status": {"args": ["status"], "label": "Status", "about": "Equity, positions and halt state (Alpaca paper)"},
    "report": {"args": ["report"], "label": "Report", "about": "Closed trades, win rate, expectancy, profit factor"},
    "replay": {"args": ["replay"], "label": "Replay a day", "about": "A day's decisions, orders, fills and alerts",
               "date": True},
    "check-config": {"args": ["check-config"], "label": "Check config", "about": "Every parameter, what it implies, warnings"},
    "profiles": {"args": ["profiles"], "label": "Profiles", "about": "The trading profiles side by side"},
    "preflight": {"args": ["preflight"], "label": "Preflight", "about": "Pre-open checklist: broker, account, clock, AI, config"},
    "simulate-exits": {"args": ["simulate-exits"], "label": "Simulate exits",
                       "about": "Exit settings compared on historical option prices (read-only)"},
    "rehearse": {"args": ["rehearse"], "label": "Rehearse", "about": "One full cycle on real data and the AI; never trades (uses AI tokens)"},
    "scan": {"args": ["scan", "--no-alerts", "--catch-up"], "label": "Scan now",
             "about": "Every signal since the open on your Robinhood positions; print only (read-only)"},
    "outlook": {"args": ["outlook"], "label": "Outlook now",
               "about": "The key support and the bullish and bearish scenarios, as the chart is now (read-only)"},
    "test-alert": {"args": ["test-alert"], "label": "Test alert", "about": "Send a test alert to your phone", "changes": True},
    "watch-once": {"args": ["watch-once"], "label": "Protection tick",
                   "about": "One pass of exits, stops and the loss limit; can close paper positions", "changes": True},
    "cycle": {"args": ["cycle"], "label": "Run one cycle",
              "about": "One AI decision cycle; can place paper orders", "changes": True},
    "flatten": {"args": ["flatten"], "label": "Flatten",
                "about": "Cancel all orders, close every paper position, halt for today", "changes": True},
}


def market_for(c: Config, held: dict = {}):  # noqa: B006 - one Market (and its cache) per process
    """Alpaca market data for the charts and alerts, made the first time it's needed."""
    if "market" not in held:
        from ..market import AlpacaMarket, Market

        night = None
        if os.environ.get("ROBINHOOD_MCP_URL"):
            from ..robinhood import RobinhoodMCP

            # the overnight (24-hour market) candles: the scanner's Robinhood sign-in, borrowed read-only, never renewed here
            night = RobinhoodMCP(os.environ["ROBINHOOD_MCP_URL"], c.scan.oauth_callback_port, borrow=True)
        held["market"] = Market(AlpacaMarket(os.environ.get("ALPACA_API_KEY", ""), os.environ.get("ALPACA_SECRET_KEY", "")), c.schedule.tz,
                                night_source=night)
    return held["market"]


def create_app(config_path: Path, token: str | None = None, timeout: float = 300,
               hosts: tuple[str, ...] = ("127.0.0.1", "localhost"), market=None) -> Starlette:
    config_path = Path(config_path).resolve()
    token = token or secrets.token_urlsafe(24)

    def data(c: Config):
        return market if market is not None else market_for(c)

    def cfg() -> Config:
        return Config.load(config_path)  # read fresh: config.yaml may have been edited

    def overrides(c: Config) -> Overrides:
        return Overrides(c.log_dir / "settings.json", c.scan)

    def live_list(c: Config) -> LiveList:
        return LiveList(c.log_dir / c.scan.live_symbols_file, c.scan.live_symbols)

    def today(c: Config) -> date:
        return datetime.now(c.schedule.tz).date()

    def day_param(request: Request, c: Config) -> date:
        d = request.query_params.get("date") or ""
        return date.fromisoformat(d) if DATE.match(d) else today(c)

    def journal(c: Config) -> Journal:
        return Journal(c.log_dir, lambda: datetime.now(c.schedule.tz))

    async def body(request: Request) -> dict:
        try:
            data = await request.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def error(message: str, status: int = 400) -> JSONResponse:
        return JSONResponse({"error": message}, status_code=status)

    # --- page ---

    asset_version = {"fingerprint": None, "value": None}

    def version() -> str:
        """Changes whenever the page's files do: new URLs (no stale cache), and an open page reloads itself."""
        files = [path for path in sorted(STATIC.iterdir()) if path.is_file()]
        fingerprint = tuple((path.name, path.stat().st_mtime_ns, path.stat().st_size) for path in files)
        if fingerprint != asset_version["fingerprint"]:
            digest = hashlib.sha256()
            for path in files:
                digest.update(path.name.encode())
                digest.update(path.read_bytes())
            asset_version["fingerprint"] = fingerprint
            asset_version["value"] = digest.hexdigest()[:16]
        return asset_version["value"]

    async def index(request: Request) -> Response:
        html = (STATIC / "index.html").read_text().replace("{{TOKEN}}", token).replace("{{V}}", version())
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    # --- scanner ---

    def scanner_state(c: Config) -> dict:
        path = c.log_dir / "scan_state.json"
        now = datetime.now(c.schedule.tz)
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            return {"status": "off", "detail": "the live scanner hasn't written its state yet", "market_open": market_hours(now)}
        age = (now - datetime.fromisoformat(state["updated"])).total_seconds()
        sessions = state.get("sessions") or ["regular"]
        bar = state.get("bar_minutes", 5) * 60
        if scan_hours(now, sessions):
            expected = (state.get("tick_seconds") or 0) or bar
            status = "running" if age <= max(3 * expected, 90) else "stale"
        else:  # between sessions the scanner checks in once a candle
            status = "idle" if age <= 2 * bar + 120 else "stale"
            state["resumes"] = next_scan_start(now, sessions).isoformat()
        state |= {"status": status, "age_seconds": round(age), "market_open": market_hours(now)}
        return state

    async def state(request: Request) -> Response:
        return JSONResponse(scanner_state(cfg()))

    async def chart(request: Request) -> Response:
        """The 1-minute candles the live scanner keeps for the page's 1m charts."""
        if request.query_params.get("minutes", "1") != "1":
            return error("only 1-minute candles are kept beside the scanner's own")
        try:
            return JSONResponse(json.loads((cfg().log_dir / "scan_chart_1m.json").read_text()))
        except (OSError, ValueError):
            return JSONResponse({"updated": None, "bar_minutes": 1, "symbols": {}})

    def signal_rows(c: Config, day: date) -> list[dict]:
        quiet = set(overrides(c).scan().log_only_patterns)  # for entries written before the journal kept "weak"
        rows = []
        for e in journal(c).events(day):
            if e.get("event") == "chart_signal":
                rows.append({"ts": e["ts"], "time": e["bar_time"], "symbol": e["symbol"], "kind": e["kind"],
                             "label": LABELS.get(e["kind"], e["kind"]), "side": e["side"], "price": e["price"],
                             "detail": e.get("detail"), "level": e.get("level"), "held": e.get("held"),
                             "against": e.get("against_position"), "weak": bool(e.get("weak", e["kind"] in quiet)), "live": False,
                             "context": e.get("context"), "context_score": e.get("context_score"),
                             "conflict": e.get("conflict"), "flow": e.get("flow"), "flow_detail": e.get("flow_detail")})
            elif e.get("event") == "live_cross":
                rows.append({"ts": e["ts"], "time": e["ts"], "symbol": e["symbol"], "kind": "live_cross",
                             "label": f"LIVE {'above' if e['side'] == 'buy' else 'below'} {e['level']}",
                             "side": e["side"], "price": e["price"],
                             "detail": f"crossed the {e['level']} {e['level_price']:.2f} inside the forming candle",
                             "level": e["level"], "held": None, "against": e.get("against_position"),
                             "weak": bool(e.get("weak")), "live": True,
                             "flow": e.get("flow"), "flow_detail": e.get("flow_detail")})
        rows.sort(key=lambda r: r["ts"], reverse=True)
        return rows

    async def signals(request: Request) -> Response:
        c = cfg()
        return JSONResponse({"date": day_param(request, c).isoformat(), "signals": signal_rows(c, day_param(request, c))})

    def audit_state(c: Config) -> dict:
        """The signal audit's latest totals (signal_audit.py), and whether it's running."""
        now = datetime.now(c.schedule.tz)
        try:
            state = json.loads((c.log_dir / "audit_state.json").read_text())
            age = (now - datetime.fromisoformat(state["updated"])).total_seconds()
        except (OSError, ValueError, KeyError):
            return {"status": "off"}
        on = scan_hours(now, overrides(c).scan().sessions)
        return state | {"status": "idle" if not on else "running" if age <= 240 else "stale", "age_seconds": round(age)}

    async def audit(request: Request) -> Response:
        c = cfg()
        day = day_param(request, c)
        try:
            data = json.loads((c.log_dir / "audit" / f"{day.isoformat()}.json").read_text())
        except (OSError, ValueError):
            data = {"day": day.isoformat(), "updated": None, "records": [], "counts": {}, "table": [], "total": 0}
        days = sorted((p.stem for p in (c.log_dir / "audit").glob("*.json") if DATE.match(p.stem)), reverse=True)
        return JSONResponse(data | {"days": days, "status": audit_state(c)["status"] if day == today(c) else "past"})

    def reports(c: Config) -> list[dict]:
        out = []
        for path in outlook.saved(c.log_dir / "reports")[:60]:
            try:
                r = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            out.append({k: r.get(k) for k in ("id", "symbol", "at", "day", "title", "kind", "price", "bias")})
        return out

    async def report_list(request: Request) -> Response:
        return JSONResponse({"reports": reports(cfg())})

    async def report_view(request: Request) -> Response:
        """A report, and what price has done since: from the live scanner's candles on its own day, then as it was written down."""
        c = cfg()
        paths = {p.stem: p for p in outlook.saved(c.log_dir / "reports")}
        path = paths.get(request.path_params["id"])
        if path is None:
            return error("no such report", 404)
        r = json.loads(path.read_text())
        if not r.get("outcome"):
            view = (scanner_state(c).get("symbols") or {}).get(r["symbol"]) or {}
            tz = c.schedule.tz
            rows = [Bar(datetime.fromtimestamp(b[0], tz), *b[1:6]) for b in view.get("bars") or []]
            r["outcome"] = outlook.follow(r, rows)
        return JSONResponse(r)

    async def dates(request: Request) -> Response:
        c = cfg()
        days = sorted((p.stem for p in c.log_dir.glob("*.jsonl") if DATE.match(p.stem)), reverse=True)
        return JSONResponse({"dates": days, "today": today(c).isoformat()})

    async def live_symbols(request: Request) -> Response:
        c = cfg()
        lst = live_list(c)
        if request.method == "POST":
            data = await body(request)
            action, syms = data.get("action"), data.get("symbols") or []
            try:
                if action == "add":
                    lst.add(syms, "web")
                elif action == "remove":
                    lst.remove(syms, "web")
                elif action == "set":
                    lst.replace(syms, "web")
                elif action == "reset":
                    lst.reset()
                else:
                    return error("action must be add, remove, set or reset")
            except ValueError as e:
                return error(str(e))
        return JSONResponse(lst.info())

    # --- patterns and settings ---

    def pattern_rows(sc: ScanConfig) -> list[dict]:
        rows = []
        for group, kind, label in formations.catalog():
            state = ("off" if group not in sc.pattern_groups or kind in sc.disabled_patterns
                     else "log" if kind in sc.log_only_patterns else "alert")
            desc = formations.CANDLES.get(kind, ("", ""))[1]
            meaning, example = explain.EXPLAIN[kind]
            rows.append({"group": group, "kind": kind, "label": label, "state": state, "about": desc,
                         "meaning": meaning, "example": example})
        return rows

    async def patterns(request: Request) -> Response:
        c = cfg()
        ov = overrides(c)
        if request.method == "POST":
            data = await body(request)
            sc = ov.scan()
            groups, disabled, quiet = list(sc.pattern_groups), set(sc.disabled_patterns), set(sc.log_only_patterns)
            if "groups" in data:
                groups = [g for g in formations.GROUPS if g in (data["groups"] or [])]
            for kind, want in (data.get("set") or {}).items():
                if kind not in formations.LABELS or want not in ("alert", "log", "off"):
                    return error(f"unknown pattern or state: {kind}={want}")
                disabled.discard(kind)
                quiet.discard(kind)
                if want == "off":
                    disabled.add(kind)
                elif want == "log":
                    quiet.add(kind)
            try:
                ov.update({"pattern_groups": groups, "disabled_patterns": sorted(disabled),
                           "log_only_patterns": sorted(quiet)})
            except ValueError as e:
                return error(str(e))
        sc = ov.scan()
        return JSONResponse({"groups": list(formations.GROUPS), "on": list(sc.pattern_groups),
                             "patterns": pattern_rows(sc)})

    async def settings(request: Request) -> Response:
        c = cfg()
        ov = overrides(c)
        if request.method == "POST":
            data = await body(request)
            try:
                if data.get("reset"):
                    ov.reset(None if data["reset"] is True else list(data["reset"]))
                else:
                    ov.update(data.get("changes") or {})
            except ValueError as e:
                return error(str(e).split("\n For further information")[0], 422)
        schema = ScanConfig.model_json_schema()
        fields = {k: v for k, v in schema["properties"].items() if k not in NOT_HERE}
        return JSONResponse({"fields": fields, "defs": schema.get("$defs", {}),
                             "base": {k: v for k, v in c.scan.model_dump(mode="json").items() if k in fields},
                             "effective": {k: v for k, v in ov.scan().model_dump(mode="json").items() if k in fields},
                             **ov.info()})

    async def config_view(request: Request) -> Response:
        c = cfg()
        return JSONResponse({"path": str(config_path), "config": c.model_dump(mode="json")})

    # --- paper-trading bot ---

    def halts(c: Config) -> dict:
        ks = KillSwitch(c.log_dir)
        return {"manual": ks._read("HALT"), "all": ks.all_halted(), "day": ks._read(f"HALT_{today(c).isoformat()}")}

    async def killswitch(request: Request) -> Response:
        c = cfg()
        if request.method == "POST":
            action = (await body(request)).get("action")
            ks = KillSwitch(c.log_dir)
            if action == "halt":
                ks.halt("halted from the web page", scope="manual")
            elif action == "halt_all":
                ks.halt("halted everything from the web page", scope="all")
            elif action == "resume":
                ks.resume()
            else:
                return error("action must be halt, halt_all or resume")
        return JSONResponse(halts(c))

    async def journal_view(request: Request) -> Response:
        c = cfg()
        day = day_param(request, c)
        events = journal(c).events(day)
        counts: dict[str, int] = {}
        for e in events:
            counts[e.get("event", "?")] = counts.get(e.get("event", "?"), 0) + 1
        kind = request.query_params.get("event")
        rows = [e for e in events if not kind or e.get("event") == kind]
        return JSONResponse({"date": day.isoformat(), "counts": counts, "events": rows[-2000:][::-1]})

    async def commands(request: Request) -> Response:
        return JSONResponse([{"name": k, "label": v["label"], "about": v["about"], "changes": v.get("changes", False),
                              "date": v.get("date", False)} for k, v in COMMANDS.items()])

    def run_command(name: str, day: str | None) -> dict:
        spec = COMMANDS[name]
        args = list(spec["args"]) + (["--date", day] if spec.get("date") and day else [])
        cmd = [sys.executable, "-m", "paperbot", "--config", str(config_path), *args]
        started = time.monotonic()
        try:
            out = subprocess.run(cmd, cwd=config_path.parent, capture_output=True, text=True, timeout=timeout, check=False,
                                 env=os.environ | {"PYTHONUNBUFFERED": "1"})
            code, text = out.returncode, out.stdout + (("\n" + out.stderr) if out.stderr.strip() else "")
        except subprocess.TimeoutExpired as e:
            code, text = -1, f"{e.stdout or ''}\n(stopped after {timeout:.0f} s)"
        return {"command": "paperbot " + " ".join(args), "code": code, "output": text,
                "seconds": round(time.monotonic() - started, 1)}

    def run(request: Request) -> Response:  # sync: Starlette runs it in a thread, so a slow command blocks nothing
        name = request.path_params["name"]
        if name not in COMMANDS:
            return error(f"no such command: {name}", 404)
        day = request.query_params.get("date")
        if day and not DATE.match(day):
            return error("date must be YYYY-MM-DD")
        return JSONResponse(run_command(name, day))

    def status_snapshot(c: Config, now: datetime | None = None) -> dict:
        now = now or datetime.now(c.schedule.tz)
        scanner = scanner_state(c)
        return {"version": version(), "now": now.isoformat(), "market_open": market_hours(now),
                "session": session_at(now), "scanner": scanner, "live_symbols": live_list(c).info(), "halts": halts(c),
                "report": next((r for r in reports(c) if r["kind"] == "morning" and r["day"] == now.date().isoformat()), None),
                "audit": {k: v for k, v in audit_state(c).items() if k in ("status", "age_seconds", "updated", "day", "total", "counts")}}

    def page_snapshot(c: Config) -> dict:
        now = datetime.now(c.schedule.tz)
        day = now.date()
        sigs = signal_rows(c, day)
        overview = status_snapshot(c, now)
        overview.update({"signals_today": len(sigs), "against_today": sum(1 for s in sigs if s["against"])})
        return {"overview": overview, "state": overview["scanner"], "signals": {"date": day.isoformat(), "signals": sigs}}

    async def overview(request: Request) -> Response:
        return JSONResponse(page_snapshot(cfg())["overview"])

    async def status(request: Request) -> Response:
        return JSONResponse(status_snapshot(cfg()))

    async def dashboard(request: Request) -> Response:
        return JSONResponse(page_snapshot(cfg()))

    # --- guard: a token on every change, local hosts only ---

    class TokenGuard:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http" and scope["method"] not in ("GET", "HEAD", "OPTIONS"):
                headers = dict(scope["headers"])
                sent = headers.get(b"x-paperbot-token", b"").decode()
                if not secrets.compare_digest(sent, token):
                    await JSONResponse({"error": "missing or wrong token; reload the page"}, status_code=403)(scope, receive, send)
                    return
            await self.app(scope, receive, send)

    async def token_view(request: Request) -> Response:
        """The current token, for a page loaded before the server restarted. Only this page's own origin can
        read it (no CORS headers, and other Host names are refused), the same as the token in the page."""
        return JSONResponse({"token": token}, headers={"Cache-Control": "no-store"})

    glossary = explain.glossary()

    async def glossary_view(request: Request) -> Response:
        """What every signal kind means, with an example, for the chart and list tooltips."""
        return JSONResponse(glossary)

    # --- the Charts tab: candles, indicators, watchlists and alerts (Alpaca market data, read-only) ---

    def watchlists(c: Config) -> Watchlists:
        return Watchlists(c.log_dir / "watchlists.json", live_list(c).info().get("symbols") or ["SPY"])

    async def chart_data(request: Request) -> Response:
        c = cfg()
        try:
            req = chartdata.ChartRequest.from_json(await body(request))
            lines = drawings(c).get(req.symbol) if LINE_TICKER.match(req.symbol) else []
            result = await run_in_threadpool(chartdata.build, data(c), c.scan, c.schedule.tz, req, datetime.now(c.schedule.tz), lines)
        except ValueError as e:
            return error(str(e))
        except Exception as e:  # the data source: keys, network, a ticker it doesn't have
            return error(f"couldn't load the chart's data: {e}", 502)
        if request.query_params.get("format") == "csv":
            name = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{req.symbol}_{result['tf']}_{result['type']}") + ".csv"
            return Response(chartdata.csv_for(result, c.schedule.tz), media_type="text/csv",
                            headers={"Content-Disposition": f'attachment; filename="{name}"'})
        return JSONResponse(result)

    def drawings(c: Config) -> Drawings:
        return Drawings(c.log_dir / "drawings.json")

    async def drawings_view(request: Request) -> Response:
        """A symbol's drawn lines: GET ?symbol=SPY, or POST {symbol, drawings} to replace them."""
        c = cfg()
        if request.method == "POST":
            d = await body(request)
            try:
                saved = drawings(c).save(str(d.get("symbol") or ""), d.get("drawings"), c.schedule.tz)
            except ValueError as e:
                return error(str(e))
            return JSONResponse({"symbol": str(d.get("symbol")).upper(), "drawings": saved})
        sym = (request.query_params.get("symbol") or "").upper()
        return JSONResponse({"symbol": sym, "drawings": drawings(c).get(sym) if sym else []})

    async def guard_view(request: Request) -> Response:
        """The position guard: each position's line under it and ahead of it, and what broke (paperbot/guard.py)."""
        g = Guard(cfg().log_dir).load()
        g.pop("seen", None)
        return JSONResponse(g)

    async def studies_view(request: Request) -> Response:
        return JSONResponse({"studies": study_catalog()})

    async def watchlists_view(request: Request) -> Response:
        c = cfg()
        w = watchlists(c)
        if request.method == "POST":
            try:
                return JSONResponse(w.save(await body(request)))
            except ValueError as e:
                return error(str(e))
        return JSONResponse(w.load())

    async def quotes(request: Request) -> Response:
        c = cfg()
        syms = [str(s).strip().upper() for s in ((await body(request)).get("symbols") or [])][:100]

        def look() -> list[dict]:
            out = []
            for s in syms:
                try:
                    last, prev = data(c).last_price(s)
                except Exception:
                    last, prev = None, None
                out.append({"symbol": s, "last": last, "prev": prev,
                            "change": None if last is None or not prev else last - prev,
                            "pct": None if last is None or not prev else (last / prev - 1) * 100})
            return out

        return JSONResponse({"quotes": await run_in_threadpool(look)})

    def alert_store(c: Config) -> alerting.AlertStore:
        return alerting.AlertStore(c.log_dir)

    def alert_rows(c: Config) -> list[dict]:
        rows = []
        for a in alert_store(c).load():
            try:
                rows.append(a | {"description": alerting.describe(a)})
            except (KeyError, TypeError, ValueError):
                rows.append(a | {"description": "this alert can't be read"})
        return rows

    async def alerts_view(request: Request) -> Response:
        c = cfg()
        store = alert_store(c)
        if request.method == "POST":
            try:
                a = alerting.validate(await body(request), watchlists(c).by_name())
                store.add(a, datetime.now(c.schedule.tz))
            except ValueError as e:
                return error(str(e))
        since = request.query_params.get("since") or ""
        events = [e for e in store.events() if not since or e["at"] > since]
        night = any(a.get("session") == "24h" and a.get("status") == "active" for a in store.load())
        return JSONResponse({"alerts": alert_rows(c), "events": events, "ops": alerting.OPS, "triggers": alerting.TRIGGERS,
                             "checking": alerting.checking_hours(datetime.now(c.schedule.tz), night), "overnight": night})

    async def alert_change(request: Request) -> Response:
        c = cfg()
        store = alert_store(c)
        aid, d = request.path_params["id"], await body(request)
        try:
            if d.get("action") == "delete":
                store.remove(aid)
            elif d.get("action") == "edit":
                store.update(aid, {"alert": alerting.validate(d.get("alert") or {}, watchlists(c).by_name()), "status": "active"},
                             datetime.now(c.schedule.tz))
            elif d.get("action") in ("pause", "resume"):
                store.update(aid, {"status": "paused" if d["action"] == "pause" else "active"}, datetime.now(c.schedule.tz))
            else:
                return error("action must be delete, edit, pause or resume")
        except KeyError:
            return error("there's no such alert", 404)
        except ValueError as e:
            return error(str(e))
        return JSONResponse({"alerts": alert_rows(c)})

    routes = [
        Route("/", index),
        Route("/api/token", token_view),
        Route("/api/glossary", glossary_view),
        Route("/api/overview", overview),
        Route("/api/status", status),
        Route("/api/dashboard", dashboard),
        Route("/api/state", state),
        Route("/api/chart", chart),
        Route("/api/signals", signals),
        Route("/api/audit", audit),
        Route("/api/reports", report_list),
        Route("/api/reports/{id}", report_view),
        Route("/api/dates", dates),
        Route("/api/live-symbols", live_symbols, methods=["GET", "POST"]),
        Route("/api/patterns", patterns, methods=["GET", "POST"]),
        Route("/api/settings", settings, methods=["GET", "POST"]),
        Route("/api/config", config_view),
        Route("/api/killswitch", killswitch, methods=["GET", "POST"]),
        Route("/api/journal", journal_view),
        Route("/api/commands", commands),
        Route("/api/commands/{name}", run, methods=["POST"]),
        Route("/api/chart-data", chart_data, methods=["POST"]),
        Route("/api/studies", studies_view),
        Route("/api/guard", guard_view),
        Route("/api/drawings", drawings_view, methods=["GET", "POST"]),
        Route("/api/watchlists", watchlists_view, methods=["GET", "POST"]),
        Route("/api/quotes", quotes, methods=["POST"]),
        Route("/api/alerts", alerts_view, methods=["GET", "POST"]),
        Route("/api/alerts/{id}", alert_change, methods=["POST"]),
        Mount("/static", VersionedStaticFiles(directory=STATIC), name="static"),
    ]
    app = Starlette(routes=routes, middleware=[
        Middleware(TrustedHostMiddleware, allowed_hosts=list(hosts)),
        Middleware(TokenGuard),
    ])
    app.state.token = token
    return app


def report_due(c: Config, now: datetime) -> bool:
    """Whether today's morning report should be made: from its time on, on a weekday, until it's there."""
    r = c.daily_report
    local = now.astimezone(ZoneInfo(r.timezone))
    if not r.enabled or local.weekday() >= 5 or not r.at <= local.time() < r.until:
        return False
    day = now.astimezone(c.schedule.tz).date().isoformat()
    for path in outlook.saved(c.log_dir / "reports"):
        if not path.stem.startswith(day):
            break  # newest first
        try:
            if json.loads(path.read_text()).get("kind") == "morning":
                return False
        except (OSError, ValueError):
            continue
    return True


def morning_reports(config_path: Path, run=subprocess.run, sleep=time.sleep, now=lambda: datetime.now().astimezone()) -> None:
    """Make each day's report at its time, in its own process. A failure is tried again after 5 minutes; a day without trading isn't."""
    given_up: date | None = None
    while True:
        try:
            c = Config.load(config_path)
            at = now()
            if at.date() != given_up and report_due(c, at):
                out = run([sys.executable, "-m", "paperbot", "--config", str(config_path), "outlook", "--morning"], cwd=config_path.parent,
                          capture_output=True, text=True, timeout=300, check=False)
                print(f"{at:%H:%M} morning report: " + (out.stdout.splitlines()[0] if out.returncode == 0 and out.stdout else
                                                         (out.stdout + out.stderr).strip()[-300:]), flush=True)
                if out.returncode == 3:
                    given_up = at.date()
                elif out.returncode:
                    sleep(270)
        except Exception as e:  # nothing here may stop the page
            print(f"morning report failed: {e!r}", flush=True)
        sleep(30)


def today_close(now: datetime, held: dict = {}) -> datetime | None:  # noqa: B006 - one lookup per day per process
    """Today's close from Alpaca's exchange calendar (13:00 on half days), looked up once a day; None if the market is
    closed today or the calendar can't be reached (tried again after 5 minutes)."""
    if held.get("day") == now.date():
        return held["close"]
    if held.get("retry") and now < held["retry"]:
        return None
    try:
        from ..broker import AlpacaPaperBroker

        if "broker" not in held:  # paper only (it refuses live keys); used here for the calendar alone
            held["broker"] = AlpacaPaperBroker(os.environ.get("ALPACA_API_KEY", ""), os.environ.get("ALPACA_SECRET_KEY", ""))
        t = held["broker"].session_close(now.date())
    except Exception as e:
        print(f"exchange calendar failed: {e!r}", flush=True)
        held["retry"] = now + timedelta(minutes=5)
        return None
    held.update(day=now.date(), close=datetime.combine(now.date(), t, tzinfo=now.tzinfo) if t else None, retry=None)
    return held["close"]


def alert_checks(config_path: Path, sleep=time.sleep) -> None:
    """Check the page's alerts every 15 seconds while there's trading (4:00 to 20:00 on weekdays)."""
    from ..journal import Journal
    from ..notify import Alerts, build_channel

    phone = None
    while True:
        try:
            c = Config.load(config_path)
            now = datetime.now(c.schedule.tz)
            store = alerting.AlertStore(c.log_dir)
            active = [a for a in store.load() if a.get("status") == "active"]
            if phone is None:
                journal = Journal(c.log_dir, lambda: datetime.now(c.schedule.tz))
                phone = Alerts(build_channel(c.alerts, c.log_dir), c.alerts, journal, lambda: datetime.now(c.schedule.tz))
            # the position guard: your positions (from the scanner) against the lines on their symbols, around the clock
            try:
                state = json.loads((c.log_dir / "scan_state.json").read_text())
            except (OSError, ValueError):
                state = {}
            if (state.get("holdings") or state.get("options")) and alerting.checking_hours(now, True):
                m, lines_store = market_for(c), Drawings(c.log_dir / "drawings.json")

                def candles(sym: str):
                    if m.night_source is not None:
                        return m.night(sym, 1, 1)
                    return m.minutes(sym, 1)[0]

                for e in Guard(c.log_dir).run(state, candles, lines_store.get, c.schedule.tz, now,
                                              lambda title, msg, pri: phone.send(title, msg, pri), today_close(now)):
                    print(f"{now:%H:%M:%S} guard: {e['text']}", flush=True)
            if active and alerting.checking_hours(now, any(a.get("session") == "24h" for a in active)):
                lists = Watchlists(c.log_dir / "watchlists.json", []).by_name()
                lines = Drawings(c.log_dir / "drawings.json")
                for e in alerting.evaluate(store, market_for(c), lists, now, lambda title, msg: phone.send(title, msg, "high"),
                                           lines.find):
                    print(f"{now:%H:%M:%S} alert: {e['message']}", flush=True)
        except Exception as e:  # nothing here may stop the page
            print(f"alert check failed: {e!r}", flush=True)
        sleep(15)


def serve(config_path: Path, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = False) -> None:
    import uvicorn

    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("the web page only listens on this computer (127.0.0.1)")
    app = create_app(config_path)
    threading.Thread(target=morning_reports, args=(Path(config_path).resolve(),), daemon=True).start()
    threading.Thread(target=alert_checks, args=(Path(config_path).resolve(),), daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    print(f"paperbot web: {url}  (this computer only; Ctrl-C to stop)", flush=True)
    if open_browser:
        import webbrowser

        threading.Timer(1.0, webbrowser.open, [url]).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")

