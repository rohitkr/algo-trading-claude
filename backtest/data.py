"""Market data access for backtests.

Reads spot and option candles through CandleStore. When a Breeze client is
supplied, option contracts that are not in DuckDB yet are fetched on demand
(only the days the backtest needs) and stored, so later runs are offline.
"""
from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from trading_data.calendar import TradingCalendar, generate_expiries
from trading_data.config import Settings
from trading_data.downloaders.options import OptionsDownloader
from trading_data.log import get_logger
from trading_data.storage import CandleStore, OptionContract

log = get_logger("backtest.data")


class DataFeed:
    def __init__(self, settings: Settings, store: CandleStore, underlying: str = "NIFTY", client=None):
        self.settings = settings
        self.store = store
        self.profile = settings.options_profile(underlying)
        self.instrument = settings.instrument(self.profile.underlying)
        self.cal = TradingCalendar(settings.calendars[self.profile.calendar])
        self.fetcher = OptionsDownloader(settings, store, client, self.profile) if client else None
        self._spot: dict[date, pd.DataFrame] = {}
        self._options: dict[tuple, pd.DataFrame] = {}
        self._tried: set[tuple] = set()
        self.api_rows = 0

    # -- spot ---------------------------------------------------------------------
    def load_spot(self, start: date, end: date) -> None:
        df = self.store.get_market_candles(self.instrument.name, start, end, "1minute", self.instrument.exchange)
        for d, day in df.groupby(df["ts"].dt.date):
            self._spot[d] = day.set_index("ts")[["open", "high", "low", "close"]]

    def spot_day(self, d: date) -> pd.DataFrame | None:
        if d not in self._spot:
            self.load_spot(d, d)
        return self._spot.get(d)

    def spot_path(self, start_day: date, end_day: date) -> pd.DataFrame:
        days = [self.spot_day(d) for d in self.cal.trading_days(start_day, end_day)]
        days = [d for d in days if d is not None and len(d)]
        return pd.concat(days) if days else pd.DataFrame(columns=["open", "high", "low", "close"])

    # -- expiries -------------------------------------------------------------------
    def expiries(self, start: date, end: date) -> list[date]:
        return generate_expiries(self.profile.expiry_rules, start, end, self.cal)

    def next_expiry(self, d: date, strictly_after: bool) -> date:
        for e in self.expiries(d, date.fromordinal(d.toordinal() + 21)):
            if e > d or (e == d and not strictly_after):
                return e
        raise ValueError(f"no expiry after {d}")

    # -- options --------------------------------------------------------------------
    def option(self, expiry: date, strike: float, right: str, days: list[date]) -> pd.DataFrame:
        """1-minute candles for one contract on `days` (indexed by ts). Empty frame if unavailable."""
        c = OptionContract(self.profile.underlying, self.profile.exchange, expiry, strike, right)
        key = (c, min(days), max(days))
        if key in self._options:
            return self._options[key]
        if self.fetcher is not None and key not in self._tried:
            self._tried.add(key)
            try:
                self.api_rows += self.fetcher.ensure_contract_days(c, days)
            except Exception as exc:  # a missing strike must not stop the whole backtest
                log.warning("could not fetch %s: %s", c.label, exc)
        df = self.store.get_option_candles(c.underlying, expiry, strike, c.right, start=min(days), end=max(days),
                                           exchange=c.exchange)
        df = df.set_index("ts")[["open", "high", "low", "close", "volume", "open_interest"]] if len(df) else \
            pd.DataFrame(columns=["open", "high", "low", "close", "volume", "open_interest"])
        self._options[key] = df
        return df


def price_at(series: pd.DataFrame, ts: datetime, field: str = "close") -> float | None:
    """Price at ts, or the last traded price before it (options can skip illiquid minutes)."""
    if series is None or series.empty:
        return None
    if ts in series.index:
        return float(series.at[ts, field])
    prior = series.loc[:ts]
    return float(prior["close"].iloc[-1]) if len(prior) else None
