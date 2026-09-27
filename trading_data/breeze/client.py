"""The single integration point with the Breeze SDK.

Responsibilities
  * build BreezeConnect and open the session from the stored token
  * count every API call against the daily limit (persisted in DuckDB)
  * throttle (per-call delay + calls-per-minute ceiling) across threads
  * retry transient failures with exponential backoff
  * page historical_data_v2 (max 1000 rows, newest first) until a window is covered

Downloaders call `fetch_candles()` and never touch BreezeConnect directly.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..config import ApiSettings, Settings
from ..log import get_logger, register_secret
from ..storage import CandleStore
from .session_store import load_session, today_ist

log = get_logger("breeze")


class BreezeError(RuntimeError):
    pass


class SessionExpiredError(BreezeError):
    pass


class ApiLimitReached(BreezeError):
    pass


def fmt_ts(ts: datetime) -> str:
    """Breeze expects ISO strings with a Z suffix but interprets them as IST wall-clock time."""
    return ts.strftime("%Y-%m-%dT%H:%M:%S.000Z")


@dataclass(frozen=True)
class HistoricalRequest:
    stock_code: str
    exchange: str
    product_type: str
    interval: str = "1minute"
    expiry: date | None = None
    right: str | None = None          # "call" | "put"
    strike: float | None = None

    def params(self) -> dict:
        p = dict(interval=self.interval, stock_code=self.stock_code,
                 exchange_code=self.exchange, product_type=self.product_type)
        if self.expiry is not None:
            p["expiry_date"] = f"{self.expiry:%Y-%m-%d}T07:00:00.000Z"
        if self.right is not None:
            p["right"] = self.right.lower()
        if self.strike is not None:
            p["strike_price"] = str(int(self.strike)) if float(self.strike).is_integer() else str(self.strike)
        return p

    def describe(self) -> str:
        s = f"{self.stock_code}/{self.exchange}/{self.interval}"
        if self.expiry:
            s += f" exp={self.expiry} {self.strike} {self.right}"
        return s


class ApiBudget:
    """Daily call counter persisted in DuckDB, plus a sliding per-minute window."""

    def __init__(self, store: CandleStore, daily_limit: int, per_minute: int):
        self.store = store
        self.daily_limit = daily_limit
        self.per_minute = per_minute
        self._lock = threading.Lock()
        self._window: deque[float] = deque()
        self.used_this_run = 0

    def used_today(self) -> int:
        return self.store.api_calls_on(today_ist())

    def remaining(self) -> int:
        return max(0, self.daily_limit - self.used_today())

    def acquire(self) -> int:
        """Reserve one call. Raises ApiLimitReached when the daily limit is hit."""
        with self._lock:
            used = self.store.api_calls_on(today_ist())
            if used >= self.daily_limit:
                raise ApiLimitReached(f"Daily API limit reached ({used}/{self.daily_limit})")
            while True:
                now = time.monotonic()
                while self._window and now - self._window[0] > 60:
                    self._window.popleft()
                if len(self._window) < self.per_minute:
                    break
                time.sleep(max(0.05, 60 - (now - self._window[0])))
            self._window.append(time.monotonic())
            self.used_this_run += 1
            return self.store.add_api_calls(today_ist(), 1)


class BreezeClient:
    def __init__(self, settings: Settings, store: CandleStore, daily_limit: int | None = None,
                 delay_seconds: float | None = None, breeze=None):
        self.settings = settings
        self.api: ApiSettings = settings.api
        self.delay = self.api.delay_seconds if delay_seconds is None else delay_seconds
        limit = min(self.api.daily_limit, daily_limit or self.api.daily_limit)
        self.budget = ApiBudget(store, limit, self.api.max_calls_per_minute)
        self._breeze = breeze   # injectable for tests

    # -- session --------------------------------------------------------------
    def connect(self) -> "BreezeClient":
        if self._breeze is not None:
            return self
        creds = self.settings.credentials
        creds.require_api()
        register_secret(creds.api_secret)
        session = load_session(self.settings.paths.session_file)
        if session is None:
            raise SessionExpiredError("No session token found. Run: python3 scripts/get_session_token.py")
        if not session.is_current():
            raise SessionExpiredError(
                f"Stored session token is from {session.created_on}; Breeze tokens last one day. "
                "Run: python3 scripts/get_session_token.py")
        register_secret(session.token)

        from breeze_connect import BreezeConnect  # imported lazily so unit tests never need the SDK
        breeze = BreezeConnect(api_key=creds.api_key)
        self.budget.acquire()  # generate_session makes one customer-details call
        try:
            breeze.generate_session(api_secret=creds.api_secret, session_token=session.token)
        except Exception as exc:  # SDK raises bare Exception with a message
            msg = str(exc)
            if any(k in msg.lower() for k in ("session", "expired", "resource not available")):
                raise SessionExpiredError(
                    "Breeze rejected the session token (expired or invalid). "
                    "Run: python3 scripts/get_session_token.py") from None
            raise BreezeError(f"Could not open Breeze session: {msg}") from None
        self._breeze = breeze
        log.info("Breeze session opened (API calls used today: %d/%d)",
                 self.budget.used_today(), self.budget.daily_limit)
        return self

    # -- raw call ---------------------------------------------------------------
    def _call_historical(self, req: HistoricalRequest, start: datetime, end: datetime) -> list[dict]:
        """One logical request with retries. Every attempt counts against the budget."""
        attempt = 0
        while True:
            attempt += 1
            used = self.budget.acquire()
            try:
                resp = self._breeze.get_historical_data_v2(
                    from_date=fmt_ts(start), to_date=fmt_ts(end), **req.params())
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
                resp = None
            else:
                err = None
            finally:
                if self.delay:
                    time.sleep(self.delay)

            if resp is not None:
                status = resp.get("Status")
                success = resp.get("Success")
                error = resp.get("Error")
                if status == 200 and isinstance(success, list):
                    log.debug("API %s %s..%s -> %d rows (calls today %d)",
                              req.describe(), start, end, len(success), used)
                    return success
                if status == 200 and success in (None, "") and not error:
                    return []
                err = f"status={status} error={error}"
                if error and any(k in str(error).lower() for k in ("session", "unauthor", "invalid user")):
                    raise SessionExpiredError(f"Breeze session rejected: {error}")
                if status and 400 <= int(status) < 500 and "limit" not in str(error).lower():
                    raise BreezeError(f"{req.describe()}: {err}")

            if attempt > self.api.max_retries:
                raise BreezeError(f"{req.describe()} {start}..{end} failed after {attempt} attempts: {err}")
            wait = self.api.retry_backoff_seconds * (2 ** (attempt - 1))
            log.warning("Retry %d/%d for %s %s..%s in %.1fs (%s)",
                        attempt, self.api.max_retries, req.describe(), start.date(), end.date(), wait, err)
            time.sleep(wait)

    # -- quotes (live trading) ------------------------------------------------------
    def get_quote(self, req: HistoricalRequest) -> dict | None:
        """Latest quote (ltp, ltt, bid/ask...) for one instrument via get_quotes; None when Breeze has none.

        Uses the same budget, throttle and retry rules as historical calls. For
        options Breeze expects the expiry as ...T06:00:00.000Z.
        """
        if self._breeze is None:
            raise BreezeError("BreezeClient.connect() has not been called")
        params = {"stock_code": req.stock_code, "exchange_code": req.exchange, "product_type": req.product_type,
                  "expiry_date": f"{req.expiry:%Y-%m-%d}T06:00:00.000Z" if req.expiry else "",
                  "right": (req.right or "").lower(), "strike_price": req.params().get("strike_price", "")}
        attempt = 0
        while True:
            attempt += 1
            self.budget.acquire()
            try:
                resp = self._breeze.get_quotes(**params)
                err = None
            except Exception as exc:
                resp, err = None, f"{type(exc).__name__}: {exc}"
            if resp is not None:
                success, error = resp.get("Success"), resp.get("Error")
                if resp.get("Status") == 200 and isinstance(success, list):
                    rows = [r for r in success if str(r.get("exchange_code", req.exchange)).upper() == req.exchange]
                    return (rows or success or [None])[0]
                if resp.get("Status") == 200 and not error:
                    return None
                err = f"status={resp.get('Status')} error={error}"
                if error and any(k in str(error).lower() for k in ("session", "unauthor", "invalid user")):
                    raise SessionExpiredError(f"Breeze session rejected: {error}")
            if attempt > self.api.max_retries:
                raise BreezeError(f"quote {req.describe()} failed after {attempt} attempts: {err}")
            time.sleep(self.api.retry_backoff_seconds * (2 ** (attempt - 1)))

    # -- paginated fetch -----------------------------------------------------------
    def fetch_candles(self, req: HistoricalRequest, start: datetime, end: datetime) -> list[dict]:
        """All candles in [start, end].

        historical_data_v2 returns at most `max_rows_per_request` rows and gives
        the most recent ones (verified live on 2026-09-27). When a page is full,
        move the window end to just before the earliest row returned and ask again.
        """
        if self._breeze is None:
            raise BreezeError("BreezeClient.connect() has not been called")
        cap = self.api.max_rows_per_request
        rows: list[dict] = []
        window_end = end
        step = timedelta(seconds=1)
        while True:
            page = self._call_historical(req, start, window_end)
            rows.extend(page)
            if len(page) < cap:
                break
            stamps = [t for t in (_parse(r.get("datetime")) for r in page) if t is not None]
            earliest = min(stamps) if stamps else None
            if earliest is None or earliest <= start or earliest >= window_end:
                break
            window_end = earliest - step
        # Pages arrive newest-first; hand back one chronological list.
        rows.sort(key=lambda r: str(r.get("datetime") or ""))
        return rows


def _parse(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
