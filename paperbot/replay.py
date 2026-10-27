"""`paperbot replay [--date YYYY-MM-DD]`: one line per decision, order, fill and alert for a day,
from the journal. For reviewing what happened and why, after the fact."""

from __future__ import annotations

from .journal import Journal


def _hhmm(e: dict) -> str:
    return e["ts"][11:16]


def timeline(journal: Journal, day) -> list[str]:
    out = []
    for e in journal.events(day):
        ev = e["event"]
        if ev == "decision":
            props = "; ".join(f"{p['action']} {p['qty']} {p['symbol']} ({p['reason']})" for p in e.get("proposals", []))
            out.append(f"{_hhmm(e)}  AI: {e.get('market_view', '')} -> {props or 'hold'}")
        elif ev == "cycle_end" and (e.get("halt") or e.get("hold_reason")):
            if e.get("halt") != "market closed":
                out.append(f"{_hhmm(e)}  cycle ended: {e.get('halt') or e.get('hold_reason')}")
        elif ev == "order_submitted" and e.get("kind") != "stop":
            px = f" @ {e['limit_price']}" if e.get("limit_price") else " at market"
            out.append(f"{_hhmm(e)}  order: {e['kind']} {e['side']} {e['qty']} {e['symbol']} {e['type']}{px} ({e.get('reason')})")
        elif ev == "order_rejected" and e.get("kind") != "stop":
            out.append(f"{_hhmm(e)}  rejected: {e['kind']} {e['symbol']}: {e.get('why')}")
        elif ev == "fill":
            pnl = f", P&L {e['pnl']:+.2f}" if e.get("pnl") is not None else ""
            out.append(f"{_hhmm(e)}  fill: {e['side']} {e['qty']} {e['symbol']} @ {e['price']} ({e.get('kind')}){pnl}")
        elif ev in ("flatten", "runaway_protection", "broker_stops_disabled", "error", "phone_command", "signal"):
            detail = e.get("reason") or e.get("what") or e.get("error") or e.get("command") or e.get("message") or ""
            out.append(f"{_hhmm(e)}  {ev}: {detail}"[:200])
    return out
