"""Adopt a position opened in Kite (not by this app), and manual exits in Kite of adopted AND normal trades:
noticed at once from the order book, our SL taken off, and never a second (double) exit from the app."""
from __future__ import annotations

import pytest

from trader import lifecycle as L
from trader.service import MANUAL_ORDER_HOLD_S, ActionError
from trader.strategy import StrategyService

from .fakes import EXPIRY
from .test_service import SYM, Rig, _active

KEY = f"NFO|{SYM}|MIS"


def sent(r):
    """Orders this app sent (tagged), not the ones placed by hand in Kite."""
    return [c for c in r.ex.calls if c[0] == "place" and c[7]]


def _kite_buy(r, qty, price=100.0):
    r.price(price)
    r.ex.manual_order("NFO", SYM, "MIS", "BUY", qty, price)        # opened by hand in Kite
    r.tick()


def _set_sl(r, tid, sl, target=None):
    p = r.svc.prepare_edit(tid, {"stop_loss": sl, **({"target": target} if target else {})})
    assert p["ok"], p
    r.svc.apply_edit(tid, p["token"])
    r.tick()


def test_outside_position_is_listed_then_adopted_whole(tmp_path):
    r = Rig(tmp_path)
    _kite_buy(r, 130, 100)
    rows = r.svc.outside_positions()
    assert len(rows) == 1
    row = rows[0]
    assert (row["tradingsymbol"], row["qty"], row["side"], row["lots"], row["avg_price"], row["adoptable"]) == \
        (SYM, 130, "BUY", 2, 100.0, True)
    n = len(sent(r))
    res = r.svc.adopt_position("NFO", SYM, "MIS")
    t = r.trade(res["trade_id"])
    assert (t["status"], t["side"], t["quantity"], t["open_qty"], t["entry_avg_price"], t["outside_base"]) == \
        (L.POSITION_ACTIVE, "BUY", 130, 130, 100.0, 0)
    assert t["current_sl"] is None and len(sent(r)) == n          # nothing sent, no SL until one is set
    assert r.svc.outside_positions() == []
    assert "POSITION_ADOPTED" in r.events(t["id"])
    _set_sl(r, t["id"], 90, target=130)
    sl = r.repo.orders(t["id"], "SL")[0]
    assert (sl["quantity"], sl["trigger_price"], sl["status"]) == (130, 90, "TRIGGER PENDING")
    r.price(130)                                                      # target: the app exits it like its own
    r.tick()
    r.tick()
    t = r.trade(t["id"])
    assert t["status"] == L.EXITED and t["exit_reason"] == L.TARGET_HIT and r.ex.net[KEY]["qty"] == 0


def test_adopted_then_exited_by_hand_in_kite_is_noticed_at_once(tmp_path):
    r = Rig(tmp_path)
    _kite_buy(r, 130, 100)
    tid = r.svc.adopt_position("NFO", SYM, "MIS")["trade_id"]
    _set_sl(r, tid, 90)
    n = len(sent(r))
    r.clock.advance(5)
    r.ex.manual_order("NFO", SYM, "MIS", "SELL", 130, 104)        # exit by hand in Kite
    r.price(130)
    r.tick()                                                       # ONE tick, no waiting for confirmations
    t = r.trade(tid)
    assert (t["status"], t["exit_reason"], t["exit_avg_price"]) == (L.MANUALLY_EXITED, L.MANUAL_EXIT, 104)
    assert t["realized_pnl"] == pytest.approx(130 * 4)
    assert r.repo.orders(tid, "SL")[0]["status"] == "CANCELLED"     # our SL is off: it can't open a short
    assert len(sent(r)) == n                                     # no exit order from the app
    assert r.ex.net[KEY]["qty"] == 0
    r.tick()
    r.restart()
    r.tick()
    assert not [c for c in r.ex.calls if c[0] == 'place' and c[7]] and r.ex.net[KEY]["qty"] == 0


def test_normal_trade_exited_by_hand_with_a_kite_order_is_noticed_at_once(tmp_path):
    r = Rig(tmp_path, reconcile_confirmations=3)
    tid = _active(r, lots=2)
    n = len(sent(r))
    r.clock.advance(5)
    r.ex.manual_order("NFO", SYM, "MIS", "SELL", 130, 102)
    r.tick()
    t = r.trade(tid)
    assert t["status"] == L.MANUALLY_EXITED and t["exit_avg_price"] == 102
    assert r.repo.orders(tid, "SL")[0]["status"] == "CANCELLED" and len(sent(r)) == n


def test_manual_partial_exit_by_kite_order_resizes_sl_at_once(tmp_path):
    r = Rig(tmp_path)
    tid = _active(r, lots=3)
    r.clock.advance(5)
    r.ex.manual_order("NFO", SYM, "MIS", "SELL", 65, 105)
    r.tick()
    t = r.trade(tid)
    assert (t["status"], t["open_qty"], t["outside_qty"], t["outside_pnl"]) == (L.POSITION_ACTIVE, 130, 65, 65 * 5)
    assert r.repo.orders(tid, "SL")[0]["quantity"] == 130
    r.tick()
    assert r.trade(tid)["open_qty"] == 130                         # the same Kite order is not counted twice


@pytest.mark.parametrize("local", [False, True])
def test_exit_by_hand_while_positions_lag_never_double_exits(tmp_path, local):
    """Kite's order book shows the hand exit before its net position does. Meanwhile the stop is hit: the app must
    send nothing (it would sell a second time and leave a short), then close the trade once the position shows it."""
    r = Rig(tmp_path, sl_local_default=local)
    _kite_buy(r, 65, 100)
    tid = r.svc.adopt_position("NFO", SYM, "MIS")["trade_id"]
    _set_sl(r, tid, 90)
    n = len(sent(r))
    r.clock.advance(5)
    r.ex.manual_order("NFO", SYM, "MIS", "SELL", 65, 91)
    r.ex.net[KEY]["qty"] = 65                                       # position not updated yet (lag)
    r.price(85)                                                     # through the stop
    r.tick(1)
    r.tick(1)
    assert r.trade(tid)["status"] == L.POSITION_ACTIVE and "MANUAL_ORDER_SEEN" in r.events(tid)
    assert len(sent(r)) == n                                     # no exit while the hand exit settles
    r.ex.net[KEY]["qty"] = 0                                        # the position catches up
    r.tick(1)
    t = r.trade(tid)
    assert t["status"] == L.MANUALLY_EXITED and len(sent(r)) == n
    assert all(o["status"] in ("CANCELLED",) for o in r.repo.orders(tid, "SL"))


def test_kite_order_that_never_moves_this_trade_stops_holding_it(tmp_path):
    r = Rig(tmp_path)
    tid = _active(r)
    r.clock.advance(5)
    r.ex.manual_order("NFO", SYM, "MIS", "SELL", 65, 100)
    r.ex.net[KEY]["qty"] = 65                                       # Zerodha's position never changes
    r.tick(1)
    assert "MANUAL_ORDER_SEEN" in r.events(tid)
    r.tick(MANUAL_ORDER_HOLD_S)
    assert "MANUAL_ORDER_NOT_THIS_TRADE" in r.events(tid)
    r.price(130)                                                    # managed again: the target exits it
    r.tick()
    r.tick()
    assert r.trade(tid)["status"] == L.EXITED


def test_kite_orders_before_the_trade_are_not_its_exits(tmp_path):
    r = Rig(tmp_path)
    r.price(100)
    r.ex.manual_order("NFO", SYM, "MIS", "BUY", 65, 100)
    r.ex.manual_order("NFO", SYM, "MIS", "SELL", 65, 101)           # an earlier round trip in Kite, today
    r.clock.advance(5)
    tid = _active(r)
    r.tick()
    assert r.trade(tid)["status"] == L.POSITION_ACTIVE
    assert "MANUAL_ORDER_SEEN" not in r.events(tid)


def test_adopt_part_keeps_the_rest_outside_and_kite_closes_the_rest_first(tmp_path):
    r = Rig(tmp_path)
    _kite_buy(r, 195, 100)
    tid = r.svc.adopt_position("NFO", SYM, "MIS", lots=1)["trade_id"]
    t = r.trade(tid)
    assert (t["status"], t["open_qty"], t["outside_base"]) == (L.POSITION_ACTIVE, 65, 130)
    rows = r.svc.outside_positions()
    assert [(x["qty"], x["lots"]) for x in rows] == [(130, 2)]
    _set_sl(r, tid, 90)
    assert r.repo.orders(tid, "SL")[0]["quantity"] == 65
    r.clock.advance(5)
    r.ex.manual_order("NFO", SYM, "MIS", "SELL", 65, 103)            # sells part of what was NOT adopted
    r.tick()
    t = r.trade(tid)
    assert (t["status"], t["open_qty"], t["outside_base"]) == (L.POSITION_ACTIVE, 65, 65)
    assert "OUTSIDE_POSITION_REDUCED" in r.events(tid) and r.repo.orders(tid, "SL")[0]["quantity"] == 65
    r.clock.advance(5)
    r.ex.manual_order("NFO", SYM, "MIS", "SELL", 65, 103)
    r.tick()
    t = r.trade(tid)
    assert (t["status"], t["open_qty"], t["outside_base"]) == (L.POSITION_ACTIVE, 65, 0)
    assert r.svc.outside_positions() == []
    r.clock.advance(5)
    r.ex.manual_order("NFO", SYM, "MIS", "SELL", 65, 95)             # now the adopted lot itself
    r.tick()
    t = r.trade(tid)
    assert t["status"] == L.MANUALLY_EXITED and r.repo.orders(tid, "SL")[0]["status"] == "CANCELLED"
    assert r.ex.net[KEY]["qty"] == 0


def test_adopt_rest_later_and_beside_an_app_trade(tmp_path):
    r = Rig(tmp_path)
    _kite_buy(r, 130, 100)
    a = r.svc.adopt_position("NFO", SYM, "MIS", lots=1)["trade_id"]
    b = r.svc.adopt_position("NFO", SYM, "MIS")["trade_id"]            # the other lot
    r.tick()
    ta, tb = r.trade(a), r.trade(b)
    assert (ta["outside_base"], tb["outside_base"], ta["status"], tb["status"]) == (0, 0, L.POSITION_ACTIVE, L.POSITION_ACTIVE)
    assert ta["mismatch_count"] == tb["mismatch_count"] == 0 and r.svc.outside_positions() == []
    with pytest.raises(ActionError, match="no .* position"):
        r.svc.adopt_position("NFO", SYM, "MIS")


def test_extra_lots_added_by_hand_after_adopt_follow_outside_rule(tmp_path):
    r = Rig(tmp_path)
    _kite_buy(r, 65, 100)
    tid = r.svc.adopt_position("NFO", SYM, "MIS")["trade_id"]
    _set_sl(r, tid, 90)
    n = len(sent(r))
    r.clock.advance(5)
    r.ex.manual_order("NFO", SYM, "MIS", "BUY", 65, 101)            # more bought by hand: a larger position
    r.tick()
    r.tick()
    assert r.trade(tid)["status"] == L.UNKNOWN                       # as before for any trade: stop, resolve in Kite
    r.price(85)
    r.tick()
    assert len(sent(r)) == n
    rows = r.svc.outside_positions()
    assert [x["qty"] for x in rows] == [65]                           # and the extra lot can be adopted on its own


def test_adopt_short_with_local_sl(tmp_path):
    r = Rig(tmp_path)
    r.price(100)
    r.ex.manual_order("NFO", SYM, "MIS", "SELL", 65, 100)
    r.tick()
    tid = r.svc.adopt_position("NFO", SYM, "MIS", sl_local=True)["trade_id"]
    t = r.trade(tid)
    assert (t["side"], t["sl_local"], t["order_type"]) == ("SELL", 1, "MIS")
    _set_sl(r, tid, 110)
    assert r.repo.orders(tid, "SL") == []                             # local: no SL order at Zerodha
    r.price(111)
    r.tick()
    r.tick()
    t = r.trade(tid)
    assert t["status"] == L.EXITED and t["exit_reason"] == L.STOP_LOSS_HIT and r.ex.net[f"NFO|{SYM}|MIS"]["qty"] == 0


def test_nrml_adopt_is_carried_not_squared_off(tmp_path):
    r = Rig(tmp_path)
    r.price(100)
    r.ex.manual_order("NFO", SYM, "NRML", "BUY", 65, 100)
    r.tick()
    tid = r.svc.adopt_position("NFO", SYM, "NRML")["trade_id"]
    assert r.trade(tid)["order_type"] == "BTST"


def test_adopt_refusals(tmp_path):
    r = Rig(tmp_path)
    with pytest.raises(ActionError, match="no .* position"):
        r.svc.adopt_position("NFO", SYM, "MIS")
    _kite_buy(r, 65, 100)
    with pytest.raises(ActionError, match="lots must be 1 to 1"):
        r.svc.adopt_position("NFO", SYM, "MIS", lots=2)
    with pytest.raises(ActionError, match="only F&O"):
        r.svc.adopt_position("NSE", "INFY", "CNC")
    r.ex.manual_order("NFO", "NIFTY26SEPFUT", "NRML", "BUY", 65, 25000)
    r.ex.manual_order("NSE", "INFY", "CNC", "BUY", 10, 1500)
    r.tick()
    rows = {x["tradingsymbol"]: x for x in r.svc.outside_positions()}
    assert set(rows) == {SYM, "NIFTY26SEPFUT"}                           # cash (NSE) is never listed
    assert not rows["NIFTY26SEPFUT"]["adoptable"] and "only options" in rows["NIFTY26SEPFUT"]["reason"]
    with pytest.raises(ActionError, match="only options"):
        r.svc.adopt_position("NFO", "NIFTY26SEPFUT", "NRML")
    assert not [t for t in r.repo.trades() if t["tradingsymbol"] == "NIFTY26SEPFUT"]


def test_strategy_adopt_makes_a_one_leg_strategy(tmp_path):
    r = Rig(tmp_path)
    _kite_buy(r, 130, 100)
    st = StrategyService(r.svc, r.repo)
    r.svc.extra_tick = st.tick
    bad = st.adopt({"exchange": "NFO", "tradingsymbol": SYM, "product": "MIS", "lots": 5})
    assert not bad["ok"] and "lots must be" in bad["errors"][0] and r.repo.strategies() == []
    res = st.adopt({"exchange": "NFO", "tradingsymbol": SYM, "product": "MIS", "sl_local": True})
    assert res["ok"], res
    v = st.view(res["strategy_id"])
    assert v["name"] == "Adopted NIFTY 25000 CE" and v["config"]["sl_local"] is True and v["open_legs"] == 1
    assert v["legs"][0]["id"] == res["trade_id"] and v["legs"][0]["sl_local"] == 1
    assert v["config"]["exit_loss_amount"] is None and v["config"]["exit_profit_amount"] is None
    r.tick()
    assert r.repo.strategy(res["strategy_id"])["status"] == "ACTIVE"
    r.clock.advance(5)
    r.ex.manual_order("NFO", SYM, "MIS", "SELL", 130, 100)
    r.tick()
    r.tick()
    assert r.repo.strategy(res["strategy_id"])["status"] == "DONE"


def test_adopted_trade_survives_a_restart(tmp_path):
    r = Rig(tmp_path)
    _kite_buy(r, 130, 100)
    tid = r.svc.adopt_position("NFO", SYM, "MIS", lots=1)["trade_id"]
    _set_sl(r, tid, 90)
    r.restart()
    r.tick()
    r.tick()
    t = r.trade(tid)
    assert (t["status"], t["open_qty"], t["outside_base"], t["mismatch_count"]) == (L.POSITION_ACTIVE, 65, 65, 0)
    assert not [c for c in r.ex.calls if c[0] == "place"]               # nothing re-sent after the restart
    assert [x["qty"] for x in r.svc.outside_positions()] == [65]


def _kite(r, sym, side, qty, price):
    r.quotes.set(sym, price)
    r.ex.manual_order("NFO", sym, "MIS", side, qty, price)


def test_adopt_several_together_as_one_strategy_with_combined_sl(tmp_path):
    """An iron-condor-like set opened in Kite: tracked as ONE strategy, the combined SL squares every leg off."""
    r = Rig(tmp_path)
    syms = [r.svc.instruments.resolve("NIFTY", EXPIRY, k, ot).tradingsymbol
            for k, ot in ((24900, "PE"), (25000, "PE"), (25000, "CE"), (25100, "CE"))]
    for sym, side, px in zip(syms, ("BUY", "SELL", "SELL", "BUY"), (50, 100, 110, 60)):
        _kite(r, sym, side, 130, px)
    r.tick()
    st = StrategyService(r.svc, r.repo)
    r.svc.extra_tick = st.tick
    n = len(sent(r))
    res = st.adopt({"positions": [{"exchange": "NFO", "tradingsymbol": s, "product": "MIS"} for s in syms],
                    "sl_local": False})
    assert res["ok"] and len(res["trade_ids"]) == 4 and not res["errors"], res
    v = st.view(res["strategy_id"])
    assert v["open_legs"] == 4 and v["name"] == "Adopted NIFTY 4 legs (24900 PE, 25000 PE, 25000 CE, 25100 CE)"
    assert sorted((t["side"], t["entry_avg_price"]) for t in v["legs"]) == \
        [("BUY", 50), ("BUY", 60), ("SELL", 100), ("SELL", 110)]
    assert r.svc.outside_positions() == [] and len(sent(r)) == n
    st.set_exits(res["strategy_id"], {"exit_sl_pnl": -2000})
    r.quotes.set(syms[1], 120)                       # short 25000 PE up 20 x 130 = -2600 combined
    r.tick()
    r.tick()
    r.tick()
    assert all(r.trade(t)["status"] == L.EXITED for t in res["trade_ids"])
    assert all(r.ex.net[f"NFO|{s}|MIS"]["qty"] == 0 for s in syms)
    assert r.repo.strategy(res["strategy_id"])["exit_reason"] == "STRATEGY_LOSS_LIMIT"


def test_adopt_together_is_all_or_nothing(tmp_path):
    r = Rig(tmp_path)
    other = r.svc.instruments.resolve("NIFTY", EXPIRY, 24900, "PE").tradingsymbol
    _kite(r, SYM, "BUY", 65, 100)
    _kite(r, other, "SELL", 65, 100)
    r.tick()
    st = StrategyService(r.svc, r.repo)
    res = st.adopt({"positions": [{"exchange": "NFO", "tradingsymbol": SYM, "product": "MIS"},
                                  {"exchange": "NFO", "tradingsymbol": other, "product": "MIS", "lots": 3}]})
    assert not res["ok"] and "lots must be 1 to 1" in res["errors"][0]
    assert r.repo.strategies() == [] and r.repo.trades(L.OPEN_STATUSES) == []     # nothing adopted
    assert len(r.svc.outside_positions()) == 2
