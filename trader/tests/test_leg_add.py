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


def test_add_lots_goes_into_the_same_leg_at_the_average_price(tmp_path):
    r, strat, tid, sid = one_leg(tmp_path)                 # BUY 2 lots (130) @ 100, SL 90
    r.price(94)
    res = strat.add_to_leg(tid, {"lots": 1, "price_type": "LIMIT", "entry_price": 94})
    assert res["ok"] and res["trade_id"] == tid, res
    r.tick(); r.tick()
    t = r.trade(tid)
    assert len(strat.view(sid)["legs"]) == 1                # no new row
    assert (t["status"], t["quantity"], t["lots"], t["filled_qty"], t["open_qty"]) == (L.POSITION_ACTIVE, 195, 3, 195, 195)
    assert t["entry_avg_price"] == 98.0                     # (130 x 100 + 65 x 94) / 195
    sl = [o for o in r.repo.orders(tid, "SL") if o["status"] not in ("CANCELLED", "COMPLETE")][-1]
    assert sl["quantity"] == 195                            # the stop covers the whole position
    assert t["mismatch_count"] == 0


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


def test_refused_add_changes_nothing(tmp_path):
    r, strat, tid, sid = one_leg(tmp_path)
    res = strat.add_to_leg(tid, {"lots": 1, "price_type": "LIMIT", "entry_price": None})
    assert not res["ok"] and "limit price" in res["errors"][0]
    t = r.trade(tid)
    assert (t["quantity"], t["lots"]) == (130, 2) and len(strat.view(sid)["legs"]) == 1


def test_add_that_does_not_fill_leaves_the_position_as_it_was(tmp_path):
    r, strat, tid, sid = one_leg(tmp_path)
    r.ex.auto_match = False                                 # the add order rests unfilled
    res = strat.add_to_leg(tid, {"lots": 1, "price_type": "LIMIT", "entry_price": 80})
    assert res["ok"] and r.trade(tid)["quantity"] == 195
    add = r.repo.orders(tid, "ENTRY")[-1]
    r.svc.placer.cancel(add, "test: cancelled")
    for _ in range(3):
        r.tick()
    t = r.trade(tid)
    assert (t["status"], t["quantity"], t["lots"], t["open_qty"]) == (L.POSITION_ACTIVE, 130, 2, 130)


def test_re_enter_a_closed_leg_on_the_other_side(tmp_path):
    r, strat, tid, sid = one_leg(tmp_path)
    r.price(85)
    for _ in range(5):
        r.tick()
    assert r.trade(tid)["status"] == L.EXITED
    res = strat.add_to_leg(tid, {"side": "SELL", "lots": 1, "price_type": "LIMIT", "entry_price": 85,
                                 "stop_loss": 95, "target": 70})
    assert res["ok"], res
    assert r.trade(res["trade_id"])["side"] == "SELL" and r.trade(res["trade_id"])["group_id"] == sid


def test_running_leg_cannot_be_added_to_on_the_other_side(tmp_path):
    r, strat, tid, sid = one_leg(tmp_path)
    res = strat.add_to_leg(tid, {"side": "SELL", "lots": 1, "price_type": "LIMIT", "entry_price": 100,
                                 "stop_loss": 110})
    assert not res["ok"] and "open as BUY" in res["errors"][0]


def test_exit_while_an_add_is_resting_cancels_it_and_closes_what_is_held(tmp_path):
    r, strat, tid, sid = one_leg(tmp_path)
    r.ex.auto_match = False
    assert strat.add_to_leg(tid, {"lots": 1, "price_type": "LIMIT", "entry_price": 80})["ok"]
    r.ex.auto_match = True
    p = r.svc.prepare(tid, "EXIT")
    r.svc.request_exit(tid, p["token"])
    for _ in range(5):
        r.tick()
    t = r.trade(tid)
    assert t["status"] == L.EXITED and t["exited_qty"] == 130 and t["open_qty"] == 0
    assert r.repo.orders(tid, "ENTRY")[-1]["status"] == "CANCELLED"


def test_re_enter_after_a_profit_lock_does_not_exit_at_once(tmp_path):
    # 2026-10-07: strategy closed by its combined SL +3000 (profit locked); re-entering kept +3000, the new P&L
    # restarted at 0 (<= +3000) and the fresh leg was squared off 7 s later
    r, strat, tid, sid = one_leg(tmp_path)                 # BUY 2 lots (130) @ 100
    r.price(120)
    r.tick()
    strat.set_exits(sid, {"exit_sl_pnl": 1300})            # lock +1,300
    r.price(109)                                           # +1,170 -> locked exit
    for _ in range(3):
        r.tick()
    assert r.trade(tid)["exit_reason"] == "STRATEGY_PROFIT_LOCKED"
    res = strat.add_to_leg(tid, {"lots": 2, "price_type": "LIMIT", "entry_price": 109, "stop_loss": 99})
    assert res["ok"], res
    for _ in range(3):
        r.tick()
    assert r.trade(res["trade_id"])["status"] == L.POSITION_ACTIVE
    assert json.loads(r.repo.strategy(sid)["config"])["exit_sl_pnl"] is None
