"""`paperbot preflight`: a pre-open checklist. Read-only, except one tiny AI call.

Incident reviews (Knight Capital, exchange outages) keep finding the same thing: the safety
nets nobody exercised were the ones that failed. This checks the pieces paperbot relies on
before the market opens.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .app import App

CLOCK_SKEW_WARN_SECONDS = 5


def clock_skew_seconds(app: App) -> float:
    """Local clock minus Alpaca's. Quote ages and order timeouts assume they agree."""
    broker_now = app.broker.clock().timestamp
    return (datetime.now(timezone.utc) - broker_now.astimezone(timezone.utc)).total_seconds()


def run_checks(app: App) -> list[tuple[bool, str]]:
    from .audit import account_findings, audit
    from .runner import AccountNotReady, check_account

    out: list[tuple[bool, str]] = []

    def check(name, fn):
        try:
            out.append((True, f"{name}: {fn()}"))
        except Exception as e:
            out.append((False, f"{name}: {e}"))

    check("broker", lambda: f"connected, equity ${app.broker.account().equity:,.0f}")

    def account():
        try:
            check_account(app)
        except AccountNotReady as e:
            raise RuntimeError(str(e)) from e
        return "; ".join(f.message for f in account_findings(app.broker) if f.level == "ok") or "ok"
    check("account", account)

    def skew():
        s = clock_skew_seconds(app)
        if abs(s) > CLOCK_SKEW_WARN_SECONDS:
            raise RuntimeError(f"local clock is {s:+.1f}s off Alpaca's; fix the system time (quote-age checks depend on it)")
        return f"{s:+.1f}s"
    check("clock", skew)

    def ai():
        if app.llm is None:
            return "skipped"
        turn = app.llm.turn("Reply with the JSON decision only.",
                            [app.llm.user_message('Return {"market_view": "ok", "proposals": []}.')], [], False)
        if turn.stop != "end_turn":
            raise RuntimeError(turn.error or f"stop_reason {turn.raw_stop}")
        return f"{app.llm.name} answered"
    check("ai", ai)

    def config():
        warns = [f.message for f in audit(app.cfg, app.today(), app.broker.account().equity) if f.level == "warn"]
        if warns:
            raise RuntimeError(" | ".join(warns))
        return "no warnings"
    check("config", config)

    def halts():
        h = app.killswitch.entries_halted(app.today())
        if h:
            raise RuntimeError(f"halted: {h} (paperbot resume clears HALT and HALT_ALL)")
        return "none"
    check("kill switch", halts)

    def lock():
        from .runner import AlreadyRunning, single_instance
        try:
            with single_instance(app.cfg.log_dir / "paperbot.lock"):
                return "no other paperbot running"
        except AlreadyRunning:
            return "a paperbot is already running (expected if you started it)"
    check("instance", lock)
    return out
