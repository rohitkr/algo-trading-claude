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
