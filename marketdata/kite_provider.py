"""Kite as the live engine's MarketDataProvider (live/interfaces.py), the drop-in for BreezeMarketData.

    bars    kite.historical_data(token, from, to, "minute") for today's completed 1-minute bars, fetched
            incrementally exactly like BreezeMarketData (only bars after the last one held, at most once
            every `min_refetch_s` per instrument, and only a bar stamped T once now >= T + 1 minute).
            Kite serves today's candles for the index and for live (unexpired) option contracts.
    prices  option_price(fresh=True) is the KiteStream tick (REST kite.ltp() when the stream can't vouch);
            fresh=False is the last completed bar's close, as before (backtest parity for mark-to-market).
    budget  none: api_budget_remaining() is None (Kite has rate limits, not a daily budget).

Contracts map to Kite instrument tokens through the day's instrument dump (zerodha.InstrumentBook).
Nothing is written to DuckDB. Expired contracts are NOT available from Kite: history for backtests and
the zero-DTE walk-forward still comes from DuckDB (filled by the Breeze downloaders).
"""
from __future__ import annotations

import logging
import threading
import time as _time
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from trading_data.calendar import TradingCalendar
from trading_data.storage import OptionContract

from .kite_stream import index_token

log = logging.getLogger("marketdata.kite_provider")
MINUTE = timedelta(minutes=1)
_IST = timezone(timedelta(hours=5, minutes=30))
COLS = ["open", "high", "low", "close"]


class KiteMarketDataProvider:
    OWNER = "live-engine"

    def __init__(self, kite_factory, stream, settings, underlying: str = "NIFTY", book_factory=None,
                 min_refetch_s: float = 5.0, historical_min_interval_s: float = 0.35,
                 monotonic=_time.monotonic, sleep=_time.sleep):
        self.kite_factory, self.stream = kite_factory, stream
        self.profile = settings.options_profile(underlying)
        self.instrument = settings.instrument(self.profile.underlying)
        self.cal = TradingCalendar(settings.calendars[self.profile.calendar])
        self.spot_token = index_token(self.profile.underlying)
        self.book_factory = book_factory or self._kite_book
        self.min_refetch_s, self.hist_interval = min_refetch_s, historical_min_interval_s
        self.monotonic, self.sleep = monotonic, sleep
        self._books: dict[date, object] = {}
        self._tokens: dict[OptionContract, int] = {}
        self._bars: dict[tuple, pd.DataFrame] = {}
        self._fetched_at: dict[tuple, float] = {}
        self._hist_lock = threading.Lock()
        self._hist_next = 0.0
        self.errors = 0
        self.last_error: str | None = None
        self.stream.acquire([self.spot_token], owner=self.OWNER)

    # -- MarketDataProvider ----------------------------------------------------------------------
    def spot_bars(self, day: date, now: datetime) -> pd.DataFrame:
        return self._bars_for(("SPOT", day), self.spot_token, day, now)

    def option_bars(self, contract: OptionContract, day: date, now: datetime) -> pd.DataFrame:
        tok = self._token(contract, now.date())
        if tok is None:
            return pd.DataFrame(columns=COLS)
        return self._bars_for(("OPT", contract, day), tok, day, now)

    def option_price(self, contract: OptionContract, now: datetime, fresh: bool = False) -> float | None:
        tok = self._token(contract, now.date())
        if fresh and tok is not None:
            self.stream.acquire([tok], owner=self.OWNER)
            try:
                px = self.stream.ltp(tok)
            except Exception as exc:
                self._error(f"ltp {contract.label}: {exc}")
                px = None
            if px and px > 0:
                return px
        bars = self.option_bars(contract, now.date(), now)
        if len(bars):
            return float(bars["close"].iloc[-1])
        if tok is not None:
            tick = self.stream.last(tok)
            return tick.price if tick else None
        return None

    def api_budget_remaining(self) -> int | None:
        return None

    # -- internals -----------------------------------------------------------------------------------
    def _kite_book(self, today: date):
        from zerodha.instruments import InstrumentBook
        return InstrumentBook.from_kite(self.kite_factory(), "data", self.profile.exchange, today)

    def _token(self, c: OptionContract, today: date) -> int | None:
        if c in self._tokens:
            return self._tokens[c]
        try:
            book = self._books.get(today)
            if book is None:
                book = self._books[today] = self.book_factory(today)
            tok = int(book.option(c.underlying, c.expiry, float(c.strike), c.right).instrument_token)
        except Exception as exc:
            self._error(f"instrument {c.label}: {exc}")
            return None
        self._tokens[c] = tok
        return tok

    def _historical(self, token: int, start: datetime, end: datetime) -> list[dict]:
        with self._hist_lock:                       # Kite historical: 3 requests/second
            wait = self._hist_next - self.monotonic()
            if wait > 0:
                self.sleep(wait)
            self._hist_next = self.monotonic() + self.hist_interval
        return self.kite_factory().historical_data(token, start, end, "minute")

    def _bars_for(self, key: tuple, token: int, day: date, now: datetime) -> pd.DataFrame:
        cache = self._bars.get(key)
        open_t, close_t = self.cal.session(day)
        latest_due = min(now.replace(second=0, microsecond=0) - MINUTE, datetime.combine(day, close_t))
        have = cache.index.max() if cache is not None and len(cache) else None
        stale = have is None or have < latest_due
        recently = self.monotonic() - self._fetched_at.get(key, -1e9) < self.min_refetch_s
        if stale and not recently:
            self._fetched_at[key] = self.monotonic()
            start = have + MINUTE if have is not None else datetime.combine(day, open_t)
            end = min(now, datetime.combine(day, close_t) + MINUTE)
            if start <= end:
                try:
                    df = candles_frame(self._historical(token, start, end), day, open_t, close_t)
                    df = df[df.index + MINUTE <= now]                      # completed bars only
                    cache = df if cache is None else pd.concat([cache, df[df.index > (have or datetime.min)]])
                    cache = cache[~cache.index.duplicated(keep="last")].sort_index()
                    self._bars[key] = cache
                except Exception as exc:
                    self._error(f"bars {key[0]} {token}: {type(exc).__name__}: {exc}")
        if cache is None:
            return pd.DataFrame(columns=COLS)
        return cache[cache.index + MINUTE <= now]

    def _error(self, msg: str) -> None:
        self.errors += 1
        self.last_error = msg
        log.warning("Kite data error: %s", msg)


def candles_frame(rows: list[dict], day: date, open_t, close_t) -> pd.DataFrame:
    """Kite historical rows -> naive-IST ts index, float OHLC, inside the session (close_t = last bar), sane (low <= o/c <= high)."""
    recs = []
    for r in rows or ():
        ts = r["date"]
        ts = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts))
        if ts.tzinfo is not None:
            ts = (ts.astimezone(_IST)).replace(tzinfo=None)
        o, h, lo, c = (float(r[k]) for k in ("open", "high", "low", "close"))
        if ts.date() != day or not (open_t <= ts.time() <= close_t) or min(o, h, lo, c) <= 0 or lo > min(o, c) \
                or h < max(o, c):
            continue
        recs.append((ts, o, h, lo, c))
    if not recs:
        return pd.DataFrame(columns=COLS, index=pd.DatetimeIndex([], name="ts"))
    df = pd.DataFrame(recs, columns=["ts", *COLS]).set_index("ts")
    return df[~df.index.duplicated(keep="last")].sort_index()

