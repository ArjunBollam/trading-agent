"""Wires the pieces together. Tests build an App with fakes; the CLI builds the real one."""

from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .broker import AlpacaPaperBroker, Broker
from .config import Config, load_dotenv
from .journal import Journal
from .notify import Alerts, Channel, build_channel
from .orders import OrderFunnel
from .risk import KillSwitch, RiskGate


@dataclass
class App:
    cfg: Config
    broker: Broker
    gate: RiskGate
    killswitch: KillSwitch
    journal: Journal
    alerts: Alerts
    funnel: OrderFunnel
    llm: Any  # an llm.LLM adapter (Claude, Grok or Groq), or None for commands that don't need one
    now: Callable[[], datetime]
    lock: threading.RLock
    last_signal_check: datetime | None = None
    last_fill_check: datetime | None = None
    excursions: dict = field(default_factory=dict)  # symbol -> [entry, worst %, best %], for MAE/MFE
    ai_failures: int = 0  # consecutive failed AI calls
    rehearsal: bool = False  # `paperbot rehearse`: dry run that ignores market hours and data age
    cycle_requested: bool = False  # set by a setup alert when schedule.trigger_cycle_on_signal is on
    reported_fills: set | None = None  # order ids already alerted; seeded from today's journal
    reconciled: set = field(default_factory=set)  # positions already checked for who opened them

    def today(self):
        return self.now().astimezone(self.cfg.schedule.tz).date()


def build_app(cfg: Config, broker: Broker, llm: Any, channel: Channel, dry_run: bool = False,
              now: Callable[[], datetime] | None = None) -> App:
    tz = cfg.schedule.tz
    now = now or (lambda: datetime.now(tz))
    lock = threading.RLock()
    journal = Journal(cfg.log_dir, now)
    alerts = Alerts(channel, cfg.alerts, journal, now)
    gate = RiskGate(cfg)
    killswitch = KillSwitch(cfg.log_dir)
    funnel = OrderFunnel(broker, gate, journal, alerts, cfg, now, lock, dry_run=dry_run, killswitch=killswitch)
    return App(cfg, broker, gate, killswitch, journal, alerts, funnel, llm, now, lock)


def build_default_app(config_path: str | Path, dry_run: bool = False, need_llm: bool = True,
                      log_subdir: str | None = None) -> App:
    config_path = Path(config_path)
    env_file = config_path.parent / ".env"
    if env_file.exists() and env_file.stat().st_mode & 0o077:
        print(f"warning: {env_file} is readable by other users; run: chmod 600 {env_file}", file=sys.stderr)
    load_dotenv(env_file)
    cfg = Config.load(config_path)
    if log_subdir:  # e.g. rehearsals: keep their journal and kill-switch files apart
        cfg.log_dir = cfg.log_dir / log_subdir
    broker = AlpacaPaperBroker(
        os.environ.get("ALPACA_API_KEY", ""),
        os.environ.get("ALPACA_SECRET_KEY", ""),
        options_feed=cfg.options_feed,
        stock_feed=cfg.stock_feed,
    )
    llm = None
    if need_llm:
        from .llm import make_llm

        llm = make_llm(cfg)  # the provider set by llm.provider in config.yaml
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    return build_app(cfg, broker, llm, build_channel(cfg.alerts, cfg.log_dir), dry_run=dry_run)
