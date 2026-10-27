"""Append-only JSONL event log: one file per trading day, one line per event.

Every prompt, tool call, AI answer, risk decision and order goes here, so any
decision can be replayed later.
"""

from __future__ import annotations

import dataclasses
import json
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable


def _default(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    return repr(obj)


class Journal:
    def __init__(self, log_dir: Path, now: Callable[[], datetime]):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._now = now
        self._lock = threading.Lock()

    def path_for(self, day: date) -> Path:
        return self.log_dir / f"{day.isoformat()}.jsonl"

    def log(self, event: str, **data: Any) -> None:
        now = self._now()
        line = json.dumps({"ts": now.isoformat(), "event": event, **data}, default=_default)
        with self._lock, self.path_for(now.date()).open("a") as f:
            f.write(line + "\n")

    def events(self, day: date, event: str | None = None) -> list[dict]:
        path = self.path_for(day)
        if not path.exists():
            return []
        out = []
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event is None or row.get("event") == event:
                out.append(row)
        return out

    def entry_reasons(self, days: list[date]) -> dict[str, str]:
        """Latest reason the AI gave for buying each contract."""
        reasons: dict[str, str] = {}
        for day in days:
            for row in self.events(day, "order_submitted"):
                if row.get("kind") == "entry" and row.get("reason"):
                    reasons[row["symbol"]] = row["reason"]
        return reasons
