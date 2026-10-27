"""Scan settings changed at runtime (from the web page or an AI tool), applied over config.yaml.

config.yaml stays the hand-edited source, comments and all. Changes made while the app runs go to
log_dir/settings.json as {"scan": {field: value}, "updated": ..., "by": ...}. Every `paperbot scan`
reads them at start, and a running `scan --live` rereads them before every check, so a change
applies within one check. A change is validated against ScanConfig before it's written; a broken
file is ignored (the last good settings stay).

live_symbols and live_symbols_file aren't here: the live list has its own file (livelist.py).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from .config import ScanConfig

NOT_HERE = {"live_symbols", "live_symbols_file"}


class Overrides:
    def __init__(self, path: Path, base: ScanConfig):
        self.path = path
        self.base = base
        self._mtime: int | None = None
        self._scan = base
        self._data: dict = {}

    def _load(self) -> None:
        try:
            mtime = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            self._mtime, self._data, self._scan = None, {}, self.base
            return
        if mtime == self._mtime:
            return
        self._mtime = mtime
        try:
            data = json.loads(self.path.read_text())
            self._scan = self._merge(data.get("scan") or {})
            self._data = data
        except (OSError, ValueError, AttributeError):
            pass

    def _merge(self, changes: dict) -> ScanConfig:
        unknown = set(changes) - set(ScanConfig.model_fields) | (set(changes) & NOT_HERE)
        if unknown:
            raise ValueError(f"not scan settings you can change here: {', '.join(sorted(unknown))}")
        return ScanConfig.model_validate(self.base.model_dump() | changes)  # pydantic's ValidationError is a ValueError

    def scan(self) -> ScanConfig:
        """config.yaml's scan settings with the changes applied (the same object until the file changes)."""
        self._load()
        return self._scan

    def changes(self) -> dict:
        self._load()
        return dict(self._data.get("scan") or {})

    def info(self) -> dict:
        self._load()
        return {"changes": self.changes(), "updated": self._data.get("updated"), "by": self._data.get("by"),
                "path": str(self.path)}

    def update(self, changes: dict, by: str = "web") -> ScanConfig:
        """Add or change settings. A value equal to config.yaml's drops the override."""
        merged = self.changes() | changes
        scan = self._merge(merged)
        base = self.base.model_dump(mode="json")
        effective = scan.model_dump(mode="json")
        self._write({k: effective[k] for k in merged if effective[k] != base[k]}, by)
        return scan

    def reset(self, keys: list[str] | None = None, by: str = "web") -> ScanConfig:
        """Back to config.yaml, for the given settings or all of them."""
        if keys is None:
            self.path.unlink(missing_ok=True)
        else:
            self._write({k: v for k, v in self.changes().items() if k not in keys}, by)
        return self.scan()

    def _write(self, scan: dict, by: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"scan": scan, "updated": datetime.now(UTC).isoformat(timespec="seconds"),
                                   "by": by}, indent=2) + "\n")
        os.replace(tmp, self.path)
