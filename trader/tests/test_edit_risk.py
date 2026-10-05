"""Edits vs TRADER_MAX_LOSS_PER_TRADE (regression for 2026-10-05: raising a pending strategy leg's entry 77.65 -> 80
kept the app's automatic stop 48.4, so the edit showed 'max_loss_per_trade: ₹10,270 at the stop' and was refused).

The limit gates new trades and edits that RAISE the loss at the stop beyond it; an edit that keeps or lowers that
loss is never blocked; an automatic stop follows the entry; a refusal names the stop that would fit."""
from __future__ import annotations

from trader import lifecycle as L
from trader.strategy import StrategyService

from .fakes import EXPIRY
from .test_service import SYM, Rig

LIMIT = 10_000


def pending_leg_without_sl(tmp_path):
    r = Rig(tmp_path, auto=False, max_loss_per_trade=LIMIT)
    r.price(77.65)
    strat = StrategyService(r.svc, r.repo)
    res = strat.create_and_trade({"config": {"order_type": "MIS"}, "legs": [dict(
        underlying="NIFTY", expiry=EXPIRY.isoformat(), strike=25000, option_type="CE", side="BUY",
        lots=5, entry_price=77.65)]})
    assert res["ok"], res
    tid = res["confirmed"][0]
    assert r.trade(tid)["status"] in (L.ENTRY_ORDER_PLACED, L.ENTRY_PENDING)
    return r, tid


def loss(t) -> float:
    return (t["entry_price"] - t["current_sl"]) * t["quantity"]


def test_raising_a_pending_entry_moves_the_automatic_stop(tmp_path):
    r, tid = pending_leg_without_sl(tmp_path)
    t = r.trade(tid)
    assert t["sl_auto"] == 1 and loss(t) <= LIMIT
    # exactly what the edit dialog sends: new entry, the unchanged (automatic) stop
    plan = r.svc.prepare_edit(tid, {"entry_price": 80, "lots": 5, "stop_loss": t["current_sl"], "target": ""})
    assert plan["ok"], plan
    r.svc.apply_edit(tid, plan["token"])
    t = r.trade(tid)
    assert t["entry_price"] == 80 and t["current_sl"] > 48.4 and loss(t) <= LIMIT
    assert t["sl_auto"] == 1


def test_a_stop_the_user_sets_is_kept_and_no_longer_automatic(tmp_path):
    r, tid = pending_leg_without_sl(tmp_path)
    plan = r.svc.prepare_edit(tid, {"stop_loss": 60})
    assert plan["ok"], plan
    r.svc.apply_edit(tid, plan["token"])
    assert r.trade(tid)["current_sl"] == 60 and r.trade(tid)["sl_auto"] == 0


def test_raising_risk_past_the_limit_is_refused_with_the_stop_that_fits(tmp_path):
    r = Rig(tmp_path, max_loss_per_trade=LIMIT)
    r.price(100)
    tid = r.open_trade(lots=5, entry_price=100, stop_loss=90, target=130)    # BUY 5 lots
    r.tick()
    t = r.trade(tid)
    qty = t["quantity"]
    wide = round(100 - LIMIT / qty - 10)
    plan = r.svc.prepare_edit(tid, {"stop_loss": wide})
    assert not plan["ok"]
    msg = " ".join(plan["errors"])
    assert "max_loss_per_trade" in msg and "use a stop-loss at or above" in msg and "fewer lots" in msg, msg


def test_tightening_a_position_already_over_the_limit_is_allowed(tmp_path):
    r = Rig(tmp_path, max_loss_per_trade=LIMIT)
    r.price(100)
    tid = r.open_trade(lots=5, entry_price=100, stop_loss=90, target=130)
    r.tick()
    assert r.trade(tid)["status"] == L.POSITION_ACTIVE
    import dataclasses
    r.svc.cfg = dataclasses.replace(r.svc.cfg, max_loss_per_trade=100)   # limit lowered after the trade was taken
    t = r.trade(tid)
    for sl in (92, 95):                                        # still over the new limit, but less risk each time
        plan = r.svc.prepare_edit(tid, {"stop_loss": sl})
        assert plan["ok"], plan
        r.svc.apply_edit(tid, plan["token"])
    plan = r.svc.prepare_edit(tid, {"stop_loss": 93})          # loosening again: more risk, over the limit
    assert not plan["ok"] and any("max_loss_per_trade" in e for e in plan["errors"])


def test_new_trades_still_respect_the_limit(tmp_path):
    r = Rig(tmp_path, max_loss_per_trade=500)
    p = r.svc.preview(dict(underlying="NIFTY", expiry=EXPIRY.isoformat(), strike=25000, option_type="CE",
                           side="BUY", lots=5, entry_price=100, stop_loss=50, target=130))
    assert not p["ok"] and any(c["name"] == "max_loss_per_trade" and not c["passed"] for c in p["risk"])


def test_limits_set_to_zero_are_off(tmp_path):
    r = Rig(tmp_path, max_open_trades=0, max_trades_per_day=0, max_lots_per_trade=0, max_qty_per_trade=0,
            max_order_value=0, max_loss_per_trade=0, max_daily_loss=0, max_entry_deviation_pct=0)
    r.price(100)
    p = r.svc.preview(dict(underlying="NIFTY", expiry=EXPIRY.isoformat(), strike=25000, option_type="CE",
                           side="BUY", lots=20, entry_price=100, stop_loss=1, target=300))
    names = {c["name"] for c in p["risk"]}
    assert p["ok"], p
    assert not names & {"max_open_trades", "max_trades_per_day", "max_lots_per_trade", "max_qty_per_trade",
                        "max_order_value", "max_loss_per_trade", "max_daily_loss", "entry_near_ltp"}
    r.svc.confirm(p["trade_id"], p["token"])
    r.tick()
    plan = r.svc.prepare_edit(p["trade_id"], {"stop_loss": 0.05})          # any stop, no loss limit
    assert plan["ok"], plan
