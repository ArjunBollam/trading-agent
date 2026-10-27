"""The web page's server: a JSON API over what the command line does, plus the page itself.

It reads what the other processes write (the scanner's state file, the journal, the kill-switch files)
and changes things only through the same files and commands the command line uses, so the page, the
command line and an AI tool all see one state. It never talks to Robinhood itself, except by running
the read-only `paperbot scan` command when asked.

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
import time
from datetime import date, datetime
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import formations
from ..config import Config, ScanConfig
from ..journal import Journal
from ..livelist import LiveList
from ..overrides import NOT_HERE, Overrides
from ..patterns import LABELS
from ..risk import KillSwitch
from ..scanner import market_hours

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
    "test-alert": {"args": ["test-alert"], "label": "Test alert", "about": "Send a test alert to your phone", "changes": True},
    "watch-once": {"args": ["watch-once"], "label": "Protection tick",
                   "about": "One pass of exits, stops and the loss limit; can close paper positions", "changes": True},
    "cycle": {"args": ["cycle"], "label": "Run one cycle",
              "about": "One AI decision cycle; can place paper orders", "changes": True},
    "flatten": {"args": ["flatten"], "label": "Flatten",
                "about": "Cancel all orders, close every paper position, halt for today", "changes": True},
}


def create_app(config_path: Path, token: str | None = None, timeout: float = 300,
               hosts: tuple[str, ...] = ("127.0.0.1", "localhost")) -> Starlette:
    config_path = Path(config_path).resolve()
    token = token or secrets.token_urlsafe(24)

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
        expected = (state.get("tick_seconds") or 0) or state.get("bar_minutes", 5) * 60
        running = age <= max(3 * expected, 90) if market_hours(now) else age <= 6 * 3600
        state |= {"status": "running" if running else "stale", "age_seconds": round(age), "market_open": market_hours(now)}
        return state

    async def state(request: Request) -> Response:
        return JSONResponse(scanner_state(cfg()))

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
                             "conflict": e.get("conflict")})
            elif e.get("event") == "live_cross":
                rows.append({"ts": e["ts"], "time": e["ts"], "symbol": e["symbol"], "kind": "live_cross",
                             "label": f"LIVE {'above' if e['side'] == 'buy' else 'below'} {e['level']}",
                             "side": e["side"], "price": e["price"],
                             "detail": f"crossed the {e['level']} {e['level_price']:.2f} inside the forming candle",
                             "level": e["level"], "held": None, "against": e.get("against_position"), "weak": False,
                             "live": True})
        rows.sort(key=lambda r: r["ts"], reverse=True)
        return rows

    async def signals(request: Request) -> Response:
        c = cfg()
        return JSONResponse({"date": day_param(request, c).isoformat(), "signals": signal_rows(c, day_param(request, c))})

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
            rows.append({"group": group, "kind": kind, "label": label, "state": state, "about": desc})
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
                "scanner": scanner, "live_symbols": live_list(c).info(), "halts": halts(c)}

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

    routes = [
        Route("/", index),
        Route("/api/token", token_view),
        Route("/api/overview", overview),
        Route("/api/status", status),
        Route("/api/dashboard", dashboard),
        Route("/api/state", state),
        Route("/api/signals", signals),
        Route("/api/dates", dates),
        Route("/api/live-symbols", live_symbols, methods=["GET", "POST"]),
        Route("/api/patterns", patterns, methods=["GET", "POST"]),
        Route("/api/settings", settings, methods=["GET", "POST"]),
        Route("/api/config", config_view),
        Route("/api/killswitch", killswitch, methods=["GET", "POST"]),
        Route("/api/journal", journal_view),
        Route("/api/commands", commands),
        Route("/api/commands/{name}", run, methods=["POST"]),
        Mount("/static", VersionedStaticFiles(directory=STATIC), name="static"),
    ]
    app = Starlette(routes=routes, middleware=[
        Middleware(TrustedHostMiddleware, allowed_hosts=list(hosts)),
        Middleware(TokenGuard),
    ])
    app.state.token = token
    return app


def serve(config_path: Path, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = False) -> None:
    import uvicorn

    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("the web page only listens on this computer (127.0.0.1)")
    app = create_app(config_path)
    url = f"http://127.0.0.1:{port}/"
    print(f"paperbot web: {url}  (this computer only; Ctrl-C to stop)", flush=True)
    if open_browser:
        import threading
        import webbrowser

        threading.Timer(1.0, webbrowser.open, [url]).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")

