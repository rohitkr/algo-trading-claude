"""Engine state on disk: open positions, strategy state, sent intents and daily counters.

Written atomically (tmp file + rename) after every poll, so a positional trade
carried overnight, its pending re-entry and the duplicate-order guard all
survive a crash or restart. One file per mode (BACKTEST replays never touch the
PAPER/LIVE state).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class EngineState:
    path: Path
    positions: dict = field(default_factory=dict)       # position_id -> position record (see engine.Position)
    strategies: dict = field(default_factory=dict)      # strategy name -> its JSON state
    sent_intents: list = field(default_factory=list)    # every intent id ever sent (duplicate guard)
    day: str | None = None
    trades_today: int = 0
    realized_today: float = 0.0
    halted: str | None = None                           # reason new entries are blocked
    session_status: str | None = None                   # MAX_LOSS_REACHED / MAX_PROFIT_REACHED for `day`
    pending_entries: dict = field(default_factory=dict)  # entry sent, result not recorded yet (crash window)
    unmanaged: dict = field(default_factory=dict)       # broker positions the engine will not touch
    blocked: dict = field(default_factory=dict)         # signal root -> reason it may never trade again
    counters: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "EngineState":
        p = Path(path)
        if not p.exists():
            return cls(p)
        raw = json.loads(p.read_text())
        return cls(p, **{k: v for k, v in raw.items() if k != "path"})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {k: v for k, v in self.__dict__.items() if k != "path"}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, default=str))
        os.replace(tmp, self.path)

    def was_sent(self, intent_id: str) -> bool:
        return intent_id in self.sent_intents

    def mark_sent(self, intent_id: str) -> None:
        if intent_id not in self.sent_intents:
            self.sent_intents.append(intent_id)
