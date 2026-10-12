"""Every message read from the channel, exactly as received (text, time, edits, replies). Later phases add the
parsed meaning beside it; the raw text is never changed, so a better parser can always re-read history."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    channel_id  INTEGER NOT NULL,
    msg_id      INTEGER NOT NULL,
    date        TEXT NOT NULL,          -- when it was posted (UTC ISO)
    edit_date   TEXT,                   -- last edit (UTC ISO), if edited
    text        TEXT NOT NULL DEFAULT '',
    reply_to    INTEGER,                -- msg_id this one replies to (e.g. "SL hit" under a signal)
    has_media   INTEGER NOT NULL DEFAULT 0,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (channel_id, msg_id)
);
CREATE INDEX IF NOT EXISTS messages_date ON messages(date);
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL           -- JSON
);
-- what we did with each signal (one row per signal): ALERT / PROPOSED / PLACED / SKIPPED / FAILED / EXPIRED
CREATE TABLE IF NOT EXISTS actions (
    signal_id   INTEGER PRIMARY KEY,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    mode        TEXT NOT NULL,
    status      TEXT NOT NULL,
    plan        TEXT,                   -- JSON: the legs + config that would be / were placed
    strategy_id INTEGER,
    reason      TEXT,
    channel_exit INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS channels (
    channel_id  INTEGER PRIMARY KEY,
    title       TEXT NOT NULL,
    username    TEXT
);
"""


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class MessageStore:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def save_channel(self, channel_id: int, title: str, username: str | None) -> None:
        with self._lock, self.conn:
            self.conn.execute("INSERT INTO channels(channel_id, title, username) VALUES (?,?,?) "
                              "ON CONFLICT(channel_id) DO UPDATE SET title=excluded.title, username=excluded.username",
                              (channel_id, title, username))

    def upsert(self, channel_id: int, msg_id: int, date: datetime, text: str | None, *, edit_date: datetime | None = None,
               reply_to: int | None = None, has_media: bool = False) -> bool:
        """Insert or update (edits). Returns True when the row is new or its text/edit changed."""
        now = _iso(datetime.now(timezone.utc))
        with self._lock, self.conn:
            old = self.conn.execute("SELECT text, edit_date FROM messages WHERE channel_id=? AND msg_id=?",
                                    (channel_id, msg_id)).fetchone()
            row = (channel_id, msg_id, _iso(date), _iso(edit_date), text or "", reply_to, int(bool(has_media)), now)
            self.conn.execute(
                "INSERT INTO messages(channel_id, msg_id, date, edit_date, text, reply_to, has_media, fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(channel_id, msg_id) DO UPDATE SET "
                "edit_date=excluded.edit_date, text=excluded.text, reply_to=excluded.reply_to, "
                "has_media=excluded.has_media, fetched_at=excluded.fetched_at", row)
            return old is None or old["text"] != (text or "") or old["edit_date"] != _iso(edit_date)

    def recent(self, limit: int = 50, channel_id: int | None = None) -> list[dict]:
        sql = "SELECT * FROM messages" + (" WHERE channel_id=?" if channel_id else "") + " ORDER BY date DESC, msg_id DESC LIMIT ?"
        args = (channel_id, limit) if channel_id else (limit,)
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def count(self) -> int:
        with self._lock:
            return int(self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])

    # -- settings + actions (phase 3) --------------------------------------------------------------------
    def get_setting(self, key: str, default=None):
        import json
        with self._lock:
            r = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def set_setting(self, key: str, value) -> None:
        import json
        with self._lock, self.conn:
            self.conn.execute("INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                              (key, json.dumps(value)))

    def action(self, signal_id: int) -> dict | None:
        with self._lock:
            r = self.conn.execute("SELECT * FROM actions WHERE signal_id=?", (signal_id,)).fetchone()
        return _action(r)

    def actions(self, limit: int = 200) -> dict[int, dict]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM actions ORDER BY signal_id DESC LIMIT ?", (limit,)).fetchall()
        return {r["signal_id"]: _action(r) for r in rows}

    def save_action(self, signal_id: int, **fields) -> dict:
        """Insert the action for a signal, or update the given fields of the existing one."""
        import json
        now = _iso(datetime.now(timezone.utc))
        if "plan" in fields and not isinstance(fields["plan"], (str, type(None))):
            fields["plan"] = json.dumps(fields["plan"])
        with self._lock, self.conn:
            if self.conn.execute("SELECT 1 FROM actions WHERE signal_id=?", (signal_id,)).fetchone():
                if fields:
                    sets = ", ".join(f"{k}=?" for k in fields)
                    self.conn.execute(f"UPDATE actions SET {sets}, updated_at=? WHERE signal_id=?",
                                      (*fields.values(), now, signal_id))
            else:
                row = {"mode": "", "status": "", **fields, "signal_id": signal_id, "created_at": now, "updated_at": now}
                cols = ", ".join(row)
                self.conn.execute(f"INSERT INTO actions({cols}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
        return self.action(signal_id)

    def placed_on(self, day_prefix: str) -> int:
        """Signals we placed a trade for on this IST day (created_at is UTC; the caller passes a UTC window start)."""
        with self._lock:
            return int(self.conn.execute("SELECT COUNT(*) FROM actions WHERE status='PLACED' AND created_at >= ?",
                                         (day_prefix,)).fetchone()[0])


def _action(r) -> dict | None:
    import json
    if r is None:
        return None
    d = dict(r)
    d["plan"] = json.loads(d["plan"]) if d.get("plan") else None
    return d
