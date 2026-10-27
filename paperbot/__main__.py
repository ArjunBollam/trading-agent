"""Command line: python -m paperbot <command>."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Config


PROFILES = {
    "default": "config.yaml",
    "scalp": "config.scalp.yaml",
    "aggressive": "config.aggressive.yaml",
}

COMPARE = [
    ("AI every (min)", lambda c: c.schedule.decision_interval_minutes),
    ("checks every (s)", lambda c: c.schedule.watch_interval_seconds),
    ("extra cycle on setups", lambda c: "yes" if c.schedule.trigger_cycle_on_signal else "no"),
    ("underlyings", lambda c: " ".join(c.risk.universe)),
    ("days to expiry", lambda c: f"{c.risk.min_dte}-{c.risk.max_dte}"),
    ("max spread %", lambda c: c.risk.max_spread_pct),
    ("risk per trade %", lambda c: c.risk.max_risk_per_trade_pct),
    ("daily loss limit %", lambda c: c.risk.daily_loss_limit_pct),
    ("positions / entries a day", lambda c: f"{c.risk.max_positions} / {c.risk.max_entries_per_day}"),
    ("stop / target %", lambda c: f"{c.exits.stop_loss_pct:g} / {c.exits.take_profit_pct:g}"),
    ("breakeven at %", lambda c: c.exits.breakeven_at_pct or "-"),
    ("trailing", lambda c: f"{c.exits.trail_pct:g}% after +{c.exits.trail_start_pct:g}%" if c.exits.trail_pct else "-"),
    ("time stop (min)", lambda c: c.exits.max_hold_minutes or "-"),
    ("close everything at", lambda c: f"{c.exits.close_all_at:%H:%M}" if c.exits.close_all_at else "-"),
    ("no new entries after", lambda c: f"{c.schedule.no_new_entries_after:%H:%M}"),
]


def compare_profiles(folder: Path) -> str:
    cfgs = {name: Config.load(folder / f) for name, f in PROFILES.items() if (folder / f).exists()}
    width = max(len(label) for label, _ in COMPARE)
    lines = [f"{'':{width}}  " + "".join(f"{n:>24}" for n in cfgs)]
    for label, get in COMPARE:
        lines.append(f"{label:{width}}  " + "".join(f"{str(get(c)):>24}" for c in cfgs.values()))
    lines.append("")
    lines.append("Start one with:  .venv/bin/python -m paperbot --profile <name> run   (one profile at a time)")
    return "\n".join(lines)


def robinhood_command(args, config_path: Path) -> int:
    """`scan` and `robinhood-login`. They need only Robinhood, not Alpaca or the AI."""
    import os
    from datetime import datetime

    from . import formations
    from .config import load_dotenv
    from .journal import Journal
    from .livelist import LiveList
    from .notify import Alerts, build_channel
    from .overrides import Overrides
    from .robinhood import RobinhoodError, RobinhoodMCP, mask
    from .scanner import Scanner, format_result, scan_forever

    load_dotenv(config_path.parent / ".env")
    cfg = Config.load(config_path)
    try:
        rh = RobinhoodMCP(os.environ.get("ROBINHOOD_MCP_URL", ""), cfg.scan.oauth_callback_port)
    except ValueError as e:
        print(f"paperbot: {e}", file=sys.stderr)
        return 2
    if args.command == "robinhood-login":
        for a in rh.accounts():
            name = a.get("nickname") or a.get("brokerage_account_type", "")
            print(f"{mask(a['account_number'])}  {name}  {a.get('type', '')}{'  (default)' if a.get('is_default') else ''}")
        print("Signed in. Put the account to scan in .env as ROBINHOOD_ACCOUNT_NUMBER.")
        return 0
    account = os.environ.get("ROBINHOOD_ACCOUNT_NUMBER", "")
    if not account:
        print("paperbot: ROBINHOOD_ACCOUNT_NUMBER is not set (run `paperbot robinhood-login` to list accounts)",
              file=sys.stderr)
        return 2
    def symbols(text: str) -> list[str]:
        return [s.strip().upper() for s in text.split(",") if s.strip()]

    overrides = Overrides(cfg.log_dir / "settings.json", cfg.scan)  # changes from the web page, over config.yaml
    extra = {"watchlist": list(dict.fromkeys(cfg.scan.watchlist + symbols(args.symbols)))}
    if args.tick is not None:
        extra["tick_seconds"] = args.tick
    applied: dict = {}

    def settings():
        """The scan settings: config.yaml, then the web page's changes, then this command line's flags."""
        src = overrides.scan()
        if applied.get("src") is not src:
            applied["src"], applied["out"] = src, src.model_copy(update=extra | {"watchlist": list(
                dict.fromkeys(src.watchlist + extra["watchlist"]))})
        return applied["out"]

    cfg.scan = settings()
    only = set(symbols(args.only)) or None
    live_list = None
    if args.live and not only:  # the live list (config.yaml, then `paperbot live-symbols`), followed as it changes
        live_list = LiveList(cfg.log_dir / cfg.scan.live_symbols_file, cfg.scan.live_symbols)
        only = set(live_list.symbols()) or None
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    unknown = sorted(set(cfg.scan.disabled_patterns + cfg.scan.log_only_patterns) - set(formations.LABELS))
    if unknown:
        print(f"paperbot: unknown pattern names in config.yaml, ignored: {', '.join(unknown)} "
              "(`paperbot patterns` lists them)", file=sys.stderr)

    def now():
        return datetime.now(cfg.schedule.tz)

    journal = Journal(cfg.log_dir, now)
    alerts = Alerts(build_channel(cfg.alerts, cfg.log_dir), cfg.alerts, journal, now)
    scanner = Scanner(rh, cfg, account, journal, alerts, now, only,
                      state_path=cfg.log_dir / "scan_state.json" if args.live else None)
    try:
        if args.live:
            what = f"{', '.join(sorted(only))} on {mask(account)}" if only else mask(account)
            every = f", and the live price every {cfg.scan.tick_seconds} s" if cfg.scan.tick_seconds else ""
            print(f"Scanning {what} after every {cfg.scan.bar_minutes}-minute bar{every}. Ctrl-C to stop.", flush=True)
            if live_list:
                print(f"Symbols follow {live_list.path} (`paperbot live-symbols add QQQ`).", flush=True)
            scan_forever(scanner, send_alerts=not args.no_alerts, symbols=live_list.symbols if live_list else None,
                         settings=settings)
        else:
            print(format_result(scanner.scan(send_alerts=not args.no_alerts, catch_up=args.catch_up)))
    except KeyboardInterrupt:
        return 0
    except RobinhoodError as e:
        print(f"paperbot: {e}", file=sys.stderr)
        return 1
    return 0


def live_symbols_command(args, config_path: Path) -> int:
    """`live-symbols`: the live scan's symbol list. Needs nothing but the config."""
    import json

    from .livelist import LiveList

    cfg = Config.load(config_path)
    live = LiveList(cfg.log_dir / cfg.scan.live_symbols_file, cfg.scan.live_symbols)
    syms = [s for arg in args.symbols for s in arg.split(",") if s.strip()]
    if args.action in ("add", "remove") and not syms:
        print(f"paperbot: live-symbols {args.action} needs at least one symbol", file=sys.stderr)
        return 2
    try:
        if args.action == "add":
            live.add(syms, args.by)
        elif args.action == "remove":
            live.remove(syms, args.by)
        elif args.action == "set":
            live.replace(syms, args.by)
        elif args.action == "reset":
            live.reset()
    except ValueError as e:
        print(f"paperbot: {e}", file=sys.stderr)
        return 2
    info = live.info()
    if args.json:
        print(json.dumps(info))
        return 0
    where = f"{info['path']}, changed {info.get('updated')} by {info.get('by')}" if info["source"] == "file" \
        else "scan.live_symbols in config.yaml"
    print(f"Live scan symbols: {', '.join(info['symbols']) or 'none set: everything you hold plus the watchlist'}")
    print(f"  from {where}. A running `paperbot scan --live` picks up changes within one check.")
    return 0


def patterns_command(args, config_path: Path) -> int:
    """`patterns`: every pattern the scan knows, by group, and what it does with each."""
    import json

    from . import formations

    sc = Config.load(config_path).scan
    rows = []
    for group, kind, label in formations.catalog():
        state = ("off" if group not in sc.pattern_groups or kind in sc.disabled_patterns
                 else "log only" if kind in sc.log_only_patterns else "alert")
        rows.append({"group": group, "kind": kind, "label": label, "state": state})
    if args.json:
        print(json.dumps(rows))
        return 0
    for r in rows:
        print(f"{r['group']:12} {r['kind']:36} {r['label']:40} {r['state']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="paperbot", description="Paper-trading options agent.")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml (default: ./config.yaml)")
    parser.add_argument("--profile", choices=sorted(PROFILES),
                        help="trading strategy: " + ", ".join(f"{k} ({v})" for k, v in PROFILES.items()))
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run forever: watcher every 30 s, the AI every 10 min")
    run.add_argument("--dry-run", action="store_true", help="do everything except send or cancel orders")
    cycle = sub.add_parser("cycle", help="run one decision cycle now")
    cycle.add_argument("--dry-run", action="store_true")
    sub.add_parser("watch-once", help="run one protection tick (exits, stops, loss limit)")
    sub.add_parser("flatten", help="cancel all orders, close all positions, halt for today")
    halt = sub.add_parser("halt", help="stop the AI from opening trades (exits keep running)")
    halt.add_argument("--all", action="store_true", help="stop everything, including exit management")
    sub.add_parser("resume", help="clear HALT and HALT_ALL (a daily loss halt stays until tomorrow)")
    sub.add_parser("status", help="print equity, positions and halt state")
    report = sub.add_parser("report", help="closed trades and win rate, expectancy, profit factor")
    report.add_argument("--days", type=int, default=7, help="look back this many days (default 7)")
    sub.add_parser("test-alert", help="send a test alert to your phone")
    sub.add_parser("profiles", help="compare the trading profiles side by side")
    sim = sub.add_parser("simulate-exits", help="compare exit settings on historical option prices (read-only)")
    sim.add_argument("--underlying", default="SPY")
    sim.add_argument("--days", type=int, default=30, help="past trading days to simulate (default 30)")
    sim.add_argument("--entry-time", default="10:30", help="daily entry time(s), ET, comma-separated (default 10:30)")
    sim.add_argument("--min-dte", type=int, default=7)
    sim.add_argument("--max-dte", type=int, default=14)
    sim.add_argument("--hold-days", type=int, default=3, help="longest hold, in calendar days (default 3)")
    sim.add_argument("--cost-pct", type=float, default=2.0, help="assumed round-trip spread cost, %% of premium")
    sub.add_parser("preflight", help="pre-open checklist: broker, account, clock, AI, config, kill switch")
    sub.add_parser("rehearse", help="one full cycle on real data and the real AI, any time; dry run, never trades")
    ev = sub.add_parser("eval-prompts", help="replay saved prompts to the AI: consistency and agreement (costs tokens)")
    ev.add_argument("--days", type=int, default=5, help="look back this many days (default 5)")
    ev.add_argument("--limit", type=int, default=10, help="at most this many prompts (default 10)")
    ev.add_argument("--repeat", type=int, default=2, help="ask each prompt this many times (default 2)")
    replay = sub.add_parser("replay", help="a day's decisions, orders, fills and alerts, from the journal")
    replay.add_argument("--date", help="YYYY-MM-DD (default: today)")
    check = sub.add_parser("check-config", help="print every parameter, what they imply, and warnings")
    check.add_argument("--broker", action="store_true", help="also check the Alpaca account (read-only)")
    scan = sub.add_parser("scan", help="chart signals on your Robinhood positions (read-only, never trades)")
    scan.add_argument("--live", action="store_true", help="keep scanning after every bar during market hours")
    scan.add_argument("--no-alerts", action="store_true", help="print only; don't send phone alerts")
    scan.add_argument("--catch-up", action="store_true",
                      help="check every bar since the open, not just the last; for scans hours apart")
    scan.add_argument("--symbols", default="", help="extra symbols to scan, comma-separated (adds to scan.watchlist)")
    scan.add_argument("--only", default="",
                      help="scan just these symbols, comma-separated, e.g. SPY for a live scalping scan beside the "
                           "scheduled one; positions are still read, so signals say how they bear on what you hold")
    scan.add_argument("--tick", type=int, metavar="SECONDS",
                      help="with --live, also check the live price every SECONDS between bar closes and alert "
                           "level crosses inside the forming bar (overrides scan.tick_seconds)")
    sub.add_parser("robinhood-login", help="sign in to Robinhood's MCP server and list your accounts")
    live = sub.add_parser("live-symbols", help="list or change the live scan's symbols; a running scan follows at once")
    live.add_argument("action", nargs="?", default="list", choices=["list", "add", "remove", "set", "reset"],
                      help="reset goes back to scan.live_symbols in config.yaml")
    live.add_argument("symbols", nargs="*", help="tickers, space- or comma-separated")
    live.add_argument("--by", default="cli", help="who changed it, kept with the list (e.g. ai, web)")
    live.add_argument("--json", action="store_true", help="print the result as JSON")
    pats = sub.add_parser("patterns", help="every chart pattern the scan knows, by group, and whether it alerts")
    pats.add_argument("--json", action="store_true", help="print the list as JSON")
    web = sub.add_parser("web", help="the web page: live scanner dashboard, signals, patterns, settings, the paper bot")
    web.add_argument("--port", type=int, default=8765, help="port on 127.0.0.1 (default 8765)")
    web.add_argument("--open", action="store_true", help="open it in your browser")
    args = parser.parse_args(argv)
    config_path = Path(args.config)
    if args.profile:  # the profile files sit next to the default config
        config_path = config_path.parent / PROFILES[args.profile]

    if args.command == "profiles":
        print(compare_profiles(config_path.parent))
        return 0

    if args.command == "check-config" and not args.broker:
        from .audit import audit, format_audit

        cfg = Config.load(config_path)
        print(format_audit(cfg, audit(cfg), 100_000))
        return 0

    if args.command == "replay":
        from datetime import date, datetime

        from .journal import Journal
        from .replay import timeline

        cfg = Config.load(config_path)
        day = date.fromisoformat(args.date) if args.date else datetime.now(cfg.schedule.tz).date()
        lines = timeline(Journal(cfg.log_dir, lambda: datetime.now(cfg.schedule.tz)), day)
        print("\n".join(lines) if lines else f"nothing logged on {day}")
        return 0

    # Commands that only touch the kill-switch files work even without keys or network.
    if args.command in ("halt", "resume"):
        from .risk import KillSwitch

        ks = KillSwitch(Config.load(config_path).log_dir)
        if args.command == "halt":
            path = ks.halt("halted from the command line", scope="all" if args.all else "manual")
            print(f"wrote {path}")
        else:
            removed = ks.resume()
            print(f"removed {', '.join(removed)}" if removed else "nothing to resume")
        return 0

    if args.command == "live-symbols":
        return live_symbols_command(args, config_path)
    if args.command == "patterns":
        return patterns_command(args, config_path)
    if args.command == "web":
        from .config import load_dotenv
        from .web.server import serve

        load_dotenv(config_path.parent / ".env")  # for the commands it runs
        serve(config_path, port=args.port, open_browser=args.open)
        return 0
    if args.command in ("scan", "robinhood-login"):
        return robinhood_command(args, config_path)

    from .app import build_default_app
    from .broker import LiveTradingRefused

    need_llm = args.command in ("run", "cycle", "eval-prompts", "preflight", "rehearse")
    dry_run = getattr(args, "dry_run", False) or args.command == "rehearse"
    try:
        app = build_default_app(config_path, dry_run=dry_run, need_llm=need_llm,
                                log_subdir="rehearsal" if args.command == "rehearse" else None)
    except (ValueError, LiveTradingRefused) as e:
        print(f"paperbot: {e}", file=sys.stderr)
        return 2

    if args.command == "run":
        from .runner import AccountNotReady, AlreadyRunning, run_forever

        mode = "DRY RUN, no orders sent" if app.funnel.dry_run else "paper account"
        print(f"paperbot running ({app.llm.name}, {mode}). Activity is logged to {app.cfg.log_dir}/<date>.jsonl "
              "and pushed to your phone. Ctrl-C to stop.", flush=True)
        try:
            run_forever(app)
        except (AlreadyRunning, AccountNotReady) as e:
            print(e, file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("stopped")
        return 0

    if args.command == "cycle":
        from .graph import run_cycle
        from .runner import AlreadyRunning, single_instance

        try:
            with single_instance(app.cfg.log_dir / "paperbot.lock"):
                state = run_cycle(app)
        except AlreadyRunning as e:
            print(e, file=sys.stderr)
            return 1
        print(f"halt: {state.get('halt')}" if state.get("halt") else f"hold: {state.get('hold_reason')}"
              if state.get("hold_reason") else f"decision: {state.get('decision')}")
        for r in state.get("results", []):
            print(f"  {r.status:9} {r.kind:5} {r.symbol} x{r.qty}: {r.detail}")
        return 0

    if args.command == "watch-once":
        from .watcher import watch_tick

        rep = watch_tick(app)
        print(rep if rep is not None else "nothing to do (market closed or HALT_ALL)")
        return 0

    if args.command == "flatten":
        app.funnel.flatten_all("command line")
        app.killswitch.halt("flattened from the command line", scope="day", today=app.today())
        app.alerts.send("Flattened", "Closed every position from the command line; halted for today.", "high")
        print("flattened and halted for today")
        return 0

    if args.command == "status":
        from .watcher import status_text

        print(status_text(app))
        return 0

    if args.command == "report":
        from datetime import timedelta

        from .performance import (closed_trades, excursion_summary, execution, execution_line, format_report,
                                  rejection_summary)

        now = app.now()
        trades = [t for t in closed_trades(app.broker.orders_since(now - timedelta(days=args.days + 7)))
                  if t.closed_at >= now - timedelta(days=args.days)]
        days = [app.today() - timedelta(days=d) for d in range(args.days + 1)]
        reasons = {e["client_order_id"]: e.get("reason", "") for d in days
                   for e in app.journal.events(d, "order_submitted") if e.get("kind") == "exit"}
        equity = app.broker.account().equity
        print(format_report(trades, reasons, args.days, app.cfg.schedule.tz, risk_threshold=equity * 0.05))
        submitted = [e for d in days for e in app.journal.events(d, "order_submitted")]
        fills = [e for d in days for e in app.journal.events(d, "fill")]
        print(execution_line(execution(submitted, fills)))
        rejected = [e for d in days for e in app.journal.events(d, "order_rejected") if e.get("kind") != "stop"]
        if (why := rejection_summary(rejected)):
            print(why)
        excursions = [e for d in sorted(days) for e in app.journal.events(d, "excursion")]
        if (mae := excursion_summary(trades, excursions)):
            print(mae)
        return 0

    if args.command == "rehearse":
        from .graph import run_cycle

        app.rehearsal = True
        state = run_cycle(app)
        today = app.today()
        for e in app.journal.events(today, "llm_turn")[-6:]:
            tools = ", ".join(t["name"] for t in e.get("tool_uses", [])) or "-"
            usage = e.get("usage") or {}
            tokens = usage.get("prompt_tokens") or usage.get("input_tokens")
            print(f"AI turn {e['turn']}: stop={e['stop_reason']} tools={tools} prompt tokens={tokens}")
        d = state.get("decision")
        print(f"decision: {d.market_view if d else state.get('hold_reason') or state.get('halt')}")
        for p in (d.proposals if d else []):
            print(f"  proposal: {p.action} {p.qty} {p.symbol}: {p.reason}")
        for r in state.get("results", []):
            print(f"  gate: {r.status} {r.kind} {r.symbol} x{r.qty}: {r.detail}")
        print("Rehearsal only: dry run, nothing was sent to the broker.")
        return 0

    if args.command == "preflight":
        from .preflight import run_checks

        results = run_checks(app)
        for ok, line in results:
            print(f"[{'ok' if ok else 'FAIL'}] {line}")
        return 0 if all(ok for ok, _ in results) else 1

    if args.command == "eval-prompts":
        from datetime import timedelta

        from .evaluate import evaluate
        from .prompt import system_prompt

        days = [app.today() - timedelta(days=d) for d in range(args.days, -1, -1)]
        print(f"Replaying up to {args.limit} saved prompts x{args.repeat} to {app.llm.name} (tools off, no orders)...")
        print(evaluate(app.llm, system_prompt(app), app.journal, days, args.limit, args.repeat).summary())
        return 0

    if args.command == "simulate-exits":
        from datetime import time as dtime

        from . import simulate as sim_mod

        x = app.cfg.exits
        print(f"Fetching {args.days} days of {args.underlying} option bars (cached in logs/simcache)...")
        samples = []
        for t in args.entry_time.split(","):
            hh, mm = (int(v) for v in t.strip().split(":"))
            samples += sim_mod.collect(app.broker, args.underlying, args.days, dtime(hh, mm),
                                       (args.min_dte, args.max_dte), args.hold_days, app.cfg.schedule.tz,
                                       app.cfg.log_dir / "simcache", app.today(), app.now())
        current = sim_mod.from_config(x)
        variants = sim_mod.grid(current)
        tz = app.cfg.schedule.tz
        summaries = sim_mod.run(samples, variants, x.close_at_dte, tz, args.cost_pct)
        stability = sim_mod.rank_stability(samples, variants, x.close_at_dte, tz, args.cost_pct)
        print(sim_mod.format_results(summaries, current, len({s.day for s in samples}), args.cost_pct, stability))
        return 0

    if args.command == "check-config":
        from .audit import account_findings, audit, format_audit

        equity = app.broker.account().equity
        findings = audit(app.cfg, app.today(), equity) + account_findings(app.broker)
        print(format_audit(app.cfg, findings, equity))
        return 0

    if args.command == "test-alert":
        app.alerts.send("paperbot test", "If you can read this on your phone, alerts work.", "default", ["white_check_mark"])
        print("sent (check logs/ for alert_failed if nothing arrives)")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
