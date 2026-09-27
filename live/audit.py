"""Append-only audit trail: one JSON line per decision, plus a CSV row per closed position.

logs/live/audit_<mode>_<YYYY-MM-DD>.jsonl records every signal, risk check
(passed and failed), order (ids, statuses, fills), exit, P&L, reconciliation,
alert and error, so every trade can be reconstructed afterwards. Records are
flushed immediately. logs/live/trades_<mode>.csv has one row per closed position.
"""
from __future__ import annotations

import csv
import json
import logging
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger("live")

TRADE_FIELDS = ["position_id", "strategy", "mode", "position_mode", "underlying", "expiry", "right", "strike",
                "side", "quantity", "entry_ts", "entry_spot", "entry_price", "stop", "hedge_strike",
                "hedge_entry_price", "net_credit", "max_loss", "margin", "exit_ts", "exit_spot", "exit_price",
                "hedge_exit_price", "exit_reason", "gross_pnl", "is_reentry", "order_ids"]


def _jsonable(v):
    if is_dataclass(v):
        return asdict(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, tuple):
        return list(v)
    return str(v)


class AuditLog:
    def __init__(self, directory: Path, mode: str, clock=datetime.now):
        self.dir = Path(directory)
        self.mode = mode
        self.clock = clock
        self.dir.mkdir(parents=True, exist_ok=True)
        self.records = 0

    def path_for(self, d: date) -> Path:
        return self.dir / f"audit_{self.mode.lower()}_{d:%Y-%m-%d}.jsonl"

    def write(self, event: str, market_ts: datetime | None = None, level: int = logging.INFO, **fields) -> dict:
        wall = self.clock()
        rec = {"wall_ts": wall.isoformat(timespec="seconds"), "market_ts": market_ts.isoformat() if market_ts else None,
               "mode": self.mode, "event": event, **fields}
        day = market_ts.date() if market_ts else wall.date()
        with open(self.path_for(day), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=_jsonable) + "\n")
        self.records += 1
        summary = ", ".join(f"{k}={v}" for k, v in fields.items()
                            if k in ("position_id", "contract", "reason", "ok", "message", "lots", "pnl", "detail"))
        log.log(level, "[%s] %s %s", self.mode, event, summary)
        return rec

    def trade(self, row: dict) -> None:
        path = self.dir / f"trades_{self.mode.lower()}.csv"
        new = not path.exists()
        with open(path, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=TRADE_FIELDS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow({k: (json.dumps(v, default=_jsonable) if isinstance(v, (dict, list)) else v)
                        for k, v in row.items()})
