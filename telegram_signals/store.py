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
