"""Local stop-loss (2026-10-13): optional per strategy (TRADER_SL_MODE default ZERODHA). LOCAL = no SL order rests
at Zerodha; the algo watches SL / target itself and exits at a marketable price when one is hit. With no live price
for TRADER_STOP_GRACE_SECONDS it falls back to an SL order at Zerodha for good."""
from __future__ import annotations

import pytest

from trader import lifecycle as L
from trader.config import TraderConfig
from trader.strategy import StrategyService

from .test_service import EXPIRY, SYM, Rig


def leg(tmp_path, sl_local=None, **rig):
    r = Rig(tmp_path, **rig)
    r.price(100)
    strat = StrategyService(r.svc, r.repo)
    r.svc.extra_tick = strat.tick
    cfg = {"order_type": "MIS"}
    if sl_local is not None:
        cfg["sl_local"] = sl_local
    res = strat.create_and_trade({"config": cfg, "legs": [dict(
        underlying="NIFTY", expiry=EXPIRY.isoformat(), strike=25000, option_type="CE", side="BUY", lots=2,
        entry_price=100, sl_value=10, sl_type="POINTS", tp_value=30, tp_type="POINTS")]})
    assert res["ok"], res
    r.tick()
    return r, strat, res["confirmed"][0], res["strategy_id"]


def sl_orders(r, tid, working_only=False):
    rows = r.repo.orders(tid, "SL")
    return [o for o in rows if o["status"] not in ("COMPLETE", "CANCELLED", "REJECTED")] if working_only else rows


def test_default_is_unchanged_an_sl_order_rests_at_zerodha(tmp_path):
    r, strat, tid, sid = leg(tmp_path)
    t = r.trade(tid)
    assert t["status"] == L.POSITION_ACTIVE and not t["sl_local"]
    assert len(sl_orders(r, tid, working_only=True)) == 1
    assert strat.view(sid)["config"]["sl_local"] is False      # the account default, fixed on the strategy


def test_local_sl_places_no_sl_order_and_exits_when_the_price_crosses_it(tmp_path):
    r, strat, tid, sid = leg(tmp_path, sl_local=True)
    t = r.trade(tid)
    assert t["status"] == L.POSITION_ACTIVE and t["sl_local"] == 1 and not t["sl_software_only"]
    for _ in range(3):
        r.tick()
    assert sl_orders(r, tid) == []                            # nothing at Zerodha for the operator to see
    assert r.svc.local_watch() is True                        # the monitor ticks fast for it
    r.price(89.5)                                             # through the SL 90
    r.tick(1)
    exits = r.repo.orders(tid, "EXIT")
    assert len(exits) == 1                                    # exit sent on the same tick: no grace, no cancel first
    assert exits[0]["order_type"] == "LIMIT" and exits[0]["price"] < 89.5   # marketable (LTP - buffer)
    r.tick(1)
    t = r.trade(tid)
    assert t["status"] == L.EXITED and t["exit_reason"] == L.STOP_LOSS_HIT
    assert sl_orders(r, tid) == []
    assert r.svc.local_watch() is False


def test_local_target_exits_too(tmp_path):
    r, strat, tid, sid = leg(tmp_path, sl_local=True)
    r.price(131)                                              # target 130
    r.tick(1); r.tick(1)
    t = r.trade(tid)
    assert t["status"] == L.EXITED and t["exit_reason"] == L.TARGET_HIT
    assert sl_orders(r, tid) == []


def test_env_default_local_applies_to_new_strategies(tmp_path):
    r, strat, tid, sid = leg(tmp_path, sl_local_default=True)
    assert r.trade(tid)["sl_local"] == 1 and sl_orders(r, tid) == []
    assert strat.view(sid)["config"]["sl_local"] is True


def test_switch_a_running_strategy_to_local_and_back(tmp_path):
    r, strat, tid, sid = leg(tmp_path)
    assert len(sl_orders(r, tid, working_only=True)) == 1
    strat.set_sl_mode(sid, True)
    r.tick(); r.tick()
    assert sl_orders(r, tid, working_only=True) == []        # the Zerodha SL was cancelled
    t = r.trade(tid)
    assert t["status"] == L.POSITION_ACTIVE and t["sl_local"] == 1 and not t["sl_software_only"]
    strat.set_sl_mode(sid, False)
    r.tick()
    w = sl_orders(r, tid, working_only=True)
    assert len(w) == 1 and w[0]["trigger_price"] == 90 and w[0]["quantity"] == 130
    assert not r.trade(tid)["sl_software_only"]               # our own cancel was not mistaken for an outside one


def test_no_price_falls_back_to_a_zerodha_sl_order(tmp_path):
    r, strat, tid, sid = leg(tmp_path, sl_local=True)
    r.quotes.prices.pop(SYM)                                  # price feed gone
    r.tick(3)
    assert sl_orders(r, tid) == [] and r.trade(tid)["sl_local"] == 1     # a short gap: still local
    r.tick(r.cfg.stop_grace_s)
    t = r.trade(tid)
    assert t["sl_local"] == 0
    assert "LOCAL_SL_NO_PRICE" in r.events(tid)
    r.tick(1)
    assert len(sl_orders(r, tid, working_only=True)) == 1     # protected at Zerodha now


def test_trailing_moves_the_local_stop_without_orders(tmp_path):
    r, strat, tid, sid = leg(tmp_path, sl_local=True)
    r.repo.update_trade(tid, trail_enabled=1, trail_type="POINTS", trail_value=5, trail_step=0.05, target=None)
    r.price(120)
    r.tick(1)
    assert r.trade(tid)["current_sl"] == 115
    assert sl_orders(r, tid) == []
    r.price(114)
    r.tick(1); r.tick(1)
    t = r.trade(tid)
    assert t["status"] == L.EXITED and t["exit_reason"] == L.TRAILING_SL_HIT


def test_sl_mode_env_parsing():
    assert TraderConfig.from_env(None, {"TRADER_SL_MODE": "local"}).sl_local_default is True
    assert TraderConfig.from_env(None, {}).sl_local_default is False
    with pytest.raises(ValueError):
        TraderConfig.from_env(None, {"TRADER_SL_MODE": "kite"})
