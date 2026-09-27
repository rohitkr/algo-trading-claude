"""Breeze as the live MarketDataProvider (live/interfaces.py) for today's 1-minute bars and quotes.

Built on the existing BreezeClient, so the session token, the persisted daily API
budget, the per-minute throttle, retries and the candle validation
(trading_data.validation) are the same ones the historical downloaders use.

Polling, not streaming: each call asks historical_data_v2 only for bars after the
last completed bar it holds, and only once a new minute can have completed
(at most once every `min_refetch_s` per instrument). A bar stamped T is returned
only once now >= T + 1 minute, so strategies never see an unfinished candle.
Quotes (get_quotes) are cached for `quote_ttl_s` per contract.

Nothing is written to DuckDB: intraday partial days stay out of the research store.
"""
from __future__ import annotations

import time as _time
from datetime import date, datetime, timedelta

import pandas as pd

from ..calendar import TradingCalendar
from ..config import Settings
from ..log import get_logger
from ..storage import OptionContract
from ..validation import clean_candles, records_to_frame
from .client import BreezeClient, HistoricalRequest

log = get_logger("breeze.live")
MINUTE = timedelta(minutes=1)
COLS = ["open", "high", "low", "close"]


class BreezeMarketData:
    def __init__(self, client: BreezeClient, settings: Settings, underlying: str = "NIFTY",
                 min_refetch_s: float = 5.0, quote_ttl_s: float = 5.0, monotonic=_time.monotonic):
        self.client = client
        self.profile = settings.options_profile(underlying)
        self.instrument = settings.instrument(self.profile.underlying)
        self.cal = TradingCalendar(settings.calendars[self.profile.calendar])
        self.min_refetch_s, self.quote_ttl_s, self.monotonic = min_refetch_s, quote_ttl_s, monotonic
        self._bars: dict[tuple, pd.DataFrame] = {}
        self._fetched_at: dict[tuple, float] = {}
        self._quotes: dict[tuple, tuple[float, float]] = {}
        self.errors = 0
        self.last_error: str | None = None

    # -- MarketDataProvider ----------------------------------------------------------------
    def spot_bars(self, day: date, now: datetime) -> pd.DataFrame:
        req = HistoricalRequest(self.instrument.stock_code, self.instrument.exchange, self.instrument.product_type)
        return self._bars_for(("SPOT", day), req, day, now)

    def option_bars(self, contract: OptionContract, day: date, now: datetime) -> pd.DataFrame:
        return self._bars_for(("OPT", contract, day), self._option_req(contract), day, now)

    def option_price(self, contract: OptionContract, now: datetime, fresh: bool = False) -> float | None:
        if fresh:
            key = ("Q", contract)
            cached = self._quotes.get(key)
            if cached and self.monotonic() - cached[1] < self.quote_ttl_s:
                return cached[0]
            try:
                q = self.client.get_quote(self._option_req(contract))
                ltp = float(q["ltp"]) if q and q.get("ltp") not in (None, "") else None
            except Exception as exc:     # fall back to the last bar below
                self._error(f"quote {contract.label}: {exc}")
                ltp = None
            if ltp and ltp > 0:
                self._quotes[key] = (ltp, self.monotonic())
                return ltp
        bars = self.option_bars(contract, now.date(), now)
        if len(bars):
            return float(bars["close"].iloc[-1])
        cached = self._quotes.get(("Q", contract))
        return cached[0] if cached else None

    def api_budget_remaining(self) -> int | None:
        return self.client.budget.remaining()

    # -- internals -------------------------------------------------------------------------
    def _option_req(self, c: OptionContract) -> HistoricalRequest:
        return HistoricalRequest(self.profile.stock_code, self.profile.exchange, self.profile.product_type,
                                 expiry=c.expiry, right=c.right.lower(), strike=c.strike)

    def _bars_for(self, key: tuple, req: HistoricalRequest, day: date, now: datetime) -> pd.DataFrame:
        cache = self._bars.get(key)
        open_t, close_t = self.cal.session(day)
        latest_due = min(now.replace(second=0, microsecond=0) - MINUTE,       # newest bar that can be complete
                         datetime.combine(day, close_t))
        have = cache.index.max() if cache is not None and len(cache) else None
        stale = have is None or have < latest_due
        recently = self.monotonic() - self._fetched_at.get(key, -1e9) < self.min_refetch_s
        if stale and not recently:
            self._fetched_at[key] = self.monotonic()
            start = have + MINUTE if have is not None else datetime.combine(day, open_t)
            end = min(now, datetime.combine(day, close_t) + MINUTE)
            if start <= end:
                try:
                    rows = self.client.fetch_candles(req, start, end)
                    df, _ = clean_candles(records_to_frame(rows), self.cal, "1minute")
                    df = df.set_index("ts")[COLS].astype(float) if len(df) else pd.DataFrame(columns=COLS)
                    df = df[df.index + MINUTE <= now]                          # keep completed bars only
                    cache = df if cache is None else pd.concat([cache, df[df.index > (have or datetime.min)]])
                    cache = cache[~cache.index.duplicated(keep="last")].sort_index()
                    self._bars[key] = cache
                except Exception as exc:
                    self._error(f"bars {req.describe()}: {exc}")
        if cache is None:
            return pd.DataFrame(columns=COLS)
        return cache[cache.index + MINUTE <= now]

    def _error(self, msg: str) -> None:
        self.errors += 1
        self.last_error = msg
        log.warning("Breeze data error: %s", msg)
