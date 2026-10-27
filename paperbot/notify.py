"""Phone alerts (ntfy or Telegram) and phone commands.

Phone commands can only make the bot safer: "halt", "flatten" and "status".
Resuming, or anything that opens a trade, has to happen on the machine.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

import httpx

from .config import AlertConfig
from .journal import Journal

PHONE_COMMANDS = {"halt", "flatten", "status"}


class Channel(Protocol):
    def send(self, title: str, message: str, priority: str, tags: list[str]) -> None: ...
    def poll_commands(self) -> list[str]: ...


class _State:
    """Remembers the last command id seen, so a restart doesn't replay old commands."""

    def __init__(self, path: Path):
        self.path = path

    def get(self) -> str | None:
        try:
            return json.loads(self.path.read_text()).get("last_id")
        except (OSError, ValueError):
            return None

    def set(self, value: str) -> None:
        self.path.write_text(json.dumps({"last_id": value}))


class NtfyChannel:
    def __init__(self, server: str, topic: str, control_topic: str | None, token: str | None, state_path: Path,
                 http: httpx.Client | None = None):
        if not topic:
            raise ValueError("NTFY_TOPIC is not set")
        self.server = server.rstrip("/")
        self.topic = topic
        self.control_topic = control_topic or None
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.state = _State(state_path)
        self.http = http or httpx.Client(timeout=10)

    def send(self, title: str, message: str, priority: str, tags: list[str]) -> None:
        headers = {**self.headers, "Title": title, "Priority": priority}
        if tags:
            headers["Tags"] = ",".join(tags)
        self.http.post(f"{self.server}/{self.topic}", content=message.encode(), headers=headers).raise_for_status()

    def poll_commands(self) -> list[str]:
        if not self.control_topic:
            return []
        since = self.state.get() or "10m"  # on first run, ignore anything older than 10 minutes
        r = self.http.get(f"{self.server}/{self.control_topic}/json", params={"poll": "1", "since": since},
                          headers=self.headers)
        r.raise_for_status()
        commands = []
        for line in r.text.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("event") != "message":
                continue
            self.state.set(event["id"])
            commands.append(str(event.get("message", "")).strip().lower())
        return commands


class TelegramChannel:
    def __init__(self, bot_token: str, chat_id: str, state_path: Path, http: httpx.Client | None = None):
        if not bot_token or not chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")
        self.base = f"https://api.telegram.org/bot{bot_token}"
        self.chat_id = str(chat_id)
        self.state = _State(state_path)
        self.http = http or httpx.Client(timeout=10)

    def send(self, title: str, message: str, priority: str, tags: list[str]) -> None:
        self.http.post(f"{self.base}/sendMessage", json={
            "chat_id": self.chat_id,
            "text": f"{title}\n{message}",
            "disable_notification": priority in ("min", "low"),
        }).raise_for_status()

    def poll_commands(self) -> list[str]:
        last = self.state.get()
        params = {"timeout": 0, **({"offset": int(last) + 1} if last else {})}
        r = self.http.get(f"{self.base}/getUpdates", params=params)
        r.raise_for_status()
        commands = []
        for update in r.json().get("result", []):
            self.state.set(str(update["update_id"]))
            msg = update.get("message") or {}
            # Only accept commands from your own chat.
            if str(msg.get("chat", {}).get("id")) != self.chat_id:
                continue
            commands.append(str(msg.get("text", "")).strip().lower().lstrip("/"))
        return commands


class ConsoleChannel:
    def send(self, title: str, message: str, priority: str, tags: list[str]) -> None:
        print(f"[alert:{priority}] {title}: {message}")

    def poll_commands(self) -> list[str]:
        return []


class Alerts:
    """Wraps a channel so alert failures are logged, never raised, and repeats are suppressed."""

    def __init__(self, channel: Channel, cfg: AlertConfig, journal: Journal, now: Callable[[], datetime]):
        self.channel = channel
        self.cfg = cfg
        self.journal = journal
        self.now = now
        self._sent: dict[tuple[str, str], datetime] = {}
        self._lock = threading.Lock()

    def send(self, title: str, message: str, priority: str = "default", tags: list[str] | None = None) -> None:
        now = self.now()
        key = (title, message)
        with self._lock:
            last = self._sent.get(key)
            if last is not None and (now - last).total_seconds() < self.cfg.dedupe_seconds:
                return
            self._sent[key] = now
        self.journal.log("alert", title=title, message=message, priority=priority)
        try:
            self.channel.send(title, message, priority, tags or [])
        except Exception as e:  # an alert outage must never stop trading or exits
            self.journal.log("alert_failed", title=title, error=repr(e))

    def poll_commands(self) -> list[str]:
        try:
            return self.channel.poll_commands()
        except Exception as e:
            self.journal.log("command_poll_failed", error=repr(e))
            return []


def build_channel(cfg: AlertConfig, state_dir: Path) -> Channel:
    if cfg.provider == "ntfy":
        return NtfyChannel(
            server=cfg.ntfy_server,
            topic=os.environ.get("NTFY_TOPIC", ""),
            control_topic=os.environ.get("NTFY_CONTROL_TOPIC"),
            token=os.environ.get("NTFY_TOKEN"),
            state_path=state_dir / "ntfy_state.json",
        )
    if cfg.provider == "telegram":
        return TelegramChannel(
            bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
            state_path=state_dir / "telegram_state.json",
        )
    return ConsoleChannel()
