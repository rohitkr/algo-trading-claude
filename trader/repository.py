"""SQLite persistence: trades, every broker order, an append-only audit trail, confirm tokens, status.

Why SQLite and not the project's DuckDB (data/market_data.duckdb): DuckDB allows one writer process, and
that file is held open by the Breeze data pipeline and the live engine. This trader must write on every
order event while those run, so it keeps its own file (TRADER_DB, WAL mode, fsync'd commits). SQLite is
row-oriented and transactional (what order state needs); DuckDB stays the analytics store, and it can
read this file directly (`INSTALL sqlite; ATTACH 'data/trader/trades.sqlite' (TYPE sqlite)`) for reports.

Every write commits before the caller does anything at the broker, so a crash at any point leaves a
record of what was about to happen (see orders.OrderPlacer for the intent-before-send rule).
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from . import lifecycle as L

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS trades (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    mode                TEXT NOT NULL,              -- PAPER | LIVE
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    confirmed_at        TEXT,
    trade_date          TEXT NOT NULL,              -- IST date the trade was created
    underlying          TEXT NOT NULL,              -- NIFTY, BANKNIFTY, SENSEX, ...
    exchange            TEXT NOT NULL,              -- NFO | BFO
    tradingsymbol       TEXT NOT NULL,
    expiry              TEXT NOT NULL,
    strike              REAL NOT NULL,
    option_type         TEXT NOT NULL CHECK (option_type IN ('CE','PE')),
    side                TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    product             TEXT NOT NULL,              -- MIS | NRML
    lot_size            INTEGER NOT NULL,           -- from the Kite instrument dump
    tick_size           REAL NOT NULL,
    lots                INTEGER NOT NULL,
    quantity            INTEGER NOT NULL,           -- lots x lot_size (requested)
    entry_price         REAL NOT NULL,              -- requested limit
    entry_avg_price     REAL,                       -- actual (weighted average of fills)
    entry_time          TEXT,                       -- first fill seen
    filled_qty          INTEGER NOT NULL DEFAULT 0, -- entry quantity actually executed
    exited_qty          INTEGER NOT NULL DEFAULT 0, -- executed by our SL / partial / exit orders
    open_qty            INTEGER NOT NULL DEFAULT 0, -- filled - exited (unsigned; direction = side)
    initial_sl          REAL NOT NULL,
    current_sl          REAL NOT NULL,
    user_sl             REAL,                       -- SL last set by an edit (trailing is measured from it)
    target              REAL,
    trail_enabled       INTEGER NOT NULL DEFAULT 0,
    trail_type          TEXT,                       -- POINTS | PERCENT
    trail_value         REAL,
    trail_step          REAL,
    best_price          REAL,                       -- best LTP since entry (trailing reference)
    partial_enabled     INTEGER NOT NULL DEFAULT 0,
    partial_lots        INTEGER,
    partial_qty         INTEGER,
    partial_price       REAL,
    partial_done        INTEGER NOT NULL DEFAULT 0,
    auto_exit_at        TEXT,                       -- per-trade exit time (ISO)
    entry_order_id      TEXT,                       -- Zerodha order ids
    sl_order_id         TEXT,
    target_order_id     TEXT,                       -- unused: targets are monitored, see README
    exit_order_id       TEXT,
    broker_status       TEXT,                       -- last Zerodha status of the entry order
    status              TEXT NOT NULL,              -- lifecycle status (trader/lifecycle.py)
    position_status     TEXT NOT NULL DEFAULT 'NONE', -- NONE | OPEN | PARTIAL | CLOSED
    exit_reason         TEXT,
    pending_exit_reason TEXT,                       -- reason of the exit in progress
    exit_avg_price      REAL,
    exit_time           TEXT,
    realized_pnl        REAL NOT NULL DEFAULT 0,
    last_ltp            REAL,
    last_ltp_at         TEXT,
    unrealized_pnl      REAL,
    sl_software_only    INTEGER NOT NULL DEFAULT 0, -- no resting SL order: the monitor enforces the stop
    stop_breached_at    TEXT,                       -- first tick the LTP was through the SL (unfilled SL order)
    exit_attempts       INTEGER NOT NULL DEFAULT 0,
    outside_qty         INTEGER NOT NULL DEFAULT 0, -- quantity closed outside this system (adopted)
    outside_pnl         REAL NOT NULL DEFAULT 0,    -- its estimated P&L (at the last LTP)
    confirm_token       TEXT,
    confirm_expires_at  TEXT,
    mismatch_count      INTEGER NOT NULL DEFAULT 0,
    error               TEXT,
    reconcile_info      TEXT,                       -- JSON: last reconciliation finding
    notes               TEXT,
    group_id            INTEGER,                    -- future multi-leg: the strategy group this leg belongs to
    leg_role            TEXT                        -- future multi-leg: e.g. SHORT_CE / LONG_CE_WING
);
CREATE INDEX IF NOT EXISTS trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS trades_date ON trades(trade_date);

CREATE TABLE IF NOT EXISTS orders (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id            INTEGER NOT NULL REFERENCES trades(id),
    tag                 TEXT NOT NULL UNIQUE,       -- Kite order tag = our idempotency key (<= 20 chars)
    kind                TEXT NOT NULL CHECK (kind IN ('ENTRY','SL','TARGET','PARTIAL','EXIT')),
    purpose             TEXT,                       -- exit reason for EXIT/PARTIAL orders
    broker_order_id     TEXT UNIQUE,
    exchange            TEXT NOT NULL,
    tradingsymbol       TEXT NOT NULL,
    product             TEXT NOT NULL,
    side                TEXT NOT NULL,
    order_type          TEXT NOT NULL,              -- LIMIT | SL | MARKET
    quantity            INTEGER NOT NULL,
    price               REAL,
    trigger_price       REAL,
    status              TEXT NOT NULL,              -- INTENT | UNCERTAIN | NOT_PLACED | Kite status (OPEN, COMPLETE, ...)
    filled_qty          INTEGER NOT NULL DEFAULT 0,
    avg_price           REAL,
    status_message      TEXT,
    modifications       INTEGER NOT NULL DEFAULT 0,
    reprices            INTEGER NOT NULL DEFAULT 0,
    cancel_requested    INTEGER NOT NULL DEFAULT 0,
    missing_since       TEXT,                       -- first time an uncertain order was not in the order book
    order_date          TEXT NOT NULL,
    placed_at           TEXT,
    last_priced_at      TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS orders_trade ON orders(trade_id);
CREATE INDEX IF NOT EXISTS orders_status ON orders(status);

CREATE TABLE IF NOT EXISTS trade_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    trade_id    INTEGER,                            -- NULL = system event
    event       TEXT NOT NULL,
    level       TEXT NOT NULL DEFAULT 'INFO',
    from_status TEXT,
    to_status   TEXT,
    detail      TEXT                                -- JSON
);
CREATE INDEX IF NOT EXISTS events_trade ON trade_events(trade_id);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON trade_events
    BEGIN SELECT RAISE(ABORT, 'trade_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON trade_events
    BEGIN SELECT RAISE(ABORT, 'trade_events is append-only'); END;

CREATE TABLE IF NOT EXISTS action_tokens (
    token       TEXT PRIMARY KEY,
    trade_id    INTEGER NOT NULL,
    action      TEXT NOT NULL,                      -- CONFIRM | EXIT | CANCEL
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used_at     TEXT,
    payload     TEXT                                -- JSON bound to the token (e.g. the exact edit previewed)
);

CREATE TABLE IF NOT EXISTS api_usage (day TEXT PRIMARY KEY, calls INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS system_status (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT NOT NULL);
"""

# Columns added after the first release: ALTERed into existing databases at start (additive only).
ADDED_COLUMNS = {"trades": [("user_sl", "REAL"), ("group_id", "INTEGER"), ("leg_role", "TEXT")],
                 "action_tokens": [("payload", "TEXT")]}

TRADE_COLUMNS: set[str] = set()     # filled at first connect (PRAGMA table_info)
ORDER_COLUMNS: set[str] = set()


def _iso(v) -> str | None:
    if v is None:
        return None
    return v.isoformat(timespec="seconds") if isinstance(v, datetime) else (v.isoformat() if isinstance(v, date) else str(v))


class Repository:
    def __init__(self, path: str | Path, clock):
        self.path = Path(path)
        self.clock = clock
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        global TRADE_COLUMNS, ORDER_COLUMNS
        TRADE_COLUMNS = {r["name"] for r in self.conn.execute("PRAGMA table_info(trades)")}
        ORDER_COLUMNS = {r["name"] for r in self.conn.execute("PRAGMA table_info(orders)")}
        with self.tx():
            self.conn.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            # Random per-database prefix for Kite tags, so a fresh database never reuses an old tag.
            self.conn.execute("INSERT OR IGNORE INTO meta VALUES ('tag_salt', ?)", (secrets.token_hex(2),))
        self.tag_salt = self.meta("tag_salt")

    def _migrate(self) -> None:
        for table, cols in ADDED_COLUMNS.items():
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, typ in cols:
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self):
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    def now(self) -> str:
        return _iso(self.clock())

    def meta(self, key: str) -> str | None:
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    # -- trades ----------------------------------------------------------------------------------
    def insert_trade(self, fields: dict) -> int:
        now = self.now()
        row = {"created_at": now, "updated_at": now, **fields}
        bad = set(row) - TRADE_COLUMNS
        if bad:
            raise KeyError(f"unknown trade columns {bad}")
        cols = ", ".join(row)
        with self.tx() as c:
            cur = c.execute(f"INSERT INTO trades ({cols}) VALUES ({', '.join('?' * len(row))})",
                            [_iso(v) if isinstance(v, (date, datetime)) else v for v in row.values()])
            return int(cur.lastrowid)

    def trade(self, trade_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        return dict(r) if r else None

    def update_trade(self, trade_id: int, expect_status: str | Iterable[str] | None = None, **fields) -> bool:
        """Update columns; with expect_status the update only happens if the status still matches
        (compare-and-set, so two actions can never both move a trade). Returns whether a row changed."""
        bad = set(fields) - TRADE_COLUMNS
        if bad:
            raise KeyError(f"unknown trade columns {bad}")
        fields = {**fields, "updated_at": self.now()}
        sets = ", ".join(f"{k}=?" for k in fields)
        args = [_iso(v) if isinstance(v, (date, datetime)) else v for v in fields.values()]
        sql = f"UPDATE trades SET {sets} WHERE id=?"
        args.append(trade_id)
        if expect_status is not None:
            st = [expect_status] if isinstance(expect_status, str) else list(expect_status)
            sql += f" AND status IN ({', '.join('?' * len(st))})"
            args += st
        with self.tx() as c:
            return c.execute(sql, args).rowcount == 1

    def set_status(self, trade_id: int, new: str, event: str, *, expect: str | Iterable[str] | None = None,
                   level: str = "INFO", detail: dict | None = None, **fields) -> bool:
        """Lifecycle transition + its audit event in ONE transaction (the state machine is enforced here)."""
        with self.tx() as c:
            t = c.execute("SELECT status FROM trades WHERE id=?", (trade_id,)).fetchone()
            if t is None:
                raise KeyError(f"no trade {trade_id}")
            old = t["status"]
            if expect is not None and old not in ([expect] if isinstance(expect, str) else list(expect)):
                return False
            L.check_transition(old, new)
            fields = {**fields, "status": new, "updated_at": self.now()}
            bad = set(fields) - TRADE_COLUMNS
            if bad:
                raise KeyError(f"unknown trade columns {bad}")
            c.execute(f"UPDATE trades SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                      [_iso(v) if isinstance(v, (date, datetime)) else v for v in fields.values()] + [trade_id])
            self._event(c, trade_id, event, level, old, new, detail)
            return True

    def trades(self, statuses: Iterable[str] | None = None, trade_date: date | None = None,
               limit: int | None = None) -> list[dict]:
        sql, args = "SELECT * FROM trades WHERE 1=1", []
        if statuses is not None:
            st = list(statuses)
            sql += f" AND status IN ({', '.join('?' * len(st))})"
            args += st
        if trade_date is not None:
            sql += " AND trade_date=?"
            args.append(trade_date.isoformat())
        sql += " ORDER BY id DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [dict(r) for r in self.conn.execute(sql, args)]

    def closed_on(self, day: date) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM trades WHERE status IN ('EXITED','MANUALLY_EXITED') AND substr(exit_time,1,10)=?",
            (day.isoformat(),))]

    def confirmed_on(self, day: date) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM trades WHERE substr(confirmed_at,1,10)=?",
                                     (day.isoformat(),)).fetchone()[0])

    # -- orders ----------------------------------------------------------------------------------
    def new_tag(self, trade_id: int, kind: str) -> str:
        """Unique Kite tag (<= 20 chars, alphanumeric): 'mt' + db salt + trade id + kind letter + sequence."""
        n = int(self.conn.execute("SELECT COUNT(*) FROM orders WHERE trade_id=?", (trade_id,)).fetchone()[0]) + 1
        return f"mt{self.tag_salt}{trade_id}{kind[0]}{n}"[:20]

    def insert_order(self, fields: dict) -> dict:
        now = self.now()
        row = {"created_at": now, "updated_at": now, "order_date": now[:10], **fields}
        bad = set(row) - ORDER_COLUMNS
        if bad:
            raise KeyError(f"unknown order columns {bad}")
        with self.tx() as c:
            cur = c.execute(f"INSERT INTO orders ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                            list(row.values()))
            oid = int(cur.lastrowid)
        return self.order(oid)

    def order(self, oid: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        return dict(r) if r else None

    def order_by_tag(self, tag: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM orders WHERE tag=?", (tag,)).fetchone()
        return dict(r) if r else None

    def update_order(self, oid: int, **fields) -> None:
        bad = set(fields) - ORDER_COLUMNS
        if bad:
            raise KeyError(f"unknown order columns {bad}")
        fields = {**fields, "updated_at": self.now()}
        with self.tx() as c:
            c.execute(f"UPDATE orders SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                      list(fields.values()) + [oid])

    def orders(self, trade_id: int | None = None, kind: str | None = None,
               statuses: Iterable[str] | None = None) -> list[dict]:
        sql, args = "SELECT * FROM orders WHERE 1=1", []
        if trade_id is not None:
            sql += " AND trade_id=?"
            args.append(trade_id)
        if kind is not None:
            sql += " AND kind=?"
            args.append(kind)
        if statuses is not None:
            st = list(statuses)
            sql += f" AND status IN ({', '.join('?' * len(st))})"
            args += st
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY id", args)]

    # -- events ----------------------------------------------------------------------------------
    def _event(self, c, trade_id, event, level, old, new, detail) -> None:
        c.execute("INSERT INTO trade_events (ts, trade_id, event, level, from_status, to_status, detail) "
                  "VALUES (?,?,?,?,?,?,?)",
                  (self.now(), trade_id, event, level, old, new,
                   json.dumps(detail, default=str) if detail is not None else None))

    def event(self, trade_id: int | None, event: str, level: str = "INFO", detail: dict | None = None) -> None:
        with self.tx() as c:
            self._event(c, trade_id, event, level, None, None, detail)

    def events(self, trade_id: int | None = None, limit: int = 200, system_only: bool = False) -> list[dict]:
        if trade_id is not None:
            rows = self.conn.execute("SELECT * FROM trade_events WHERE trade_id=? ORDER BY id DESC LIMIT ?",
                                     (trade_id, limit))
        elif system_only:
            rows = self.conn.execute("SELECT * FROM trade_events WHERE trade_id IS NULL ORDER BY id DESC LIMIT ?",
                                     (limit,))
        else:
            rows = self.conn.execute("SELECT * FROM trade_events ORDER BY id DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d["detail"]) if d["detail"] else None
            out.append(d)
        return out

    # -- action tokens (server-side confirmation) ------------------------------------------------
    def issue_token(self, trade_id: int, action: str, expires_at: datetime, payload: dict | None = None) -> str:
        tok = secrets.token_urlsafe(24)
        with self.tx() as c:
            c.execute("INSERT INTO action_tokens (token, trade_id, action, created_at, expires_at, used_at, payload) "
                      "VALUES (?,?,?,?,?,NULL,?)",
                      (tok, trade_id, action, self.now(), _iso(expires_at),
                       json.dumps(payload, default=str) if payload is not None else None))
        return tok

    def consume_token(self, token: str, trade_id: int, action: str) -> dict | None:
        """Single use: the token row (with its decoded payload) exactly once for a valid, unexpired token of
        this trade and action; None otherwise."""
        with self.tx() as c:
            ok = c.execute("UPDATE action_tokens SET used_at=? WHERE token=? AND trade_id=? AND action=? "
                           "AND used_at IS NULL AND expires_at>=?",
                           (self.now(), token, trade_id, action, self.now())).rowcount == 1
            if not ok:
                return None
            r = dict(c.execute("SELECT * FROM action_tokens WHERE token=?", (token,)).fetchone())
        r["payload"] = json.loads(r["payload"]) if r["payload"] else None
        return r

    # -- Breeze API usage (BreezeClient's ApiBudget store interface) -----------------------------
    def api_calls_on(self, day: date) -> int:
        r = self.conn.execute("SELECT calls FROM api_usage WHERE day=?", (day.isoformat(),)).fetchone()
        return int(r["calls"]) if r else 0

    def add_api_calls(self, day: date, n: int) -> int:
        with self.tx() as c:
            c.execute("INSERT INTO api_usage VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET calls=calls+excluded.calls",
                      (day.isoformat(), n))
        return self.api_calls_on(day)

    # -- system status ---------------------------------------------------------------------------
    def set_status_value(self, key: str, value) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO system_status VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET "
                      "value=excluded.value, updated_at=excluded.updated_at",
                      (key, json.dumps(value, default=str), self.now()))

    def status_values(self) -> dict:
        return {r["key"]: {"value": json.loads(r["value"]) if r["value"] else None, "updated_at": r["updated_at"]}
                for r in self.conn.execute("SELECT * FROM system_status")}
