"""Multi-leg strategies: leg placement reuses the single-trade engine untouched; the only new behaviour
tested here is StrategyService's own layer - combined P&L exit/trailing, move-SL-to-cost, and BTST carry."""
from __future__ import annotations

import pytest

from trader import lifecycle as L
from trader.strategy import StrategyService

from .fakes import EXPIRY
from .test_service import Rig

CE, PE = "NIFTY2692925000CE", "NIFTY2692925000PE"


def _strategy(r, **cfg):
    strat = StrategyService(r.svc, r.repo)
    r.svc.extra_tick = strat.tick          # mirrors app.py's wiring: this is what makes strategy.tick() run
    return strat


def _short_straddle_payload(**cfg_overrides):
    cfg = {"order_type": "MIS"}
    cfg.update(cfg_overrides)
    legs = [
        dict(underlying="NIFTY", expiry=EXPIRY.isoformat(), strike=25000, option_type="CE", side="SELL",
             lots=1, entry_price=100, sl_value=20, sl_type="POINTS", tp_value=40, tp_type="POINTS"),
        dict(underlying="NIFTY", expiry=EXPIRY.isoformat(), strike=25000, option_type="PE", side="SELL",
             lots=1, entry_price=90, sl_value=20, sl_type="POINTS", tp_value=40, tp_type="POINTS"),
    ]
    return {"config": cfg, "legs": legs}


def test_trade_all_places_and_groups_both_legs(tmp_path):
    r = Rig(tmp_path)
    r.quotes.set(CE, 100)
    r.quotes.set(PE, 90)
    strat = _strategy(r)
    res = strat.create_and_trade(_short_straddle_payload())
    assert res["ok"], res
    assert len(res["confirmed"]) == 2 and not res["failed"]
    t1, t2 = r.trade(res["confirmed"][0]), r.trade(res["confirmed"][1])
    assert t1["group_id"] == t2["group_id"] == res["strategy_id"]
    assert t1["product"] == t2["product"] == "MIS"              # order_type MIS -> product MIS
    s = strat.view(res["strategy_id"])
    assert s["status"] == "ACTIVE" and len(s["legs"]) == 2


def test_sell_leg_sl_tp_resolved_correctly(tmp_path):
    r = Rig(tmp_path)
    r.quotes.set(CE, 100)
    r.quotes.set(PE, 90)
    strat = _strategy(r)
    res = strat.create_and_trade(_short_straddle_payload())
    assert res["ok"], res
    ce_trade = next(r.trade(tid) for tid in res["confirmed"] if r.trade(tid)["tradingsymbol"] == CE)
    # SELL: stop-loss ABOVE entry, target BELOW entry (points)
    assert (ce_trade["initial_sl"], ce_trade["target"]) == (120, 60)


def test_bad_leg_places_nothing(tmp_path):
    r = Rig(tmp_path)
    r.quotes.set(CE, 100)
    r.quotes.set(PE, 90)
    strat = _strategy(r)
    payload = _short_straddle_payload()
    payload["legs"][1]["entry_price"] = -5        # invalid: triggers validate() failure on leg 2
    res = strat.create_and_trade(payload)
    assert not res["ok"] and res["errors"]
    assert r.places() == []                        # nothing reached the broker for either leg
    assert strat.repo.strategies() == []            # no strategy row created either


def test_no_trade_after_blocks_creation(tmp_path):
    r = Rig(tmp_path)
    strat = _strategy(r)
    res = strat.create_and_trade(_short_straddle_payload(no_trade_after="09:00"))  # clock starts at 10:00
    assert not res["ok"] and "no-trade-after" in res["errors"][0]
    assert r.places() == []


def test_combined_profit_target_exits_both_legs(tmp_path):
    r = Rig(tmp_path)
    r.quotes.set(CE, 100)
    r.quotes.set(PE, 90)
    strat = _strategy(r)
    res = strat.create_and_trade(_short_straddle_payload(exit_profit_amount=1000))
    assert res["ok"], res
    r.tick()                                        # both SELL entries fill immediately (LIMIT at/above LTP... )
    for tid in res["confirmed"]:
        assert r.trade(tid)["status"] == L.POSITION_ACTIVE
    r.quotes.set(CE, 70); r.quotes.set(PE, 60)       # both SELL legs now well in profit: (30+30)*65 = 3900
    r.tick()                                          # strategy.tick() sets pending_exit_reason on both legs
    r.tick()                                          # next pass: the ordinary engine places + fills the exit
    for tid in res["confirmed"]:
        t = r.trade(tid)
        assert (t["status"], t["exit_reason"]) == (L.EXITED, "STRATEGY_PROFIT_TARGET")
    assert strat.view(res["strategy_id"])["status"] == "DONE"


def test_combined_loss_limit_exits_both_legs(tmp_path):
    r = Rig(tmp_path)
    r.quotes.set(CE, 100)
    r.quotes.set(PE, 90)
    strat = _strategy(r)
    res = strat.create_and_trade(_short_straddle_payload(exit_loss_amount=1000))
    assert res["ok"], res
    r.tick()
    r.quotes.set(CE, 115); r.quotes.set(PE, 105)     # both SELL legs losing: -(15+15)*65 = -1950, past -1000
    r.tick()
    r.tick()
    for tid in res["confirmed"]:
        t = r.trade(tid)
        assert (t["status"], t["exit_reason"]) == (L.EXITED, "STRATEGY_LOSS_LIMIT")


def test_move_sl_to_cost_tightens_stop(tmp_path):
    r = Rig(tmp_path)
    r.quotes.set(CE, 100)
    r.quotes.set(PE, 90)
    strat = _strategy(r)
    res = strat.create_and_trade(_short_straddle_payload(move_sl_to_cost_enabled=True, move_sl_to_cost_at=10))
    assert res["ok"], res
    r.tick()
    ce_tid = next(tid for tid in res["confirmed"] if r.trade(tid)["tradingsymbol"] == CE)
    assert r.trade(ce_tid)["current_sl"] == 120                 # unchanged: not moved 10pts in favour yet
    r.quotes.set(CE, 88)                                        # SELL leg: favourable move of 12 points
    r.tick()
    assert r.trade(ce_tid)["current_sl"] == 100                 # pulled to cost (entry avg)
    assert "SL_MOVED_TO_COST" in r.events(ce_tid)


def test_btst_leg_skips_global_square_off(tmp_path):
    from datetime import datetime, time

    from .fakes import Clock
    clock = Clock(datetime(2026, 9, 28, 14, 59))
    r = Rig(tmp_path, clock=clock, square_off_time=time(15, 0))
    r.quotes.set(CE, 100)
    strat = _strategy(r)
    payload = {"config": {"order_type": "BTST"},
              "legs": [dict(underlying="NIFTY", expiry=EXPIRY.isoformat(), strike=25000, option_type="CE",
                            side="SELL", lots=1, entry_price=100, sl_value=20, sl_type="POINTS")]}
    res = strat.create_and_trade(payload)
    assert res["ok"], res
    tid = res["confirmed"][0]
    assert r.trade(tid)["order_type"] == "BTST" and r.trade(tid)["product"] == "NRML"
    clock.now = datetime(2026, 9, 28, 15, 5)         # past the global square-off time
    r.svc.tick()
    assert r.trade(tid)["status"] == L.POSITION_ACTIVE   # BTST: not squared off, carries on


def test_profit_trailing_lock_and_trail(tmp_path):
    r = Rig(tmp_path)
    r.quotes.set(CE, 100)
    r.quotes.set(PE, 90)
    strat = _strategy(r)
    res = strat.create_and_trade(_short_straddle_payload(
        trailing_mode="LOCK_AND_TRAIL", lock_if_profit_reaches=500, lock_profit_at=200,
        trail_every_increase=200, trail_profit_by=150))
    assert res["ok"], res
    r.tick()
    sid = res["strategy_id"]
    # combined profit reaches ~500 (10+10)*65=1300 already past the lock threshold on this move
    r.quotes.set(CE, 92); r.quotes.set(PE, 82)        # +8+8 = 16*65=1040 profit
    r.tick()
    s = r.repo.strategy(sid)
    assert s["locked_pnl"] is not None and s["locked_pnl"] >= 200       # lock engaged, or trail overtook it
    # now let profit fall back below the locked floor: should trigger a full exit
    r.quotes.set(CE, 99); r.quotes.set(PE, 89)
    r.tick()
    r.tick()
    for tid in res["confirmed"]:
        t = r.trade(tid)
        assert (t["status"], t["exit_reason"]) == (L.EXITED, "STRATEGY_TRAIL_STOP")


def test_leg_without_sl_gets_a_wide_auto_stop(tmp_path):
    r = Rig(tmp_path, max_loss_per_trade=10000)
    r.quotes.set(CE, 100)
    strat = _strategy(r)
    payload = {"config": {"order_type": "MIS"},
              "legs": [dict(underlying="NIFTY", expiry=EXPIRY.isoformat(), strike=25000, option_type="CE",
                            side="BUY", lots=1, entry_price=100)]}       # no sl_value at all
    res = strat.create_and_trade(payload)
    assert res["ok"], res
    t = r.trade(res["confirmed"][0])
    # BUY: stop below entry, and the account's own risk check already bounds it to <= max_loss_per_trade
    assert t["initial_sl"] < 100
    assert abs(100 - t["initial_sl"]) * t["quantity"] <= 10000 + 1e-6


def test_buy_legs_placed_before_sell_legs(tmp_path):
    r = Rig(tmp_path)
    r.quotes.set(CE, 100)
    r.quotes.set(PE, 90)
    strat = _strategy(r)
    # order in the payload is SELL-then-BUY; the engine must still place BUY first
    legs = [
        dict(underlying="NIFTY", expiry=EXPIRY.isoformat(), strike=25000, option_type="CE", side="SELL",
             lots=1, entry_price=100, sl_value=20, sl_type="POINTS"),
        dict(underlying="NIFTY", expiry=EXPIRY.isoformat(), strike=25000, option_type="PE", side="BUY",
             lots=1, entry_price=90, sl_value=20, sl_type="POINTS"),
    ]
    res = strat.create_and_trade({"config": {"order_type": "MIS"}, "legs": legs})
    assert res["ok"], res
    order_ids = res["confirmed"]
    first, second = r.trade(order_ids[0]), r.trade(order_ids[1])
    assert first["side"] == "BUY" and second["side"] == "SELL"
