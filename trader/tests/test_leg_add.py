"""Add lots to a running strategy leg / re-enter a closed one (2026-10-05): a new leg of the same strategy, same
contract and side, with its own SL/target; a finished strategy is reopened and its rules restart from zero."""
from __future__ import annotations

import json

import pytest

from trader import lifecycle as L
from trader.service import ActionError
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
    cfg = json.loads(r.repo.strategy(sid)["config"])
    assert cfg["exit_sl_pnl"] is None and cfg["exit_loss_amount"] is None and cfg["exit_profit_amount"] is None


def test_waiting_entry_goes_to_market_and_fills(tmp_path):
    r = Rig(tmp_path)
    r.price(100)
    strat = StrategyService(r.svc, r.repo)
    res = strat.create_and_trade({"config": {"order_type": "MIS"}, "legs": [dict(
        underlying="NIFTY", expiry=EXPIRY.isoformat(), strike=25000, option_type="CE", side="BUY", lots=1,
        entry_price=95, sl_value=10, sl_type="POINTS")]})            # limit below the market: rests unfilled
    tid = res["confirmed"][0]
    r.tick()
    assert r.trade(tid)["status"] == L.ENTRY_PENDING
    out = r.svc.entry_to_market(tid)
    assert out["price"] == 102.0                            # LTP 100 + 2% buffer, rounded up to the tick
    t = r.trade(tid)
    assert t["status"] == L.POSITION_ACTIVE and t["entry_avg_price"] <= 102.0


def test_market_button_refuses_a_running_position(tmp_path):
    import pytest
    from trader.service import ActionError
    r, strat, tid, sid = one_leg(tmp_path)
    with pytest.raises(ActionError):
        r.svc.entry_to_market(tid)


def resting_add(tmp_path):
    r, strat, tid, sid = one_leg(tmp_path)                 # BUY 2 lots (130) @ 100, SL 90
    r.ex.auto_match = False                                 # the add rests unfilled
    assert strat.add_to_leg(tid, {"lots": 1, "price_type": "LIMIT", "entry_price": 95})["ok"]
    r.tick()
    return r, strat, tid


def test_resting_add_shows_on_the_leg_until_it_fills(tmp_path):
    r, strat, tid = resting_add(tmp_path)
    pa = r.svc.trade_view(tid)["pending_adds"][0]
    assert pa and pa["qty"] == 65 and pa["price"] == 95 and pa["filled"] == 0
    r.ex.auto_match = True
    r.svc.update_add(tid, "market")                         # fill it now
    v = r.svc.trade_view(tid)
    assert v["pending_adds"] == [] and v["open_qty"] == 195 and v["quantity"] == 195


def test_resting_add_price_can_be_changed_and_it_can_be_cancelled(tmp_path):
    r, strat, tid = resting_add(tmp_path)
    assert r.svc.update_add(tid, "price", 96.03)["price"] == 96.05
    assert r.svc.trade_view(tid)["pending_adds"][0]["price"] == 96.05
    with pytest.raises(ActionError):
        r.svc.update_add(tid, "price", 89)                  # below the SL 90: refused
    r.svc.update_add(tid, "cancel")
    for _ in range(2):
        r.tick()
    v = r.svc.trade_view(tid)
    assert v["pending_adds"] == [] and (v["quantity"], v["open_qty"]) == (130, 130)


def test_several_adds_can_rest_at_different_prices_and_each_merges(tmp_path):
    # 2026-10-08: a second, lower add was refused ("an earlier add is still working")
    r, strat, tid = resting_add(tmp_path)                   # add #1: 65 @ 95, resting
    assert strat.add_to_leg(tid, {"lots": 1, "price_type": "LIMIT", "entry_price": 93})["ok"]
    r.tick()
    adds = r.svc.trade_view(tid)["pending_adds"]
    assert [a["price"] for a in adds] == [95, 93]
    assert r.trade(tid)["quantity"] == 260                  # 130 held + 2 x 65 waiting
    r.ex.auto_match = True
    r.svc.update_add(tid, "market", add_id=adds[1]["id"])  # fill the lower one now
    v = r.svc.trade_view(tid)
    assert [a["price"] for a in v["pending_adds"]] == [95] and v["open_qty"] == 195
    r.svc.update_add(tid, "cancel", add_id=adds[0]["id"])  # drop the other
    for _ in range(2):
        r.tick()
    v = r.svc.trade_view(tid)
    assert v["pending_adds"] == [] and (v["quantity"], v["open_qty"]) == (195, 195)


def test_same_strike_twice_in_one_new_strategy_is_allowed(tmp_path):
    r = Rig(tmp_path)
    r.price(100)
    strat = StrategyService(r.svc, r.repo)
    leg = dict(underlying="NIFTY", expiry=EXPIRY.isoformat(), strike=25000, option_type="CE", side="BUY", lots=1,
               sl_value=10, sl_type="POINTS")
    res = strat.create_and_trade({"config": {"order_type": "MIS"},
                                  "legs": [{**leg, "entry_price": 100}, {**leg, "entry_price": 97}]})
    assert res["ok"] and len(res["confirmed"]) == 2, res


def test_resting_add_lots_can_be_changed(tmp_path):
    # 2026-10-09: the add's Edit only changed the price
    r, strat, tid = resting_add(tmp_path)                   # add 1 lot (65) @ 95 resting; 130 held
    add = r.svc.trade_view(tid)["pending_adds"][0]
    r.svc.update_add(tid, "price", 94, add_id=add["id"], lots=3)
    r.tick()
    v = r.svc.trade_view(tid)
    assert (v["pending_adds"][0]["qty"], v["pending_adds"][0]["price"]) == (195, 94)
    assert v["quantity"] == 130 + 195 and v["open_qty"] == 130
    r.ex.auto_match = True
    r.svc.update_add(tid, "market", add_id=add["id"])
    assert r.svc.trade_view(tid)["open_qty"] == 325
