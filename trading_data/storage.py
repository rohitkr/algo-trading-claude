"""DuckDB persistence: the only module that contains SQL.

Canonical tables
  market_candles        underlying / index candles (instrument, exchange, timeframe, ts)
  option_candles        option candles (underlying, exchange, expiry, strike, option_right, timeframe, ts)

Bookkeeping tables (resume + reporting; never the only source of truth)
  market_day_status     per instrument/day download attempts and outcome
  option_contracts      per contract download status
  option_expiry_plan    ATM / strike range chosen per expiry (the legacy "atm_cache")
  api_usage             Breeze calls per IST calendar day (shared by all scripts)

Synthetic data (opt-in, never mixed into canonical tables)
  option_candles_synthetic   legacy forward-filled bars, flagged as generated

Timestamps are exchange-local (IST) wall-clock times stored as naive TIMESTAMP.
All writes are upserts on the logical identity of a record, so re-running a
download never creates duplicates and a failed request never deletes data.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import duckdb
import pandas as pd

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (
    key   VARCHAR PRIMARY KEY,
    value VARCHAR
);

CREATE TABLE IF NOT EXISTS market_candles (
    instrument    VARCHAR   NOT NULL,
    exchange      VARCHAR   NOT NULL,
    timeframe     VARCHAR   NOT NULL,
    ts            TIMESTAMP NOT NULL,
    open          DOUBLE    NOT NULL,
    high          DOUBLE    NOT NULL,
    low           DOUBLE    NOT NULL,
    close         DOUBLE    NOT NULL,
    volume        BIGINT,
    open_interest BIGINT,
    source        VARCHAR   NOT NULL DEFAULT 'breeze',
    ingested_at   TIMESTAMP NOT NULL DEFAULT current_localtimestamp(),
    PRIMARY KEY (instrument, exchange, timeframe, ts)
);

CREATE TABLE IF NOT EXISTS option_candles (
    underlying    VARCHAR       NOT NULL,
    exchange      VARCHAR       NOT NULL,
    expiry        DATE          NOT NULL,
    strike        DECIMAL(12,2) NOT NULL,
    option_right  VARCHAR       NOT NULL CHECK (option_right IN ('CALL', 'PUT')),
    timeframe     VARCHAR       NOT NULL,
    ts            TIMESTAMP     NOT NULL,
    open          DOUBLE        NOT NULL,
    high          DOUBLE        NOT NULL,
    low           DOUBLE        NOT NULL,
    close         DOUBLE        NOT NULL,
    volume        BIGINT,
    open_interest BIGINT,
    source        VARCHAR       NOT NULL DEFAULT 'breeze',
    ingested_at   TIMESTAMP     NOT NULL DEFAULT current_localtimestamp(),
    PRIMARY KEY (underlying, exchange, expiry, strike, option_right, timeframe, ts)
);

CREATE TABLE IF NOT EXISTS option_candles_synthetic (
    underlying    VARCHAR       NOT NULL,
    exchange      VARCHAR       NOT NULL,
    expiry        DATE          NOT NULL,
    strike        DECIMAL(12,2) NOT NULL,
    option_right  VARCHAR       NOT NULL,
    timeframe     VARCHAR       NOT NULL,
    ts            TIMESTAMP     NOT NULL,
    open          DOUBLE        NOT NULL,
    high          DOUBLE        NOT NULL,
    low           DOUBLE        NOT NULL,
    close         DOUBLE        NOT NULL,
    volume        BIGINT,
    open_interest BIGINT,
    method        VARCHAR       NOT NULL DEFAULT 'forward_fill',
    created_at    TIMESTAMP     NOT NULL DEFAULT current_localtimestamp(),
    PRIMARY KEY (underlying, exchange, expiry, strike, option_right, timeframe, ts)
);

CREATE TABLE IF NOT EXISTS market_day_status (
    instrument  VARCHAR   NOT NULL,
    exchange    VARCHAR   NOT NULL,
    timeframe   VARCHAR   NOT NULL,
    trade_date  DATE      NOT NULL,
    status      VARCHAR   NOT NULL,   -- complete | incomplete | empty | invalid
    bar_count   INTEGER   NOT NULL,
    attempts    INTEGER   NOT NULL,
    detail      VARCHAR,
    updated_at  TIMESTAMP NOT NULL,
    PRIMARY KEY (instrument, exchange, timeframe, trade_date)
);

CREATE TABLE IF NOT EXISTS option_contracts (
    underlying    VARCHAR       NOT NULL,
    exchange      VARCHAR       NOT NULL,
    expiry        DATE          NOT NULL,
    strike        DECIMAL(12,2) NOT NULL,
    option_right  VARCHAR       NOT NULL,
    timeframe     VARCHAR       NOT NULL,
    status        VARCHAR       NOT NULL,   -- complete | no_data | failed
    row_count     INTEGER       NOT NULL,
    first_ts      TIMESTAMP,
    last_ts       TIMESTAMP,
    attempts      INTEGER       NOT NULL,
    used_retry    BOOLEAN       NOT NULL DEFAULT false,
    detail        VARCHAR,
    updated_at    TIMESTAMP     NOT NULL,
    PRIMARY KEY (underlying, exchange, expiry, strike, option_right, timeframe)
);

CREATE TABLE IF NOT EXISTS option_expiry_plan (
    underlying     VARCHAR NOT NULL,
    exchange       VARCHAR NOT NULL,
    expiry         DATE    NOT NULL,
    atm_date       DATE    NOT NULL,
    spot_open      DOUBLE  NOT NULL,
    atm            DECIMAL(12,2) NOT NULL,
    strike_low     DECIMAL(12,2) NOT NULL,
    strike_high    DECIMAL(12,2) NOT NULL,
    strike_step    DECIMAL(12,2) NOT NULL,
    planned_at     TIMESTAMP NOT NULL,
    PRIMARY KEY (underlying, exchange, expiry)
);

CREATE TABLE IF NOT EXISTS api_usage (
    usage_date DATE    PRIMARY KEY,
    calls      INTEGER NOT NULL
);

CREATE OR REPLACE VIEW option_candles_with_synthetic AS
    SELECT underlying, exchange, expiry, strike, option_right, timeframe, ts,
           open, high, low, close, volume, open_interest, false AS is_synthetic
    FROM option_candles
    UNION ALL
    SELECT s.underlying, s.exchange, s.expiry, s.strike, s.option_right, s.timeframe, s.ts,
           s.open, s.high, s.low, s.close, s.volume, s.open_interest, true AS is_synthetic
    FROM option_candles_synthetic s
    ANTI JOIN option_candles o USING (underlying, exchange, expiry, strike, option_right, timeframe, ts);
"""

MARKET_COLS = ["instrument", "exchange", "timeframe", "ts", "open", "high", "low", "close", "volume", "open_interest"]
OPTION_COLS = ["underlying", "exchange", "expiry", "strike", "option_right", "timeframe", "ts",
               "open", "high", "low", "close", "volume", "open_interest"]


@dataclass(frozen=True)
class OptionContract:
    """Logical identity of one option contract."""
    underlying: str
    exchange: str
    expiry: date
    strike: float
    right: str          # "CALL" | "PUT"

    def __post_init__(self):
        r = self.right.upper()
        r = {"CE": "CALL", "PE": "PUT", "C": "CALL", "P": "PUT"}.get(r, r)
        if r not in ("CALL", "PUT"):
            raise ValueError(f"Invalid option right {self.right!r}")
        object.__setattr__(self, "right", r)
        object.__setattr__(self, "underlying", self.underlying.upper())
        object.__setattr__(self, "exchange", self.exchange.upper())
        object.__setattr__(self, "strike", float(self.strike))

    @property
    def label(self) -> str:
        strike = int(self.strike) if float(self.strike).is_integer() else self.strike
        return f"{self.underlying} {self.expiry:%Y-%m-%d} {strike} {self.right}"


class CandleStore:
    """Thread-safe wrapper around one DuckDB connection."""

    def __init__(self, path: Path | str, read_only: bool = False):
        self.path = Path(path)
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.con = duckdb.connect(str(self.path), read_only=read_only)
        if not read_only:
            self.initialize()

    # -- lifecycle ------------------------------------------------------------
    def initialize(self) -> None:
        with self._lock:
            self.con.execute(SCHEMA)
            self.con.execute("INSERT OR REPLACE INTO schema_info VALUES ('version', ?)", [str(SCHEMA_VERSION)])

    def close(self) -> None:
        with self._lock:
            self.con.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def query_df(self, sql: str, params: list | None = None) -> pd.DataFrame:
        with self._lock:
            return self.con.execute(sql, params or []).df()

    def _upsert(self, table: str, df: pd.DataFrame, cols: list[str], key: list[str]) -> int:
        if df.empty:
            return 0
        frame = df[cols]
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in key)
        with self._lock:
            self.con.register("_incoming", frame)
            try:
                self.con.execute("BEGIN TRANSACTION")
                self.con.execute(
                    f"INSERT INTO {table} ({', '.join(cols)}) SELECT {', '.join(cols)} FROM _incoming "
                    f"ON CONFLICT ({', '.join(key)}) DO UPDATE SET {updates}, "
                    f"{'ingested_at' if table != 'option_candles_synthetic' else 'created_at'} = current_localtimestamp()"
                )
                self.con.execute("COMMIT")
            except Exception:
                self.con.execute("ROLLBACK")
                raise
            finally:
                self.con.unregister("_incoming")
        return len(frame)

    # -- market candles ------------------------------------------------------
    def upsert_market_candles(self, df: pd.DataFrame) -> int:
        """df columns: MARKET_COLS. Rows must already be validated."""
        return self._upsert("market_candles", df, MARKET_COLS, MARKET_COLS[:4])

    def market_bar_counts(self, instrument: str, exchange: str, timeframe: str,
                          start: date, end: date) -> dict[date, int]:
        """Bars stored per trading day, straight from the canonical table."""
        rows = self.query_df(
            "SELECT CAST(ts AS DATE) AS d, count(*) AS n FROM market_candles "
            "WHERE instrument = ? AND exchange = ? AND timeframe = ? AND ts >= ? AND ts < ? + INTERVAL 1 DAY "
            "GROUP BY 1",
            [instrument, exchange, timeframe, start, end],
        )
        return {r.d.date() if hasattr(r.d, "date") else r.d: int(r.n) for r in rows.itertuples()}

    def market_coverage(self, instrument: str, exchange: str, timeframe: str,
                        start: date | None = None, end: date | None = None) -> tuple[date | None, date | None]:
        """First and last day with stored candles (optionally within [start, end])."""
        sql = ("SELECT min(ts), max(ts) FROM market_candles WHERE instrument = ? AND exchange = ? AND timeframe = ?")
        params: list = [instrument, exchange, timeframe]
        if start is not None:
            sql += " AND ts >= ?"
            params.append(start)
        if end is not None:
            sql += " AND ts < ? + INTERVAL 1 DAY"
            params.append(end)
        with self._lock:
            lo, hi = self.con.execute(sql, params).fetchone()
        return (lo.date() if lo else None, hi.date() if hi else None)

    def market_day_summaries(self, instrument: str, exchange: str, timeframe: str,
                             start: date | None = None, end: date | None = None) -> pd.DataFrame:
        """One row per stored day: trade_date, candles, first_ts, last_ts."""
        sql = ("SELECT CAST(ts AS DATE) AS trade_date, count(*) AS candles, min(ts) AS first_ts, max(ts) AS last_ts "
               "FROM market_candles WHERE instrument = ? AND exchange = ? AND timeframe = ?")
        params: list = [instrument, exchange, timeframe]
        if start is not None:
            sql += " AND ts >= ?"
            params.append(start)
        if end is not None:
            sql += " AND ts < ? + INTERVAL 1 DAY"
            params.append(end)
        return self.query_df(sql + " GROUP BY 1 ORDER BY 1", params)

    def failed_days(self, instrument: str, exchange: str, timeframe: str) -> list[date]:
        df = self.query_df("SELECT trade_date FROM market_day_status WHERE instrument = ? AND exchange = ? "
                           "AND timeframe = ? AND status = 'failed' ORDER BY 1", [instrument, exchange, timeframe])
        return [_to_date(x) for x in df["trade_date"]]

    def get_market_candles(self, instrument: str, start, end, timeframe: str = "1minute",
                           exchange: str | None = None) -> pd.DataFrame:
        """Candles for [start, end] (dates are inclusive whole days)."""
        start_ts, end_ts = _range(start, end)
        sql = ("SELECT instrument, exchange, timeframe, ts, open, high, low, close, volume, open_interest "
               "FROM market_candles WHERE instrument = ? AND timeframe = ? AND ts >= ? AND ts <= ?")
        params = [instrument.upper(), timeframe, start_ts, end_ts]
        if exchange:
            sql += " AND exchange = ?"
            params.append(exchange.upper())
        return self.query_df(sql + " ORDER BY ts", params)

    # -- market day status ------------------------------------------------------
    def record_day_status(self, instrument: str, exchange: str, timeframe: str, trade_date: date,
                          status: str, bar_count: int, detail: str | None = None) -> None:
        with self._lock:
            self.con.execute(
                "INSERT INTO market_day_status VALUES (?, ?, ?, ?, ?, ?, 1, ?, current_localtimestamp()) "
                "ON CONFLICT (instrument, exchange, timeframe, trade_date) DO UPDATE SET "
                "status = excluded.status, bar_count = excluded.bar_count, "
                "attempts = market_day_status.attempts + 1, detail = excluded.detail, updated_at = excluded.updated_at",
                [instrument, exchange, timeframe, trade_date, status, bar_count, detail],
            )

    def day_attempts(self, instrument: str, exchange: str, timeframe: str) -> dict[date, tuple[str, int]]:
        rows = self.query_df(
            "SELECT trade_date, status, attempts FROM market_day_status "
            "WHERE instrument = ? AND exchange = ? AND timeframe = ?", [instrument, exchange, timeframe])
        return {_to_date(r.trade_date): (r.status, int(r.attempts)) for r in rows.itertuples()}

    # -- option candles ------------------------------------------------------
    def upsert_option_candles(self, df: pd.DataFrame) -> int:
        return self._upsert("option_candles", df, OPTION_COLS, OPTION_COLS[:7])

    def upsert_synthetic_option_candles(self, df: pd.DataFrame) -> int:
        return self._upsert("option_candles_synthetic", df, OPTION_COLS, OPTION_COLS[:7])

    def option_row_count(self, c: OptionContract, timeframe: str = "1minute") -> int:
        with self._lock:
            return self.con.execute(
                "SELECT count(*) FROM option_candles WHERE underlying = ? AND exchange = ? AND expiry = ? "
                "AND strike = ? AND option_right = ? AND timeframe = ?",
                [c.underlying, c.exchange, c.expiry, c.strike, c.right, timeframe]).fetchone()[0]

    def option_bar_counts(self, c: OptionContract, start: date, end: date,
                          timeframe: str = "1minute") -> dict[date, int]:
        df = self.query_df(
            "SELECT CAST(ts AS DATE) AS d, count(*) AS n FROM option_candles WHERE underlying = ? AND exchange = ? "
            "AND expiry = ? AND strike = ? AND option_right = ? AND timeframe = ? AND ts >= ? AND ts < ? + INTERVAL 1 DAY "
            "GROUP BY 1", [c.underlying, c.exchange, c.expiry, c.strike, c.right, timeframe, start, end])
        return {_to_date(r.d): int(r.n) for r in df.itertuples()}

    def get_option_candles(self, underlying: str, expiry, strike: float, right: str,
                           timeframe: str = "1minute", start=None, end=None,
                           exchange: str | None = None, include_synthetic: bool = False) -> pd.DataFrame:
        """Candles for one contract. Backtests should leave include_synthetic False."""
        c = OptionContract(underlying, exchange or "", _to_date(expiry), strike, right)
        table = "option_candles_with_synthetic" if include_synthetic else "option_candles"
        sql = (f"SELECT * FROM {table} WHERE underlying = ? AND expiry = ? AND strike = ? "
               "AND option_right = ? AND timeframe = ?")
        params = [c.underlying, c.expiry, c.strike, c.right, timeframe]
        if exchange:
            sql += " AND exchange = ?"
            params.append(c.exchange)
        if start is not None or end is not None:
            s, e = _range(start or date(1900, 1, 1), end or date(2999, 12, 31))
            sql += " AND ts >= ? AND ts <= ?"
            params += [s, e]
        df = self.query_df(sql + " ORDER BY ts", params)
        return df.drop(columns=[c for c in ("source", "ingested_at") if c in df.columns])

    def get_option_chain(self, underlying: str, expiry, ts: datetime, timeframe: str = "1minute") -> pd.DataFrame:
        """All strikes/rights of one expiry at one timestamp."""
        return self.query_df(
            "SELECT strike, option_right, open, high, low, close, volume, open_interest FROM option_candles "
            "WHERE underlying = ? AND expiry = ? AND timeframe = ? AND ts = ? ORDER BY strike, option_right",
            [underlying.upper(), _to_date(expiry), timeframe, ts])

    def list_expiries(self, underlying: str) -> list[date]:
        df = self.query_df("SELECT DISTINCT expiry FROM option_candles WHERE underlying = ? ORDER BY 1",
                           [underlying.upper()])
        return [_to_date(x) for x in df["expiry"]]

    # -- option bookkeeping ------------------------------------------------------
    def record_contract_status(self, c: OptionContract, timeframe: str, status: str, row_count: int,
                               first_ts=None, last_ts=None, used_retry: bool = False,
                               detail: str | None = None) -> None:
        with self._lock:
            self.con.execute(
                "INSERT INTO option_contracts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, current_localtimestamp()) "
                "ON CONFLICT (underlying, exchange, expiry, strike, option_right, timeframe) DO UPDATE SET "
                "status = excluded.status, row_count = excluded.row_count, first_ts = excluded.first_ts, "
                "last_ts = excluded.last_ts, attempts = option_contracts.attempts + 1, "
                "used_retry = excluded.used_retry, detail = excluded.detail, updated_at = excluded.updated_at",
                [c.underlying, c.exchange, c.expiry, c.strike, c.right, timeframe, status, row_count,
                 first_ts, last_ts, used_retry, detail])

    def contract_statuses(self, underlying: str, exchange: str, expiry: date,
                          timeframe: str = "1minute") -> dict[tuple[float, str], tuple[str, int]]:
        """(strike, right) -> (status, stored row count). Row count comes from option_candles."""
        df = self.query_df(
            "SELECT s.strike, s.option_right, s.status, coalesce(c.n, 0) AS n "
            "FROM option_contracts s LEFT JOIN ("
            "  SELECT strike, option_right, count(*) AS n FROM option_candles "
            "  WHERE underlying = ? AND exchange = ? AND expiry = ? AND timeframe = ? GROUP BY 1, 2"
            ") c USING (strike, option_right) "
            "WHERE s.underlying = ? AND s.exchange = ? AND s.expiry = ? AND s.timeframe = ?",
            [underlying, exchange, expiry, timeframe] * 2)
        return {(float(r.strike), r.option_right): (r.status, int(r.n)) for r in df.itertuples()}

    def save_expiry_plan(self, underlying: str, exchange: str, expiry: date, atm_date: date,
                         spot_open: float, atm: float, lo: float, hi: float, step: float) -> None:
        with self._lock:
            self.con.execute(
                "INSERT OR REPLACE INTO option_expiry_plan VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, current_localtimestamp())",
                [underlying, exchange, expiry, atm_date, spot_open, atm, lo, hi, step])

    def get_expiry_plan(self, underlying: str, exchange: str, expiry: date) -> dict | None:
        df = self.query_df("SELECT * FROM option_expiry_plan WHERE underlying = ? AND exchange = ? AND expiry = ?",
                           [underlying, exchange, expiry])
        return None if df.empty else df.iloc[0].to_dict()

    # -- API usage ------------------------------------------------------------
    def api_calls_on(self, d: date) -> int:
        with self._lock:
            row = self.con.execute("SELECT calls FROM api_usage WHERE usage_date = ?", [d]).fetchone()
        return int(row[0]) if row else 0

    def add_api_calls(self, d: date, n: int = 1) -> int:
        with self._lock:
            self.con.execute(
                "INSERT INTO api_usage VALUES (?, ?) ON CONFLICT (usage_date) DO UPDATE SET calls = api_usage.calls + excluded.calls",
                [d, n])
            return self.con.execute("SELECT calls FROM api_usage WHERE usage_date = ?", [d]).fetchone()[0]


def _to_date(x) -> date:
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, pd.Timestamp):
        return x.date()
    if isinstance(x, str):
        return date.fromisoformat(x)
    return x


def _range(start, end) -> tuple[datetime, datetime]:
    s = start if isinstance(start, datetime) else datetime.combine(_to_date(start), datetime.min.time())
    e = end if isinstance(end, datetime) else datetime.combine(_to_date(end), datetime.max.time())
    return s, e
