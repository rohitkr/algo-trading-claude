"""Tradingsymbol, lot size and tick size from Kite's daily instrument dump (NFO and BFO), never hardcoded.

zerodha.instruments.InstrumentBook.from_kite caches each exchange's dump per day in
data/kite_instruments_<EXCH>_<date>.csv; this service reloads when the date changes. SENSEX/BANKEX
options live on BFO, everything else on NFO. The dump is public, so PAPER mode loads it without a Kite
login; if Kite cannot be reached, the newest cached CSV is used and flagged as stale.
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Callable

from zerodha.instruments import Instrument, InstrumentBook

from .config import exchange_for

log = logging.getLogger("trader.instruments")
OPT = {"CE": "CALL", "PE": "PUT"}


def kite_loader(kite_factory: Callable[[], object], cache_dir: str | Path = "data"):
    """Loader for InstrumentService: today's dump via Kite (cached), else the newest cached CSV."""
    def load(exchange: str, today: date) -> tuple[InstrumentBook, str]:
        try:
            return InstrumentBook.from_kite(kite_factory(), cache_dir, exchange, today), "kite"
        except Exception as exc:
            files = sorted(Path(cache_dir).glob(f"kite_instruments_{exchange}_*.csv"))
            if not files:
                raise RuntimeError(f"cannot load the {exchange} instrument list from Kite ({exc}) "
                                   f"and no cached copy exists in {cache_dir}") from exc
            log.warning("Kite instruments %s unavailable (%s); using stale cache %s", exchange, exc, files[-1].name)
            return InstrumentBook.from_csv(files[-1]), f"stale cache {files[-1].name}"
    return load


class InstrumentService:
    def __init__(self, loader: Callable[[str, date], tuple[InstrumentBook, str]], underlyings, clock):
        self.loader, self.underlyings, self.clock = loader, tuple(underlyings), clock
        self._books: dict[str, tuple[date, InstrumentBook, str]] = {}

    def book(self, exchange: str) -> InstrumentBook:
        today = self.clock().date()
        cached = self._books.get(exchange)
        if cached is None or cached[0] != today:
            book, source = self.loader(exchange, today)
            self._books[exchange] = (today, book, source)
            log.info("instrument list %s: %d options (%s)", exchange, len(book), source)
        return self._books[exchange][1]

    def source(self, exchange: str) -> str | None:
        c = self._books.get(exchange)
        return c[2] if c else None

    def expiries(self, underlying: str) -> list[date]:
        today = self.clock().date()
        return [e for e in self.book(exchange_for(underlying)).expiries(underlying.upper()) if e >= today]

    def strikes(self, underlying: str, expiry: date) -> list[float]:
        return self.book(exchange_for(underlying)).strikes(underlying.upper(), expiry)

    def resolve(self, underlying: str, expiry: date, strike: float, option_type: str) -> Instrument:
        if option_type not in OPT:
            raise ValueError(f"option type must be CE or PE, not {option_type!r}")
        return self.book(exchange_for(underlying)).option(underlying.upper(), expiry, float(strike), OPT[option_type])

    def by_symbol(self, exchange: str, tradingsymbol: str) -> Instrument:
        return self.book(exchange).by_symbol(tradingsymbol)
