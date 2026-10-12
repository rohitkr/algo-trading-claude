"""Exit at a limit price (2026-10-12): a resting order at your price, never re-priced; the SL order makes room first
(never SL + limit both filling); the software stop still protects while the limit waits."""
from __future__ import annotations

import pytest

from trader import lifecycle as L
from trader.service import ActionError

from .test_service import Rig


def active(tmp_path, lots=2):
    r = Rig(tmp_path)
    r.price(100)
    tid = r.open_trade(lots=lots, entry_price=100, stop_loss=90, target=None)
    r.tick()
    assert r.trade(tid)["status"] == L.POSITION_ACTIVE
    r.ex.auto_match = False                                  # the limit rests until we let it fill
    return r, tid


def working(r, tid, kind):
    return [o for o in r.repo.orders(tid, kind) if o["status"] not in ("COMPLETE", "CANCELLED", "REJECTED")]


def test_full_limit_exit_rests_at_your_price_and_closes_with_its_reason(tmp_path):
    r, tid = active(tmp_path)
    r.svc.limit_exit(tid, 130, 120.03)
    for _ in range(3):
        r.tick(); r.clock.advance(30)                        # long enough that a normal exit would be re-priced
    le = working(r, tid, "PARTIAL")
    assert len(le) == 1 and le[0]["price"] == 120.05 and le[0]["quantity"] == 130 and le[0]["purpose"] == "LIMIT_EXIT"
    assert working(r, tid, "SL") == []                       # SL cancelled: the limit covers everything
    assert len(r.repo.orders(tid, "SL")) == 1                # and never re-placed while the limit was going in
    assert r.svc.trade_view(tid)["limit_exit"]["price"] == 120.05
    r.ex.auto_match = True
    r.price(121)
    for _ in range(3):
        r.tick()
    t = r.trade(tid)
    assert t["status"] == L.EXITED and t["exit_reason"] == "LIMIT_EXIT" and t["exit_avg_price"] >= 120.05


def test_partial_limit_exit_keeps_the_sl_on_the_rest(tmp_path):
    r, tid = active(tmp_path, lots=3)                        # 195 open
    r.svc.limit_exit(tid, 65, 115)
    r.tick()
    assert working(r, tid, "PARTIAL")[0]["quantity"] == 65
    assert working(r, tid, "SL")[0]["quantity"] == 130       # SL shrank first
    assert len(r.repo.orders(tid, "SL")) == 1


def test_sl_still_protects_while_the_limit_waits(tmp_path):
    r, tid = active(tmp_path)
    r.svc.limit_exit(tid, 130, 130)
    r.tick()
    r.ex.auto_match = True
    r.price(85)                                              # through the SL 90
    for _ in range(5):
        r.tick(); r.clock.advance(5)
    t = r.trade(tid)
    assert t["status"] == L.EXITED and t["exit_reason"] != "LIMIT_EXIT"
    assert all(o["status"] == "CANCELLED" for o in r.repo.orders(tid, "PARTIAL"))   # the limit was pulled


def test_limit_exit_can_be_repriced_by_you_or_cancelled(tmp_path):
    r, tid = active(tmp_path)
    r.svc.limit_exit(tid, 130, 120)
    r.tick()
    r.svc.update_limit_exit(tid, "price", 118)
    assert working(r, tid, "PARTIAL")[0]["price"] == 118
    r.svc.update_limit_exit(tid, "cancel")
    for _ in range(3):
        r.tick()
    assert working(r, tid, "PARTIAL") == [] and working(r, tid, "SL")[0]["quantity"] == 130   # SL back on all
    assert r.svc.trade_view(tid)["limit_exit"] is None


def test_limit_exit_checks(tmp_path):
    r, tid = active(tmp_path)
    with pytest.raises(ActionError, match="lots"):
        r.svc.limit_exit(tid, 50, 120)
    with pytest.raises(ActionError, match="open"):
        r.svc.limit_exit(tid, 195, 120)
    r.svc.limit_exit(tid, 65, 120)
    with pytest.raises(ActionError, match="already waiting"):
        r.svc.limit_exit(tid, 65, 121)
