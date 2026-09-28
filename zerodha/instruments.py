"""NFO option instrument lookup (tradingsymbol, lot size, tick size) from Kite's instrument dump.

Kite publishes the full instrument list once a day (kite.instruments("NFO"),
~100k rows). It is cached to a CSV per day so repeated runs are instant.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

KITE_TYPE = {"CALL": "CE", "PUT": "PE"}
RIGHT = {"CE": "CALL", "PE": "PUT"}


@dataclass(frozen=True)
class Instrument:
    tradingsymbol: str
    name: str                   # underlying, e.g. NIFTY
    expiry: date
    strike: float
    right: str                  # CALL | PUT
    lot_size: int
    tick_size: float
    instrument_token: int
    exchange: str = "NFO"

    @property
    def key(self) -> str:       # "NFO:NIFTY26SEP25000CE" as used by kite.ltp/quote
        return f"{self.exchange}:{self.tradingsymbol}"


def _date(v) -> date | None:
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10]) if v else None


class InstrumentBook:
    def __init__(self, rows: Iterable[dict]):
        self._by_key: dict[tuple, Instrument] = {}
        self._by_symbol: dict[str, Instrument] = {}
        for r in rows:
            t = r.get("instrument_type")
            if t not in RIGHT:
                continue
            inst = Instrument(tradingsymbol=r["tradingsymbol"], name=r["name"], expiry=_date(r["expiry"]),
                              strike=float(r["strike"]), right=RIGHT[t], lot_size=int(r["lot_size"]),
                              tick_size=float(r["tick_size"]), instrument_token=int(r["instrument_token"]),
                              exchange=r.get("exchange", "NFO"))
            self._by_key[(inst.name, inst.expiry, inst.strike, inst.right)] = inst
            self._by_symbol[inst.tradingsymbol] = inst

    def __len__(self) -> int:
        return len(self._by_key)

    def option(self, underlying: str, expiry: date, strike: float, right: str) -> Instrument:
        key = (underlying, expiry, float(strike), right)
        try:
            return self._by_key[key]
        except KeyError:
            near = sorted({k[2] for k in self._by_key if k[0] == underlying and k[1] == expiry and k[3] == right},
                          key=lambda s: abs(s - strike))[:5]
            raise KeyError(f"no {underlying} {expiry} {strike:g} {right} in the NFO instrument list"
                           + (f"; nearest strikes: {near}" if near else "; expiry not listed")) from None

    def by_symbol(self, tradingsymbol: str) -> Instrument:
        return self._by_symbol[tradingsymbol]

    def expiries(self, underlying: str) -> list[date]:
        return sorted({k[1] for k in self._by_key if k[0] == underlying})

    def strikes(self, underlying: str, expiry: date) -> list[float]:
        return sorted({k[2] for k in self._by_key if k[0] == underlying and k[1] == expiry})

    # -- loading ----------------------------------------------------------------------
    @classmethod
    def from_csv(cls, path: str | Path) -> "InstrumentBook":
        with open(path, newline="") as fh:
            return cls(csv.DictReader(fh))

    @classmethod
    def from_kite(cls, kite, cache_dir: str | Path | None = "data", exchange: str = "NFO",
                  today: date | None = None) -> "InstrumentBook":
        cache = Path(cache_dir) / f"kite_instruments_{exchange}_{today or date.today()}.csv" if cache_dir else None
        if cache is not None and cache.exists():
            return cls.from_csv(cache)
        rows = kite.instruments(exchange)
        if cache is not None and rows:
            cache.parent.mkdir(parents=True, exist_ok=True)
            with open(cache, "w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
        return cls(rows)


class SyntheticInstrumentBook(InstrumentBook):
    """Paper/replay book: creates an instrument for any requested contract (no Kite login needed).

    Symbols look like NIFTY260929P25100 (not Kite's real format); LIVE mode always uses
    InstrumentBook.from_kite so real tradingsymbols, lot and tick sizes come from Zerodha.
    """

    def __init__(self, lot_size: int, tick_size: float = 0.05, exchange: str = "NFO"):
        super().__init__([])
        self.lot_size, self.tick_size, self.exchange = lot_size, tick_size, exchange

    def option(self, underlying: str, expiry: date, strike: float, right: str) -> Instrument:
        key = (underlying, expiry, float(strike), right)
        if key not in self._by_key:
            sym = f"{underlying}{expiry:%y%m%d}{right[0]}{float(strike):g}"
            inst = Instrument(sym, underlying, expiry, float(strike), right, self.lot_size, self.tick_size,
                              len(self._by_key) + 1, self.exchange)
            self._by_key[key] = inst
            self._by_symbol[sym] = inst
        return self._by_key[key]
