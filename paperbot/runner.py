"""The long-running process: a watch tick every 30 s, and a decision cycle every 10 minutes.

The decision cycle runs in a worker thread so the watcher keeps ticking while the AI
thinks. Order placement in both is serialized by app.lock.
"""

from __future__ import annotations

import fcntl
import threading
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from .app import App
from .heartbeat import Heartbeat
from .graph import build_graph, run_cycle
from .prompt import cycle_slot
from .watcher import watch_tick


def warn_config(app: App) -> None:
    """Push config warnings (stale earnings dates, risky combinations) once at startup."""
    from .audit import audit

    warns = [f.message for f in audit(app.cfg, app.today(), app.broker.account().equity) if f.level == "warn"]
    if warns:
        app.journal.log("config_warnings", warnings=warns)
        app.alerts.send("Config warnings", " | ".join(warns[:3]) + (f" (+{len(warns) - 3} more)" if len(warns) > 3 else "")
                        + " Run `paperbot check-config` for details.", "default", ["warning"])


class AlreadyRunning(RuntimeError):
    pass


class AccountNotReady(RuntimeError):
    pass


@contextmanager
def single_instance(lock_path: Path) -> Iterator[None]:
    """Refuse to start if another paperbot process (run or cycle) holds the lock."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise AlreadyRunning(f"another paperbot holds {lock_path}") from e
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


FAILED = object()  # what safe() returns when fn raised
SLOW_TICK_SECONDS = 20


def safe(app: App, where: str, fn: Callable[..., Any], *args: Any) -> Any:
    """Run fn, log and alert on any exception, and keep going. Returns FAILED if fn raised."""
    try:
        return fn(*args)
    except Exception as e:
        app.journal.log("error", where=where, error=repr(e), traceback=traceback.format_exc())
        app.alerts.send(f"paperbot error ({where})", repr(e)[:300], "high", ["warning"])
        return FAILED


def check_account(app: App) -> None:
    """Refuse to start on an account that can't buy options or is blocked from trading, and
    warn if the local clock disagrees with the broker's."""
    try:
        from .preflight import CLOCK_SKEW_WARN_SECONDS, clock_skew_seconds

        skew = clock_skew_seconds(app)
        if abs(skew) > CLOCK_SKEW_WARN_SECONDS:
            app.alerts.send("Clock skew", f"This computer's clock is {skew:+.1f}s off Alpaca's. Quote-age and "
                            "order-timeout checks depend on it; fix the system time.", "high", ["warning"])
    except Exception as e:  # never block startup on the skew check itself
        app.journal.log("clock_check_failed", error=repr(e))
    acct = app.broker.account()
    if acct.trading_blocked:
        raise AccountNotReady(f"account {acct.account_number} is blocked from trading")
    if acct.options_level is not None and acct.options_level < 2:
        raise AccountNotReady(f"options trading level is {acct.options_level}; buying calls and puts needs level 2")


def run_forever(app: App, stop: threading.Event | None = None) -> None:
    stop = stop or threading.Event()
    sched = app.cfg.schedule
    graph = build_graph(app)
    worker: threading.Thread | None = None
    last_slot: str | None = None

    with single_instance(app.cfg.log_dir / "paperbot.lock"):
        check_account(app)
        warn_config(app)
        heartbeat = Heartbeat.from_env(app.journal, app.now)
        mode = "DRY RUN (no orders sent). " if app.funnel.dry_run else ""
        app.journal.log("start", dry_run=app.funnel.dry_run)
        ai = app.llm.name if app.llm else "the AI"
        app.alerts.send("paperbot started", f"{mode}Paper account. Exits checked every "
                        f"{sched.watch_interval_seconds}s, {ai} every {sched.decision_interval_minutes} min.", "low")
        while not stop.is_set():
            started = time.monotonic()
            heartbeat.beat(safe(app, "watch", watch_tick, app) is not FAILED)
            took = time.monotonic() - started
            if took > SLOW_TICK_SECONDS:  # exits and stops were checked late
                app.journal.log("slow_tick", seconds=round(took, 1))
                app.alerts.send("Slow watch tick", f"A protection check took {took:.0f}s (Alpaca or the network may be "
                                "slow). Broker stops still protect positions in between.", "high", ["turtle"])
            slot = cycle_slot(app.now().astimezone(sched.tz), sched.decision_interval_minutes)
            due, triggered = slot != last_slot, app.cycle_requested and slot == last_slot
            if (due or triggered) and (worker is None or not worker.is_alive()):
                last_slot = slot
                app.cycle_requested = False
                worker = threading.Thread(target=safe, args=(app, "cycle", run_cycle, app, graph, triggered),
                                          daemon=True)
                worker.start()
            stop.wait(sched.watch_interval_seconds)
    app.journal.log("stop")
