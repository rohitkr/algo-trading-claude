"""Local storage of the Breeze session token (gitignored, owner-only file).

A Breeze session token is valid for the trading day it was generated on, so
the file stores the IST date alongside it and older tokens are treated as
expired. BREEZE_SESSION_TOKEN in .env takes precedence when set.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def today_ist() -> date:
    return datetime.now(IST).date()


def now_ist() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


@dataclass(frozen=True)
class StoredSession:
    token: str
    created_on: date
    source: str

    def is_current(self) -> bool:
        return self.created_on == today_ist()

    def __repr__(self) -> str:
        return f"StoredSession(created_on={self.created_on}, source={self.source}, token=<redacted>)"


def save_session(path: Path, token: str, source: str = "login") -> StoredSession:
    path.parent.mkdir(parents=True, exist_ok=True)
    s = StoredSession(token=token, created_on=today_ist(), source=source)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"session_token": token, "created_on": s.created_on.isoformat(), "source": source}))
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return s


def load_session(path: Path, environ: dict | None = None) -> StoredSession | None:
    env = os.environ if environ is None else environ
    if env.get("BREEZE_SESSION_TOKEN", "").strip():
        return StoredSession(env["BREEZE_SESSION_TOKEN"].strip(), today_ist(), "env")
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
        return StoredSession(raw["session_token"], date.fromisoformat(raw["created_on"]), raw.get("source", "file"))
    except (ValueError, KeyError):
        return None
