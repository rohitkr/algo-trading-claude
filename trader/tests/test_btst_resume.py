"""2026-10-14: a BTST leg was closed the same evening by its strategy's square-off time (15:40). The exit started after
the market had closed: the SL order was cancelled first, then Zerodha rejected 6 exits ("could not be converted to an
AMO") and the leg went to ERROR, left overnight with no stop and no Edit / Exit. Fixed: BTST legs get no auto-exit,
nothing is sent while the exchange is closed, and a leg in ERROR can be resumed (checked against Zerodha first)."""
from __future__ import annotations

from datetime import datetime

import pytest

from trader import lifecycle as L
from trader.broker import is_working
from trader.config import TraderConfig
from trader.service import ActionError
from trader.strategy import StrategyService

from .fakes import Clock
from .test_service import EXPIRY, Rig


def strategy(tmp_path, order_type="BTST", square_off="15:40", start=datetime(2026, 9, 28, 14, 55), **cfg):
    r = Rig(tmp_path, clock=Clock(start), market_hours_guard=True, square_off_time=None, product="NRML", **cfg)
    r.price(100)
    strat = StrategyService(r.svc, r.repo)
    r.svc.extra_tick = strat.tick
    res = strat.create_and_trade({"config": {"order_type": order_type, "square_off_time": square_off}, "legs": [dict(
        underlying="NIFTY", expiry=EXPIRY.isoformat(), strike=25000, option_type="CE", side="BUY", lots=2,
        entry_price=100, sl_value=10, sl_type="POINTS", tp_value=None, tp_type="POINTS")]})
    assert res["ok"], res
    r.tick()
    return r, strat, res["confirmed"][0], res["strategy_id"]


def working_sl(r, tid):
    return [o for o in r.repo.orders(tid, "SL") if is_working(o["status"])]


def test_btst_leg_gets_no_auto_exit_and_keeps_its_sl_overnight(tmp_path):
    r, strat, tid, sid = strategy(tmp_path)
    t = r.trade(tid)
    assert t["auto_exit_at"] is None and t["status"] == L.POSITION_ACTIVE
    r.clock.now = datetime(2026, 9, 28, 15, 41)
    r.tick(1)
    r.clock.now = datetime(2026, 9, 29, 9, 16)              # next morning
    r.tick(1)
    t = r.trade(tid)
    assert t["status"] == L.POSITION_ACTIVE and not t["pending_exit_reason"]
    assert len(working_sl(r, tid)) == 1 and r.repo.orders(tid, "EXIT") == []


def test_old_btst_leg_with_an_auto_exit_time_is_not_closed(tmp_path):
    r, strat, tid, sid = strategy(tmp_path)
    r.repo.update_trade(tid, auto_exit_at="2026-09-28T15:20:00")   # a leg created before the fix
    r.clock.now = datetime(2026, 9, 28, 15, 21)
    r.tick(1)
    assert not r.trade(tid)["pending_exit_reason"] and r.repo.orders(tid, "EXIT") == []


def test_nothing_is_sent_while_the_market_is_closed(tmp_path):
    r, strat, tid, sid = strategy(tmp_path, order_type="CNC")      # auto-exit 15:40 = after the 15:30 close
    assert r.trade(tid)["auto_exit_at"] == "2026-09-28T15:40:00"
    r.clock.now = datetime(2026, 9, 28, 15, 52)
    for _ in range(5):
        r.tick(15)
    assert r.repo.orders(tid, "EXIT") == []                  # no exit attempts after the close
    assert len(working_sl(r, tid)) == 1                      # and the SL order was never pulled
    r.price(80)                                              # even "through the SL" on a stale price
    r.tick(5)
    assert r.repo.orders(tid, "EXIT") == [] and r.trade(tid)["status"] == L.POSITION_ACTIVE


def test_manual_exit_is_refused_while_closed(tmp_path):
    r, strat, tid, sid = strategy(tmp_path)
    r.clock.now = datetime(2026, 9, 28, 16, 0)
    with pytest.raises(ActionError, match="market is closed"):
        strat.exit_all(sid)
    with pytest.raises(ActionError, match="market is closed"):
        r.svc.limit_exit(tid, 130, 120)
    with pytest.raises(ActionError, match="market is closed"):
        r.svc.prepare_partial_exit(tid, 65)


def leg_in_error(tmp_path):
    """The 2026-10-13 state: SL cancelled for an exit, exits failed, leg in ERROR with the position still open."""
    r, strat, tid, sid = strategy(tmp_path)
    t = r.trade(tid)
    r.repo.set_status(tid, L.ERROR, "EXIT_FAILED_REPEATEDLY", expect=t["status"], level="CRITICAL",
                      error="exit orders keep failing; exit manually in Kite", pending_exit_reason="AUTO_EXIT",
                      exit_attempts=6, auto_exit_at="2026-09-28T15:40:00")
    for o in working_sl(r, tid):
        r.svc.placer.cancel(o, "exit (AUTO_EXIT)")
    r.tick(1)
    assert r.trade(tid)["status"] == L.ERROR and working_sl(r, tid) == []
    return r, strat, tid, sid


def test_resume_puts_an_error_leg_back_under_the_algo_with_its_sl(tmp_path):
    r, strat, tid, sid = leg_in_error(tmp_path)
    r.clock.now = datetime(2026, 9, 29, 9, 20)
    res = r.svc.resume_trade(tid)
    t = r.trade(tid)
    assert res["ok"] and t["status"] == L.POSITION_ACTIVE
    assert not t["pending_exit_reason"] and t["exit_attempts"] == 0 and t["error"] is None and t["auto_exit_at"] is None
    sl = working_sl(r, tid)
    assert len(sl) == 1 and sl[0]["trigger_price"] == 90 and sl[0]["quantity"] == 130
    r.tick(1)
    assert r.trade(tid)["status"] == L.POSITION_ACTIVE and r.repo.orders(tid, "EXIT") == []
    assert "RESUMED_BY_USER" in r.events(tid)


def test_resume_waits_for_the_open_and_refuses_other_states(tmp_path):
    r, strat, tid, sid = leg_in_error(tmp_path)
    r.clock.now = datetime(2026, 9, 28, 20, 0)               # evening: resumed, but nothing sent yet
    r.svc.resume_trade(tid)
    assert r.trade(tid)["status"] == L.POSITION_ACTIVE and working_sl(r, tid) == []
    r.clock.now = datetime(2026, 9, 29, 9, 15)
    r.tick(1)
    assert len(working_sl(r, tid)) == 1
    with pytest.raises(ActionError, match="only a leg in ERROR"):
        r.svc.resume_trade(tid)


def test_market_hours_guard_defaults():
    assert TraderConfig.from_env(None, {"TRADER_MODE": "LIVE"}).market_hours_guard is True
    assert TraderConfig.from_env(None, {}).market_hours_guard is False
    assert TraderConfig.from_env(None, {"TRADER_MODE": "LIVE", "TRADER_MARKET_HOURS_GUARD": "false"}).market_hours_guard is False
    c = TraderConfig(market_hours_guard=True)
    assert c.market_open("NIFTY", datetime(2026, 9, 28, 15, 29)) and not c.market_open("NIFTY", datetime(2026, 9, 28, 15, 30))
    assert not c.market_open("NIFTY", datetime(2026, 10, 3, 11, 0))                 # Saturday
    assert c.market_open("CRUDEOIL", datetime(2026, 9, 28, 22, 0))


def test_resume_takes_your_sl_and_target_and_refuses_one_through_the_price(tmp_path):
    r, strat, tid, sid = leg_in_error(tmp_path)
    r.clock.now = datetime(2026, 9, 29, 9, 20)
    r.price(85)                                              # opened below yesterday's SL 90
    with pytest.raises(ActionError, match="through the live price 85"):
        r.svc.resume_trade(tid)                              # keeping SL 90 would exit at once
    with pytest.raises(ActionError, match="already reached"):
        r.svc.resume_trade(tid, stop_loss=70, target=80)
    assert r.trade(tid)["status"] == L.ERROR                 # nothing changed
    r.svc.resume_trade(tid, stop_loss=70.03, target=120)
    t = r.trade(tid)
    assert t["status"] == L.POSITION_ACTIVE and t["current_sl"] == 70 and t["target"] == 120 and t["user_sl"] == 70
    sl = working_sl(r, tid)
    assert len(sl) == 1 and sl[0]["trigger_price"] == 70
    r.tick(1)
    assert r.trade(tid)["status"] == L.POSITION_ACTIVE and r.repo.orders(tid, "EXIT") == []


def test_resume_replaces_an_old_sl_order_that_already_triggered(tmp_path):
    r, strat, tid, sid = strategy(tmp_path)                  # SL order working at trigger 90
    t = r.trade(tid)
    r.repo.set_status(tid, L.ERROR, "EXIT_FAILED_REPEATEDLY", expect=t["status"], level="CRITICAL",
                      error="exit orders keep failing", pending_exit_reason="AUTO_EXIT", exit_attempts=6)
    r.clock.now = datetime(2026, 9, 28, 15, 0)              # same day: the old SL order is still working
    r.ex.auto_match = False                                  # it triggers but its limit doesn't fill
    r.price(85)                                              # through the old trigger
    r.tick(1)
    r.ex.auto_match = True
    r.svc.resume_trade(tid, stop_loss=70)
    for _ in range(3):
        r.tick(1)
    t = r.trade(tid)
    assert t["status"] == L.POSITION_ACTIVE and t["current_sl"] == 70
    old, new = r.repo.orders(tid, "SL")[0], working_sl(r, tid)
    assert old["status"] == "CANCELLED" and len(new) == 1 and new[0]["trigger_price"] == 70
