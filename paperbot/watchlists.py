"""Named watchlists for the Charts tab, as many as you like (logs/watchlists.json). Separate from the scanner's live
symbols: a list's "Scan these" button makes it the scanner's list."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

TICKER = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
MAX_LISTS, MAX_SYMBOLS = 100, 500


class Watchlists:
    def __init__(self, path: Path, starter: list[str]):
        self.path = Path(path)
        self.starter = starter

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, dict) and isinstance(data.get("lists"), list):
                return data
        except (OSError, ValueError):
            pass
        return {"lists": [{"name": "Watchlist", "symbols": list(self.starter)}], "current": "Watchlist"}

    def by_name(self) -> dict[str, list[str]]:
        return {x["name"]: x["symbols"] for x in self.load()["lists"]}

    def save(self, data: dict) -> dict:
        lists = data.get("lists")
        if not isinstance(lists, list) or not 1 <= len(lists) <= MAX_LISTS:
            raise ValueError(f"keep 1 to {MAX_LISTS} watchlists")
        out, seen = [], set()
        for x in lists:
            name = str((x or {}).get("name") or "").strip()[:40]
            if not name or name in seen:
                raise ValueError("each watchlist needs its own name")
            seen.add(name)
            symbols = list(dict.fromkeys(str(s).strip().upper() for s in (x.get("symbols") or []) if str(s).strip()))
            bad = [s for s in symbols if not TICKER.match(s)]
            if bad:
                raise ValueError(f"not a ticker: {', '.join(bad[:5])}")
            if len(symbols) > MAX_SYMBOLS:
                raise ValueError(f"at most {MAX_SYMBOLS} symbols in a watchlist")
            out.append({"name": name, "symbols": symbols})
        current = data.get("current") if data.get("current") in seen else out[0]["name"]
        saved = {"lists": out, "current": current}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(saved, indent=1))
        os.replace(tmp, self.path)
        return saved
