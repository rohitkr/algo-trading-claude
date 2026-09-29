"""MCX commodity options (CRUDEOIL, CRUDEOILM, GOLDM): units per lot, lots at the Kite boundary, the
futures contract as "spot", and MCX's own trading window / square-off."""
from __future__ import annotations

from datetime import date, datetime, time

import pytest

from trader import lifecycle as L
from trader.broker import KiteTraderBroker, OrderSpec
from trader.config import TraderConfig, exchange_for
from trader.instruments import InstrumentService
from trader.market import ManualQuotes
from trader.paper import PaperExchange
from trader.repository import Repository
from trader.service import TradeService
from zerodha.instruments import InstrumentBook

from .fakes import Clock

OPT_EXP = date(2026, 10, 15)
SYM = "CRUDEOIL26OCT5500CE"


def mcx_rows():
    """Shaped like Kite's MCX dump: lot_size 1 on every contract, futures alongside the options."""
    out = [{"instrument_token": 9001, "tradingsymbol": "CRUDEOIL26OCTFUT", "name": "CRUDEOIL", "expiry": "2026-10-19",
            "strike": 0, "tick_size": 1, "lot_size": 1, "instrument_type": "FUT", "exchange": "MCX"},
           {"instrument_token": 9002, "tradingsymbol": "CRUDEOIL26SEPFUT", "name": "CRUDEOIL", "expiry": "2026-09-18",
            "strike": 0, "tick_size": 1, "lot_size": 1, "instrument_type": "FUT", "exchange": "MCX"},
           {"instrument_token": 9101, "tradingsymbol": "GOLDM26OCTFUT", "name": "GOLDM", "expiry": "2026-10-05",
            "strike": 0, "tick_size": 1, "lot_size": 1, "instrument_type": "FUT", "exchange": "MCX"},
           {"instrument_token": 9102, "tradingsymbol": "GOLDM26NOVFUT", "name": "GOLDM", "expiry": "2026-11-05",
            "strike": 0, "tick_size": 1, "lot_size": 1, "instrument_type": "FUT", "exchange": "MCX"},
           {"instrument_token": 9201, "tradingsymbol": "SILVERM26OCT90000CE", "name": "SILVERM", "expiry": "2026-10-20",
            "strike": 90000, "tick_size": 1, "lot_size": 1, "instrument_type": "CE", "exchange": "MCX"}]
    tok = 8000
    for strike in (5450, 5500, 5550):
        for t in ("CE", "PE"):
            tok += 1
            out.append({"instrument_token": tok, "tradingsymbol": f"CRUDEOIL26OCT{strike}{t}", "name": "CRUDEOIL",
                        "expiry": OPT_EXP.isoformat(), "strike": strike, "tick_size": 0.1, "lot_size": 1,
                        "instrument_type": t, "exchange": "MCX"})
    out.append({"instrument_token": 8100, "tradingsymbol": "GOLDM26OCT147000CE", "name": "GOLDM", "expiry": "2026-10-29",
                "strike": 147000, "tick_size": 0.5, "lot_size": 1, "instrument_type": "CE", "exchange": "MCX"})
    return out


def loader(exchange, today):
    return InstrumentBook(mcx_rows() if exchange == "MCX" else []), "test"


def test_exchange_units_and_sessions_from_config():
    assert [exchange_for(u) for u in ("NIFTY", "SENSEX", "CRUDEOIL", "crudeoilm", "GOLDM")] == \
        ["NFO", "BFO", "MCX", "MCX", "MCX"]
    cfg = TraderConfig.from_env(env_file=None, environ={"TRADER_LOT_UNITS_SILVERM": "5",
                                                        "TRADER_MCX_TRADING_END": "23:40"})
    assert cfg.lot_units["CRUDEOIL"] == 100 and cfg.lot_units["SILVERM"] == 5
    assert cfg.session_for("CRUDEOIL") == (time(9, 0), time(23, 40), time(23, 20))
    assert cfg.session_for("NIFTY") == (time(9, 15), time(15, 0), time(15, 15))
    assert "CRUDEOILM" in TraderConfig().underlyings and "GOLDM" in TraderConfig().underlyings


def test_mcx_book_uses_units_per_lot_and_the_options_future_as_spot():
    svc = InstrumentService(loader, ("CRUDEOIL", "GOLDM"), Clock(), TraderConfig().lot_units)
    assert svc.resolve("CRUDEOIL", OPT_EXP, 5500, "CE").lot_size == 100
    assert svc.units_per_lot("MCX", SYM) == 100 and svc.units_per_lot("NFO", "anything") == 1
    assert svc.resolve("GOLDM", date(2026, 10, 29), 147000, "CE").lot_size == 10
    with pytest.raises(KeyError):                                   # no configured unit count: not offered
        svc.by_symbol("MCX", "SILVERM26OCT90000CE")
    assert svc.spot_future("CRUDEOIL") == (9001, "CRUDEOIL26OCTFUT")   # expired Sep future skipped
    assert svc.spot_future("GOLDM") == (9102, "GOLDM26NOVFUT")        # Oct-29 options are on the Nov future


class FakeKite:
    def __init__(self):
        self.placed, self.modified = [], []

    def place_order(self, **kw):
        self.placed.append(kw)
        return "1"

    def modify_order(self, **kw):
        self.modified.append(kw)
        return "1"

    def orders(self):
        return [{"order_id": "1", "status": "COMPLETE", "tradingsymbol": SYM, "exchange": "MCX",
                 "transaction_type": "BUY", "quantity": 2, "filled_quantity": 2, "average_price": 100},
                {"order_id": "2", "status": "COMPLETE", "tradingsymbol": "NIFTY2692925000CE", "exchange": "NFO",
                 "transaction_type": "BUY", "quantity": 65, "filled_quantity": 65, "average_price": 10}]

    def positions(self):
        return {"net": [{"exchange": "MCX", "tradingsymbol": SYM, "product": "NRML", "quantity": 2},
                        {"exchange": "MCX", "tradingsymbol": "SOMEONEELSE", "product": "NRML", "quantity": 3},
                        {"exchange": "NFO", "tradingsymbol": "NIFTY2692925000CE", "product": "MIS", "quantity": 65}]}


def test_broker_sends_lots_to_kite_and_reads_back_units():
    svc = InstrumentService(loader, ("CRUDEOIL",), Clock(), TraderConfig().lot_units)
    kite = FakeKite()
    b = KiteTraderBroker(kite, live=False, units=svc.units_per_lot)
    b.place(OrderSpec("MCX", SYM, "BUY", 200, "NRML", "LIMIT", price=100.0))
    assert kite.placed[-1]["quantity"] == 2
    with pytest.raises(ValueError):
        b.place(OrderSpec("MCX", SYM, "BUY", 150, "NRML", "LIMIT", price=100.0))    # not a whole lot
    b.modify("1", quantity=300, exchange="MCX", tradingsymbol=SYM)
    assert kite.modified[-1]["quantity"] == 3
    b.modify("1", price=101.0)                                                     # no quantity: fine
    snap = b.snapshot(datetime(2026, 9, 28, 20, 0))
    assert (snap.by_id["1"].quantity, snap.by_id["1"].filled_qty) == (200, 200)
    assert snap.by_id["2"].quantity == 65                                        # NFO untouched
    assert snap.net("MCX", SYM, "NRML") == 200 and snap.net("MCX", "SOMEONEELSE", "NRML") == 3
    assert snap.net("NFO", "NIFTY2692925000CE", "MIS") == 65


class McxRig:
    def __init__(self, tmp_path, now=datetime(2026, 9, 28, 20, 0)):
        self.clock = Clock(now)
        self.cfg = TraderConfig(db_path=tmp_path / "t.sqlite", audit_dir=tmp_path / "logs",
                                paper_state=tmp_path / "paper.json", product="NRML")
        self.quotes = ManualQuotes()
        self.quotes.set(SYM, 100.0)
        self.ex = PaperExchange(self.quotes.price_for, self.cfg.paper_state, clock=self.clock)
        self.repo = Repository(self.cfg.db_path, self.clock)
        self.instruments = InstrumentService(loader, ("CRUDEOIL",), self.clock, self.cfg.lot_units)
        self.svc = TradeService(self.cfg, self.repo, KiteTraderBroker(self.ex, live=False,
                                                                      units=self.instruments.units_per_lot),
                                self.instruments, self.quotes, self.clock)

    def tick(self, s=3):
        self.clock.advance(s)
        return self.svc.tick()

    def preview(self, **kw):
        req = dict(underlying="CRUDEOIL", expiry=OPT_EXP.isoformat(), strike=5500, option_type="CE", side="BUY",
                   lots=2, entry_price=100, stop_loss=90, target=130, product="NRML")
        req.update(kw)
        return self.svc.preview(req)


def test_paper_crudeoil_trade_in_the_evening_session(tmp_path):
    r = McxRig(tmp_path)                                   # 20:00: NSE closed, MCX open
    p = r.preview()
    assert p["ok"], p
    s = p["summary"]
    assert (s["quantity"], s["lot_size"], s["max_loss_at_sl"]) == (200, 100, 2000.0)   # 10 x 200 barrels
    r.svc.confirm(p["trade_id"], p["token"])
    tid = p["trade_id"]
    place = [c for c in r.ex.calls if c[0] == "place"][0]
    assert 2 in place                                       # Kite got 2 LOTS
    r.tick()
    t = r.repo.trade(tid)
    assert (t["status"], t["filled_qty"], t["open_qty"]) == (L.POSITION_ACTIVE, 200, 200)
    sl = r.repo.orders(tid, "SL")[0]
    assert sl["quantity"] == 200
    r.quotes.set(SYM, 110.0)
    r.tick()
    assert r.repo.trade(tid)["unrealized_pnl"] == pytest.approx(10 * 200)
    r.tick()
    assert r.repo.trade(tid)["status"] == L.POSITION_ACTIVE      # reconciliation agrees (units both sides)
    r.quotes.set(SYM, 130.0)
    r.tick()
    t = r.repo.trade(tid)
    assert t["status"] == L.EXITED and t["realized_pnl"] == pytest.approx(30 * 200)


def test_mcx_window_and_square_off(tmp_path):
    r = McxRig(tmp_path, now=datetime(2026, 9, 28, 23, 16))
    p = r.preview()
    assert not p["ok"] and any(c["name"] == "trading_end" and not c["passed"] for c in p["risk"])
    r = McxRig(tmp_path / "b", now=datetime(2026, 9, 28, 23, 10))
    p = r.preview()
    assert p["ok"], p
    r.svc.confirm(p["trade_id"], p["token"])
    r.tick()
    assert r.repo.trade(p["trade_id"])["status"] == L.POSITION_ACTIVE
    r.tick(600)                                            # 23:20: MCX square-off
    r.tick()
    t = r.repo.trade(p["trade_id"])
    assert t["exit_reason"] == L.SQUARE_OFF or t["pending_exit_reason"] == L.SQUARE_OFF


def test_mcx_without_kite_prices_says_why(tmp_path):
    r = McxRig(tmp_path)
    r.svc.quotes.name = "breeze"                      # MARKET_DATA_PROVIDER=BREEZE: no MCX data at all
    s = r.svc.spot("GOLDM", with_ltp=True)
    assert s["spot"] is None and "MARKET_DATA_PROVIDER=KITE" in s["error"]
    c = r.svc.contract("CRUDEOIL", OPT_EXP.isoformat(), 5500, "CE", with_ltp=True)
    assert c["ltp"] is None and "MARKET_DATA_PROVIDER=KITE" in c["price_error"]
    r.svc.quotes.name = "kite"
    assert r.svc.spot("NIFTY")["spot"] is None and "error" not in r.svc.spot("NIFTY")
