"""Optional heartbeat for a dead man's switch such as healthchecks.io.

paperbot pings HEALTHCHECK_URL after each successful watch tick (at most once a minute) and
pings HEALTHCHECK_URL/fail when a tick errors. If the pings stop (crash, sleep, network), the
service alerts you. It catches the one failure the bot can't report itself: not running.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Callable

import httpx

from .journal import Journal


class Heartbeat:
    def __init__(self, url: str | None, journal: Journal, now: Callable[[], datetime],
                 min_interval_s: float = 60, http: httpx.Client | None = None):
        self.url = (url or "").rstrip("/") or None
        self.journal = journal
        self.now = now
        self.min_interval_s = min_interval_s
        self.http = http or httpx.Client(timeout=5)
        self._last: datetime | None = None

    @classmethod
    def from_env(cls, journal: Journal, now: Callable[[], datetime]) -> Heartbeat:
        return cls(os.environ.get("HEALTHCHECK_URL"), journal, now)

    def beat(self, ok: bool) -> None:
        if not self.url:
            return
        now = self.now()
        if ok and self._last is not None and (now - self._last).total_seconds() < self.min_interval_s:
            return
        try:
            self.http.get(self.url if ok else f"{self.url}/fail")
            self._last = now
        except Exception as e:  # a monitoring outage must never affect trading
            self.journal.log("heartbeat_failed", error=repr(e))
