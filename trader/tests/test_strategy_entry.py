"""Multi-leg entry safety (regression for 2026-09-30: a SENSEX iron condor placed only its two BUY legs because
each leg counted as a separate trade against TRADER_MAX_OPEN_TRADES=3, and the refused SELL legs vanished).

A strategy is ONE position for max-open-trades / trades-per-day; any leg failing its checks means nothing is
sent; a failed hedge (BUY) leg stops the SELL legs; legs that never became positions stay on the strategy."""
from __future__ import annotations

from trader import lifecycle as L
from trader.service import ActionError
from trader.strategy import StrategyService

from .fakes import EXPIRY
from .test_service import Rig

SYM = {(k, t): f"NIFTY26929{k}{t}" for k in (24900, 25000, 25100) for t in ("CE", "PE")}


def leg(strike: int, opt: str, side: str, price: float) -> dict:
    return dict(underlying="NIFTY", expiry=EXPIRY.isoformat(), strike=strike, option_type=opt, side=side,
                lots=1, entry_price=price, sl_value=30, sl_type="POINTS")


def iron_condor() -> dict:
    # listed SELL-first on purpose: the engine must still send the BUY (hedge) legs first
    return {"config": {"order_type": "MIS"},
            "legs": [leg(25000, "PE", "SELL", 90), leg(25000, "CE", "SELL", 100),
                     leg(24900, "PE", "BUY", 60), leg(25100, "CE", "BUY", 70)]}


def rig(tmp_path, **cfg) -> tuple[Rig, StrategyService]:
    r = Rig(tmp_path, **cfg)
    for (k, t), sym in SYM.items():
        r.quotes.set(sym, {(25000, "PE"): 90, (25000, "CE"): 100, (24900, "PE"): 60, (25100, "CE"): 70,
                           (24900, "CE"): 150, (25100, "PE"): 150}[(k, t)])
    strat = StrategyService(r.svc, r.repo)
    r.svc.extra_tick = strat.tick
    return r, strat


def open_standalone_trade(r: Rig) -> int:
    r.quotes.set(SYM[(24900, "CE")], 150)
    tid = r.open_trade(strike=24900, option_type="CE", side="BUY", entry_price=150, stop_loss=120, target=200)
    r.tick()
    assert r.trade(tid)["status"] == L.POSITION_ACTIVE
    return tid


def test_iron_condor_counts_as_one_open_trade(tmp_path) -> None:
    # the exact incident: limit 3, one other trade already open, 4-leg strategy
    r, strat = rig(tmp_path, max_open_trades=3)
    open_standalone_trade(r)
    res = strat.create_and_trade(iron_condor())
    assert res["ok"] and not res["failed"], res
    assert len(res["confirmed"]) == 4
    sides = [r.trade(t)["side"] for t in res["confirmed"]]
    assert sides == ["BUY", "BUY", "SELL", "SELL"]                       # hedges first
    placed = {c[1] for c in r.places()}
    assert {SYM[(25000, "PE")], SYM[(25000, "CE")]} <= placed           # the SELL legs really reached the broker
    assert all(r.trade(t)["group_id"] == res["strategy_id"] for t in res["confirmed"])


def test_strategy_uses_one_slot_of_trades_per_day(tmp_path) -> None:
    r, strat = rig(tmp_path, max_trades_per_day=2)
    open_standalone_trade(r)                                            # 1 of 2 used
    res = strat.create_and_trade(iron_condor())                         # 4 legs = 1 more position
    assert res["ok"] and len(res["confirmed"]) == 4, res
    assert r.repo.confirmed_on(r.clock().date()) == 2


def test_over_the_limit_nothing_is_sent_and_the_reason_is_returned(tmp_path) -> None:
    r, strat = rig(tmp_path, max_open_trades=1)
    open_standalone_trade(r)
    before = len(r.places())
    res = strat.create_and_trade(iron_condor())
    assert not res["ok"]
    assert any("max_open_trades" in e for e in res["errors"]), res["errors"]
    assert len(r.places()) == before                                    # all-or-nothing: not one leg sent
    assert strat.repo.strategies() == []


def test_failed_hedge_stops_the_sell_legs_and_everything_stays_visible(tmp_path, monkeypatch) -> None:
    r, strat = rig(tmp_path)
    real_confirm = r.svc.confirm

    def confirm(trade_id: int, token: str) -> dict:
        if r.trade(trade_id)["tradingsymbol"] == SYM[(25100, "CE")]:
            raise ActionError("cannot reach Zerodha to check positions: timeout")
        return real_confirm(trade_id, token)

    monkeypatch.setattr(r.svc, "confirm", confirm)
    res = strat.create_and_trade(iron_condor())
    placed = {c[1] for c in r.places()}
    assert SYM[(24900, "PE")] in placed
    assert SYM[(25000, "PE")] not in placed and SYM[(25000, "CE")] not in placed   # no naked shorts
    view = strat.view(res["strategy_id"])
    failed = {f["tradingsymbol"]: f["error"] for f in view["failed_legs"]}
    assert set(failed) == {SYM[(25100, "CE")], SYM[(25000, "PE")], SYM[(25000, "CE")]}
    assert failed[SYM[(25000, "PE")]].startswith("not placed: hedge leg failed")
    assert len(view["legs"]) == 4                                       # nothing silently dropped


def test_a_failed_sell_leg_is_shown_on_the_strategy(tmp_path, monkeypatch) -> None:
    r, strat = rig(tmp_path)
    real_confirm = r.svc.confirm

    def confirm(trade_id: int, token: str) -> dict:
        if r.trade(trade_id)["tradingsymbol"] == SYM[(25000, "CE")]:
            raise ActionError("blocked by risk limits: ltp_not_through_stop (LTP 131 is already through the stop 130)")
        return real_confirm(trade_id, token)

    monkeypatch.setattr(r.svc, "confirm", confirm)
    res = strat.create_and_trade(iron_condor())
    assert res["ok"] and len(res["confirmed"]) == 3 and len(res["failed"]) == 1
    view = strat.view(res["strategy_id"])
    assert [f["tradingsymbol"] for f in view["failed_legs"]] == [SYM[(25000, "CE")]]
    assert "ltp_not_through_stop" in res["failed"][0]["error"]


def test_editing_a_leg_does_not_count_its_siblings(tmp_path) -> None:
    r, strat = rig(tmp_path, max_open_trades=1)
    res = strat.create_and_trade(iron_condor())
    assert res["ok"] and len(res["confirmed"]) == 4, res
    r.tick()
    tid = res["confirmed"][2]
    plan = r.svc.prepare_edit(tid, {"stop_loss": r.trade(tid)["current_sl"] + 5})
    assert not [c for c in plan.get("risk", []) if not c["passed"] and c["name"] == "max_open_trades"], plan


def test_market_legs_are_marketable_limits_from_the_live_price(tmp_path) -> None:
    r, strat = rig(tmp_path)
    payload = iron_condor()
    for lg in payload["legs"]:
        lg["price_type"] = "MARKET"
        lg["entry_price"] = None                                         # no price needed for Market
    res = strat.create_and_trade(payload)
    assert res["ok"] and len(res["confirmed"]) == 4, res
    buy_pe = next(r.trade(t) for t in res["confirmed"] if r.trade(t)["tradingsymbol"] == SYM[(24900, "PE")])
    sell_ce = next(r.trade(t) for t in res["confirmed"] if r.trade(t)["tradingsymbol"] == SYM[(25000, "CE")])
    assert buy_pe["entry_price"] == 61.2                                 # LTP 60 + 2% buffer, rounded up to tick
    assert sell_ce["entry_price"] == 98.0                                # LTP 100 - 2%, rounded down


def test_market_leg_without_a_live_price_places_nothing(tmp_path) -> None:
    r, strat = rig(tmp_path)
    r.quotes.prices.pop(SYM[(25000, "CE")])
    payload = iron_condor()
    payload["legs"][1]["price_type"] = "MARKET"
    res = strat.create_and_trade(payload)
    assert not res["ok"] and any("no live price" in e for e in res["errors"])
    assert r.places() == []


def test_user_arranged_execution_order_is_kept(tmp_path) -> None:
    r, strat = rig(tmp_path)
    payload = iron_condor()                                              # SELL PE, SELL CE, BUY PE, BUY CE
    payload["keep_order"] = True
    res = strat.create_and_trade(payload)
    assert res["ok"], res
    assert [c[1] for c in r.places()][:4] == [SYM[(25000, "PE")], SYM[(25000, "CE")], SYM[(24900, "PE")],
                                               SYM[(25100, "CE")]]


def test_default_execution_order_is_buy_first(tmp_path) -> None:
    r, strat = rig(tmp_path)
    res = strat.create_and_trade(iron_condor())
    assert res["ok"], res
    assert [r.trade(t)["side"] for t in res["confirmed"]] == ["BUY", "BUY", "SELL", "SELL"]


def test_limit_prices_are_rounded_to_the_tick_not_rejected(tmp_path) -> None:
    r, strat = rig(tmp_path)
    payload = iron_condor()
    payload["legs"][2]["entry_price"] = 60.03                           # BUY 24900 PE, typed off-tick
    res = strat.create_and_trade(payload)
    assert res["ok"] and len(res["confirmed"]) == 4, res
    leg = next(r.trade(t) for t in res["confirmed"] if r.trade(t)["tradingsymbol"] == SYM[(24900, "PE")])
    assert leg["entry_price"] == 60.05
