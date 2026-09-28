"""Breeze usage (idle = 0 calls, one cached quote per symbol, budget pacing) and trade edits."""
from __future__ import annotations

import pytest

from trader import lifecycle as L
from trader.market import BreezeQuotes
from trader.repository import Repository
from trader.service import ActionError
from zerodha.instruments import InstrumentBook

from .fakes import EXPIRY, Clock, rows
from .test_service import SYM, Rig, _active


class CountingQuotes:
    name = "counting"

    def __init__(self):
        self.calls = []

    def ltp(self, inst, max_age=None):
        self.calls.append(inst.tradingsymbol)
        return 100.0

    def status(self):
        return {"source": self.name}


def test_idle_makes_zero_price_calls(tmp_path):
    r = Rig(tmp_path)
    cq = CountingQuotes()
    r.svc.quotes = cq
    for _ in range(20):
        r.tick()
    r.svc.dashboard()
    r.svc.contract("NIFTY", EXPIRY.isoformat(), 25000, "CE")      # form lookup without "Get LTP"
    assert cq.calls == []


def test_breeze_quotes_cached_per_symbol_and_paced(tmp_path):
    clock = Clock()
    mono = [0.0]
    repo = Repository(tmp_path / "q.sqlite", clock)

    class FakeClient:
        n = 0

        def get_quote(self, req):
            FakeClient.n += 1
            repo.add_api_calls(clock().date(), 1)
            return {"ltp": 100}

    bq = BreezeQuotes(settings=None, usage_store=repo, daily_budget=2000, ttl_s=15, client=FakeClient(),
                      monotonic=lambda: mono[0], clock=clock, reserve=100)
    bq._request = lambda inst: None
    inst = InstrumentBook(rows()).by_symbol(SYM)
    for _ in range(10):                  # many requests (ticks, UI refreshes) within the interval: one call
        bq.ltp(inst)
    assert FakeClient.n == 1
    mono[0] += 16                        # 19,800 s left / 1,899 spare calls = ~10 s pacing: the 15 s interval wins
    bq.ltp(inst)
    assert FakeClient.n == 2
    repo.add_api_calls(clock().date(), 1850)             # budget nearly gone: pacing stretches the interval
    mono[0] += 16
    bq.ltp(inst)
    assert FakeClient.n == 2 and bq.pacing_interval() > 16


# -- edits ----------------------------------------------------------------------------------------------
def _edit(r, tid, **changes):
    p = r.svc.prepare_edit(tid, changes)
    assert p["ok"], p
    return r.svc.apply_edit(tid, p["token"])


def test_edit_pending_entry_modifies_order_in_place(tmp_path):
    r = Rig(tmp_path)
    tid = r.open_trade(lots=1, entry_price=95, stop_loss=90, target=120)
    entry = r.repo.orders(tid, "ENTRY")[0]
    _edit(r, tid, entry_price=98, lots=2, stop_loss=92)
    t = r.trade(tid)
    assert (t["entry_price"], t["lots"], t["quantity"], t["initial_sl"]) == (98, 2, 130, 92)
    o = r.ex.orders_[entry["broker_order_id"]]
    assert (o["price"], o["quantity"]) == (98, 130)
    assert len(r.places()) == 1                       # modified, never re-placed
    ev = [e for e in r.repo.events(tid) if e["event"] == "TRADE_EDITED"][0]
    assert ev["detail"]["diff"]["entry_price"] == [95, 98]


def test_edit_validation_and_single_use_token(tmp_path):
    r = Rig(tmp_path)
    tid = r.open_trade(entry_price=95, stop_loss=90)
    assert not r.svc.prepare_edit(tid, {"stop_loss": 96})["ok"]         # BUY: SL above entry
    p = r.svc.prepare_edit(tid, {"target": 125})
    r.svc.apply_edit(tid, p["token"])
    assert r.trade(tid)["target"] == 125
    with pytest.raises(ActionError):
        r.svc.apply_edit(tid, p["token"])


def test_edit_qty_not_below_filled(tmp_path):
    r = Rig(tmp_path, auto=False)
    r.price(100)
    tid = r.open_trade(lots=3)
    r.ex.fill(r.repo.orders(tid, "ENTRY")[0]["broker_order_id"], 130, 100)
    r.tick()
    p = r.svc.prepare_edit(tid, {"lots": 1})
    assert not p["ok"] and any("below the filled" in e for e in p["errors"])


def test_edit_active_sl_modifies_resting_order_and_target(tmp_path):
    r = Rig(tmp_path)
    tid = _active(r)
    r.price(110)
    r.tick()
    _edit(r, tid, stop_loss=104, target=140)            # SL into profit is fine for an open position
    sl = r.repo.orders(tid, "SL")[0]
    assert sl["trigger_price"] == 104 and r.ex.orders_[sl["broker_order_id"]]["trigger_price"] == 104
    assert not r.svc.prepare_edit(tid, {"stop_loss": 111})["ok"]     # through the current price
    r.price(103)
    r.tick()
    t = r.trade(tid)
    assert (t["status"], t["exit_reason"]) == (L.EXITED, L.STOP_LOSS_HIT)


def test_edit_active_enable_trailing_and_partial(tmp_path):
    r = Rig(tmp_path)
    tid = _active(r, lots=2)
    _edit(r, tid, trail_enabled=True, trail_value=5, partial_enabled=True, partial_lots=1, partial_price=112)
    r.price(112)
    r.tick()
    t = r.trade(tid)
    assert t["open_qty"] == 65 and t["current_sl"] == 107


def test_edit_refused_after_exit(tmp_path):
    r = Rig(tmp_path)
    tid = _active(r)
    r.price(130)
    r.tick()
    assert not r.svc.prepare_edit(tid, {"target": 150})["ok"]


def test_price_changed_in_kite_is_adopted(tmp_path):
    r = Rig(tmp_path)
    tid = r.open_trade(entry_price=95, stop_loss=90)
    oid = r.repo.orders(tid, "ENTRY")[0]["broker_order_id"]
    r.ex.modify_order("regular", oid, price=97)          # changed by hand in Kite
    r.tick()
    assert r.trade(tid)["entry_price"] == 97
