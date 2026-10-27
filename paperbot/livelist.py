"""The live scan's symbols, changeable while it runs.

`paperbot scan --live` reads this list before every check, so a symbol added here is scanned within
one check, with no restart. It is kept in scan.live_symbols_file (in log_dir) as
{"symbols": [...], "updated": "...", "by": "..."}; until that file exists, scan.live_symbols from
config.yaml is the list. An empty list means everything you hold plus the watchlist.

Change it with `paperbot live-symbols add|remove|set|reset`, or from Python (an AI tool, a web page)
with LiveList.add(), remove(), replace() and reset(). Writes are atomic, so a scan never reads half
a file; when two writers race, the last one wins.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


def clean(symbols: list[str]) -> list[str]:
    """Upper-cased, de-duplicated tickers in order; ValueError on anything that isn't one."""
    out: list[str] = []
    for s in symbols:
        s = str(s).strip().upper()
        if not SYMBOL.match(s):
            raise ValueError(f"not a ticker symbol: {s!r}")
        if s not in out:
            out.append(s)
    return out


class LiveList:
    def __init__(self, path: Path, default: list[str]):
        self.path = path
        self.default = clean(default)
        self._mtime: int | None = None
        self._cached: list[str] = list(self.default)
        self._meta: dict = {}

    def symbols(self) -> list[str]:
        """The current list. The file is re-read only when it changed; a broken file keeps the last good list."""
        try:
            mtime = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            self._mtime, self._meta = None, {}
            return list(self.default)
        if mtime != self._mtime:
            self._mtime = mtime
            try:
                data = json.loads(self.path.read_text())
                self._cached = clean(data.get("symbols", []))
                self._meta = {k: data.get(k) for k in ("updated", "by")}
            except (OSError, ValueError, AttributeError):
                pass
        return list(self._cached)

    def info(self) -> dict:
        symbols = self.symbols()
        source = "file" if self.path.exists() else "config"
        return {"symbols": symbols, "source": source, "path": str(self.path), **self._meta}

    def replace(self, symbols: list[str], by: str = "cli") -> list[str]:
        syms = clean(symbols)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"symbols": syms, "updated": datetime.now(UTC).isoformat(timespec="seconds"),
                                   "by": by}, indent=2) + "\n")
        os.replace(tmp, self.path)
        return syms

    def add(self, symbols: list[str], by: str = "cli") -> list[str]:
        return self.replace(self.symbols() + clean(symbols), by)

    def remove(self, symbols: list[str], by: str = "cli") -> list[str]:
        gone = set(clean(symbols))
        return self.replace([s for s in self.symbols() if s not in gone], by)

    def reset(self) -> list[str]:
        """Back to scan.live_symbols from config.yaml."""
        self.path.unlink(missing_ok=True)
        return self.symbols()
