"""A position already open in Kite in the same contract (not opened by this app) only WARNS: Trade all places
nothing and asks "still execute?"; on Execute (ack_warnings) the order goes in. Never refused by the app itself."""
from __future__ import annotations

from trader import lifecycle as L
from trader.strategy import StrategyService

from .fakes import EXPIRY
from .test_service import SYM, Rig


def test_trade_all_asks_then_executes(tmp_path):
    r = Rig(tmp_path)
    r.price(100)
    r.ex.manual_order("NFO", SYM, "MIS", "BUY", 195, 100)
    strat = StrategyService(r.svc, r.repo)
    body = {"config": {"order_type": "MIS"}, "legs": [dict(
        underlying="NIFTY", expiry=EXPIRY.isoformat(), strike=25000, option_type="CE", side="BUY",
        lots=1, entry_price=100)]}
    res = strat.create_and_trade(body)
    assert not res["ok"] and res["needs_ack"] and not res["errors"]
    assert res["warnings"] == [f"Zerodha already shows 195 of {SYM} not opened here"]
    assert len(r.places()) == 1 and not r.repo.trades(L.OPEN_STATUSES)          # nothing placed, nothing open
    res = strat.create_and_trade({**body, "ack_warnings": True})
    assert res["ok"] and len(res["confirmed"]) == 1, res
    r.tick()
    r.tick()
    t = r.trade(res["confirmed"][0])
    assert (t["status"], t["open_qty"], t["outside_base"]) == (L.POSITION_ACTIVE, 65, 195)
