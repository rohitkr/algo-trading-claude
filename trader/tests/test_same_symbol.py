"""Adding lots to a symbol already held (2026-10-05): a second trade on the same symbol and side is allowed, each
trade keeps its own SL/target, and reconciliation shares Zerodha's one net position between them."""
from __future__ import annotations

from trader import lifecycle as L

from .test_service import SYM, Rig


def two_trades(tmp_path):
    r = Rig(tmp_path)
    r.price(100)
    a = r.open_trade(lots=2, entry_price=100, stop_loss=90, target=130)
    r.tick()
    b = r.open_trade(lots=1, entry_price=100, stop_loss=95, target=120)
    for _ in range(4):
        r.tick()
    return r, a, b


def test_second_trade_on_same_symbol_is_allowed_and_both_stay_managed(tmp_path):
    r, a, b = two_trades(tmp_path)
    assert r.trade(a)["status"] == r.trade(b)["status"] == L.POSITION_ACTIVE
    assert r.trade(a)["mismatch_count"] == r.trade(b)["mismatch_count"] == 0


def test_each_trade_keeps_its_own_stop(tmp_path):
    r, a, b = two_trades(tmp_path)
    r.price(94)                                    # through b's stop (95), not a's (90)
    for _ in range(4):
        r.tick()
    assert r.trade(b)["status"] == L.EXITED
    assert r.trade(a)["status"] == L.POSITION_ACTIVE and r.trade(a)["open_qty"] == 130


def test_quantity_closed_in_kite_comes_off_the_newest_trade(tmp_path):
    r, a, b = two_trades(tmp_path)
    r.ex.set_position("NFO", SYM, "MIS", 130)     # 65 sold by hand in Kite
    r.tick()
    r.tick()
    assert r.trade(a)["status"] == L.POSITION_ACTIVE and r.trade(a)["open_qty"] == 130
    assert r.trade(b)["status"] == L.MANUALLY_EXITED


def test_opposite_side_on_same_symbol_is_refused(tmp_path):
    r = Rig(tmp_path)
    r.price(100)
    r.open_trade(lots=1, entry_price=100, stop_loss=90, target=130)
    r.tick()
    p = r.svc.preview(dict(underlying="NIFTY", expiry=r.trade(1)["expiry"], strike=25000, option_type="CE",
                           side="SELL", lots=1, entry_price=100, stop_loss=110, target=80))
    assert not p["ok"] and any(c["name"] == "no_opposite_trade_in_symbol" and not c["passed"] for c in p["risk"])


def test_adding_can_be_switched_off_in_env(tmp_path):
    r = Rig(tmp_path, allow_add_to_symbol=False)
    r.price(100)
    r.open_trade(lots=1, entry_price=100, stop_loss=90, target=130)
    r.tick()
    p = r.svc.preview(dict(underlying="NIFTY", expiry=r.trade(1)["expiry"], strike=25000, option_type="CE",
                           side="BUY", lots=1, entry_price=100, stop_loss=90, target=130))
    assert not p["ok"] and any(c["name"] == "one_trade_per_symbol" and not c["passed"] for c in p["risk"])
