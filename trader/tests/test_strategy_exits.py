"""Combined max loss / max profit of a strategy (2026-10-05): optional at creation, changeable while it runs;
reaching either squares off every open leg."""
from __future__ import annotations

import pytest

from trader import lifecycle as L
from trader.service import ActionError
from trader.strategy import GlobalConfig

from .test_leg_add import one_leg


def test_blank_or_zero_means_off():
    c = GlobalConfig.from_json({"exit_loss_amount": "", "exit_profit_amount": 0})
    assert c.exit_loss_amount is None and c.exit_profit_amount is None


def test_max_profit_set_after_entry_squares_off_every_leg(tmp_path):
    r, strat, tid, sid = one_leg(tmp_path)                 # BUY 2 lots @ 100, own target 130
    r.price(105)                                           # +5 x 130 = +650
    r.tick()
    res = strat.set_exits(sid, {"exit_profit_amount": 500, "exit_loss_amount": 2000})
    assert res["ok"] and res["strategy"]["config"]["exit_profit_amount"] == 500
    for _ in range(4):
        r.tick()
    assert r.trade(tid)["status"] == L.EXITED and r.trade(tid)["exit_reason"] == "STRATEGY_PROFIT_TARGET"
    assert r.repo.strategy(sid)["status"] == "DONE"


def test_max_loss_can_be_removed_while_running(tmp_path):
    r, strat, tid, sid = one_leg(tmp_path, exit_loss_amount=300)
    strat.set_exits(sid, {"exit_loss_amount": "", "exit_profit_amount": None})
    r.price(97)                                            # -3 x 130 = -390: would have hit the old ₹300
    for _ in range(3):
        r.tick()
    assert r.trade(tid)["status"] == L.POSITION_ACTIVE


def test_finished_strategy_cannot_be_changed(tmp_path):
    r, strat, tid, sid = one_leg(tmp_path)
    strat.exit_all(sid)
    with pytest.raises(ActionError):
        strat.set_exits(sid, {"exit_loss_amount": 100})


def test_combined_sl_can_be_negative_then_trailed_into_profit(tmp_path):
    r, strat, tid, sid = one_leg(tmp_path)                 # BUY 2 lots (130) @ 100
    strat.set_exits(sid, {"exit_sl_pnl": -2000})           # a ₹2,000 loss
    r.price(110)                                           # +10 x 130 = +1,300
    for _ in range(2):
        r.tick()
    assert r.trade(tid)["status"] == L.POSITION_ACTIVE
    strat.set_exits(sid, {"exit_sl_pnl": 650})             # trail: lock ₹650 of profit
    r.price(106)                                           # +780: still above the lock
    r.tick()
    assert r.trade(tid)["status"] == L.POSITION_ACTIVE
    r.price(104)                                           # +520: below the lock -> square off
    for _ in range(4):
        r.tick()
    assert r.trade(tid)["status"] == L.EXITED and r.trade(tid)["exit_reason"] == "STRATEGY_PROFIT_LOCKED"


def test_negative_combined_sl_exits_at_that_loss(tmp_path):
    r, strat, tid, sid = one_leg(tmp_path)
    strat.set_exits(sid, {"exit_sl_pnl": -500})
    r.price(97)                                            # -390: not yet
    r.tick()
    assert r.trade(tid)["status"] == L.POSITION_ACTIVE
    r.price(95)                                            # -650
    for _ in range(4):
        r.tick()
    assert r.trade(tid)["exit_reason"] == "STRATEGY_LOSS_LIMIT"


def test_combined_exit_is_placed_in_the_same_tick_it_triggers(tmp_path):
    # 2026-10-07: trigger -> SL cancel -> exit took one 3 s tick per step (7-10 s in all), giving back profit
    r, strat, tid, sid = one_leg(tmp_path)
    strat.set_exits(sid, {"exit_sl_pnl": -300})
    r.price(97)                                            # -390 at the stop
    r.tick()                                               # ONE tick: trigger, cancel the SL, place + fill the exit
    t = r.trade(tid)
    assert t["exit_reason"] == "STRATEGY_LOSS_LIMIT" and t["status"] == L.EXITED, (t["status"], r.events(tid)[-8:])


def test_monitor_ticks_fast_while_an_exit_is_in_progress():
    from trader.monitor import Monitor

    class Svc:
        def __init__(self):
            self.n = 0

        def tick(self):
            self.n += 1

        def exiting(self):
            return True

    svc = Svc()
    m = Monitor(svc, interval_s=5, fast_s=0.05)
    m.start()
    import time
    time.sleep(0.4)
    m.stop()
    assert svc.n >= 4                                      # 5 s cadence would have ticked once
