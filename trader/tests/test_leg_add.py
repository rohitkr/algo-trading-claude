"""Add lots to a running strategy leg / re-enter a closed one (2026-10-05): a new leg of the same strategy, same
contract and side, with its own SL/target; a finished strategy is reopened and its rules restart from zero."""
from __future__ import annotations

import json

from trader import lifecycle as L
from trader.strategy import StrategyService

from .fakes import EXPIRY
from .test_service import SYM, Rig


def one_leg(tmp_path, **cfg):
    r = Rig(tmp_path)
    r.price(100)
    strat = StrategyService(r.svc, r.repo)
    r.svc.extra_tick = strat.tick
    res = strat.create_and_trade({"config": {"order_type": "MIS", **cfg}, "legs": [dict(
        underlying="NIFTY", expiry=EXPIRY.isoformat(), strike=25000, option_type="CE", side="BUY", lots=2,
        entry_price=100, sl_value=10, sl_type="POINTS", tp_value=30, tp_type="POINTS")]})
    assert res["ok"], res
    r.tick()
    return r, strat, res["confirmed"][0], res["strategy_id"]


def test_add_lots_to_a_running_leg(tmp_path):
    r, strat, tid, sid = one_leg(tmp_path)
    res = strat.add_to_leg(tid, {"lots": 1, "price_type": "LIMIT", "entry_price": 100, "stop_loss": 90, "target": 130})
    assert res["ok"], res
    r.tick(); r.tick()
    new = r.trade(res["trade_id"])
    assert new["group_id"] == sid and new["tradingsymbol"] == SYM and new["side"] == "BUY"
    assert new["status"] == L.POSITION_ACTIVE and new["quantity"] == 65
    assert r.trade(tid)["status"] == L.POSITION_ACTIVE and r.trade(tid)["mismatch_count"] == 0
    assert len(strat.view(sid)["legs"]) == 2


def test_re_enter_a_closed_leg_reopens_the_strategy(tmp_path):
    r, strat, tid, sid = one_leg(tmp_path, exit_loss_amount=1000)
    r.price(85)                                       # stop 90 hit: -15 x 130 = -1,950, strategy DONE
    for _ in range(5):
        r.tick()
    assert r.trade(tid)["status"] == L.EXITED and r.repo.strategy(sid)["status"] == "DONE"
    res = strat.add_to_leg(tid, {"lots": 2, "price_type": "LIMIT", "entry_price": 85, "stop_loss": 75})
    assert res["ok"], res
    for _ in range(3):
        r.tick()
    s = r.repo.strategy(sid)
    # reopened, and the old loss does not trip the ₹1,000 strategy loss rule again straight away
    assert s["status"] == "ACTIVE" and s["pnl_base"] < -1000
    assert r.trade(res["trade_id"])["status"] == L.POSITION_ACTIVE
    assert not r.trade(res["trade_id"])["pending_exit_reason"]


def test_refused_add_leaves_nothing_on_the_strategy(tmp_path):
    r, strat, tid, sid = one_leg(tmp_path)
    res = strat.add_to_leg(tid, {"lots": 1, "price_type": "LIMIT", "entry_price": 100, "stop_loss": 110})
    assert not res["ok"] and res["errors"]
    assert len(strat.view(sid)["legs"]) == 1
