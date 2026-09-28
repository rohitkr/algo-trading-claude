"""Test doubles: a controllable clock, an instrument book, and the fake Kite (trader.paper.PaperExchange)."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from zerodha.instruments import InstrumentBook

EXPIRY = date(2026, 9, 29)
SENSEX_EXPIRY = date(2026, 10, 1)


class Clock:
    def __init__(self, start: datetime = datetime(2026, 9, 28, 10, 0, 0)):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


def rows():
    out = []
    tok = 1000
    for strike in (24900, 25000, 25100):
        for t in ("CE", "PE"):
            tok += 1
            out.append({"instrument_token": tok, "tradingsymbol": f"NIFTY26929{strike}{t}", "name": "NIFTY",
                        "expiry": EXPIRY.isoformat(), "strike": strike, "tick_size": 0.05, "lot_size": 65,
                        "instrument_type": t, "exchange": "NFO"})
    for strike in (48000, 48100):
        for t in ("CE", "PE"):
            tok += 1
            out.append({"instrument_token": tok, "tradingsymbol": f"BANKNIFTY26SEP{strike}{t}", "name": "BANKNIFTY",
                        "expiry": EXPIRY.isoformat(), "strike": strike, "tick_size": 0.05, "lot_size": 30,
                        "instrument_type": t, "exchange": "NFO"})
    return out


def bfo_rows():
    out = []
    tok = 5000
    for strike in (81000, 81100):
        for t in ("CE", "PE"):
            tok += 1
            out.append({"instrument_token": tok, "tradingsymbol": f"SENSEX26O01{strike}{t}", "name": "SENSEX",
                        "expiry": SENSEX_EXPIRY.isoformat(), "strike": strike, "tick_size": 0.05, "lot_size": 20,
                        "instrument_type": t, "exchange": "BFO"})
    return out


def loader(exchange: str, today: date):
    return InstrumentBook(rows() if exchange == "NFO" else bfo_rows()), "test"
