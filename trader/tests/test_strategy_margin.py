"""Margin needed for a strategy before placing it (2026-10-06): Kite basket margins, display only."""
from __future__ import annotations

from trader.strategy import StrategyService

from .fakes import EXPIRY
from .test_service import Rig

LEG = dict(underlying="NIFTY", expiry=EXPIRY.isoformat(), strike=25000, option_type="CE", side="SELL", lots=2,
           entry_price=100)


def test_margin_sends_the_legs_as_one_basket_and_returns_the_hedged_total(tmp_path):
    r = Rig(tmp_path)
    seen = []
    r.svc.margin_fn = lambda orders: seen.append(orders) or {
        "initial": {"total": 300000.0}, "final": {"total": 120000.5},
        "orders": [{"charges": {"total": 61.2}}, {"charges": {"total": 40.1}}]}
    res = StrategyService(r.svc, r.repo).margin({"config": {"order_type": "MIS"},
                                                 "legs": [LEG, {**LEG, "side": "BUY", "strike": 25100}]})
    assert res["margin"] == 120000.5 and res["charges"] == 101.3
    o = seen[0][0]
    assert (o["transaction_type"], o["quantity"], o["product"], o["exchange"]) == ("SELL", 130, "MIS", "NFO")


def test_no_kite_means_no_margin_not_an_error(tmp_path):
    r = Rig(tmp_path)
    res = StrategyService(r.svc, r.repo).margin({"legs": [LEG]})
    assert res["margin"] is None and "Kite" in res["error"]
