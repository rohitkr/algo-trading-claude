"""Phase 3: a Telegram signal -> your trade, through the trader's paper engine (trader.tests.Rig)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from telegram_signals.store import MessageStore
from telegram_signals.trading import SignalTrader
from trader import lifecycle as L
from trader.strategy import StrategyService
from trader.tests.test_service import Rig

UTC = timezone.utc
IST_OFFSET = timedelta(hours=5, minutes=30)
SYM = {(k, t): f"NIFTY26929{k}{t}" for k in (24900, 25000, 25100) for t in ("CE", "PE")}


def setup(tmp_path, **rules):
    r = Rig(tmp_path)
    for k, sym in SYM.items():
        r.quotes.set(sym, 100.0)
    strat = StrategyService(r.svc, r.repo)
    r.svc.extra_tick = strat.tick
    store = MessageStore(tmp_path / "tg.sqlite")
    tr = SignalTrader(store, strat, r.svc)
    tr.save_rules({"mode": "auto", "start": "09:15", "end": "15:00", **rules})
    return r, strat, store, tr


def post(r, store, msg_id, text, reply_to=None, ago_s=5):
    when = (r.clock() - IST_OFFSET - timedelta(seconds=ago_s)).replace(tzinfo=UTC)   # trader clock is naive IST
    store.upsert(1, msg_id, when, text, reply_to=reply_to)


BULL = "🟢 BUY NIFTY 25000 CE\n💰 Entry : ₹150 - ₹154\n📊 Intraday Trade"
BEAR = "🟢 BUY NIFTY 25000 PE\n💰 Entry : ₹150 - ₹154\n📊 Intraday Trade"


def test_bullish_tip_sells_the_put_in_auto_mode(tmp_path):
    r, strat, store, tr = setup(tmp_path)
    post(r, store, 10, BULL)
    tr.tick()
    a = store.action(10)
    assert a["status"] == "PLACED" and a["plan"]["summary"].startswith("SELL NIFTY 25000 PE ×1")
    legs = strat.view(a["strategy_id"])["legs"]
    assert [(t["side"], t["tradingsymbol"]) for t in legs] == [("SELL", SYM[(25000, "PE")])]
    tr.tick()
    assert len(r.repo.strategies()) == 1                     # acted on once only


def test_bearish_credit_spread_hedge_first(tmp_path):
    r, strat, store, tr = setup(tmp_path, action="credit_spread", spread_width=1)
    post(r, store, 11, BEAR)
    tr.tick()
    plan = store.action(11)["plan"]
    assert [(l["side"], l["strike"], l["option_type"]) for l in plan["legs"]] == [("BUY", 25100, "CE"), ("SELL", 25000, "CE")]
    assert store.action(11)["status"] == "PLACED"


def test_old_signals_from_the_catch_up_never_trade(tmp_path):
    r, strat, store, tr = setup(tmp_path)
    post(r, store, 12, BULL, ago_s=3600)                      # posted an hour ago (e.g. read at startup)
    tr.tick()
    assert store.action(12) is None and r.repo.strategies() == []


def test_alert_and_gates_place_nothing(tmp_path):
    r, strat, store, tr = setup(tmp_path, mode="alert")
    post(r, store, 13, BULL)
    tr.tick()
    assert store.action(13)["status"] == "ALERT" and r.repo.strategies() == []
    tr.save_rules({"mode": "auto", "indices": ["SENSEX"]})
    post(r, store, 14, BULL)
    tr.tick()
    assert store.action(14)["status"] == "SKIPPED" and "not in your list" in store.action(14)["reason"]
    tr.save_rules({"indices": ["NIFTY"], "end": "09:30"})
    post(r, store, 15, BULL)
    tr.tick()
    assert "time window" in store.action(15)["reason"]


def test_approve_mode_places_on_approval_and_expires_otherwise(tmp_path):
    r, strat, store, tr = setup(tmp_path, mode="approve", approve_timeout_s=60)
    post(r, store, 16, BULL)
    tr.tick()
    assert store.action(16)["status"] == "PROPOSED" and r.repo.strategies() == []
    assert tr.approve(16)["status"] == "PLACED"
    post(r, store, 17, BEAR)
    tr.tick()
    with pytest.raises(ValueError):
        tr.approve(99)
    tr.skip(17)
    assert store.action(17)["status"] == "SKIPPED"


def test_max_trades_per_day(tmp_path):
    r, strat, store, tr = setup(tmp_path, max_trades_per_day=1)
    post(r, store, 18, BULL)
    tr.tick()
    post(r, store, 19, BEAR)
    tr.tick()
    assert store.action(18)["status"] == "PLACED" and "max 1 trades" in store.action(19)["reason"]


def test_their_sl_hit_squares_off_ours(tmp_path):
    r, strat, store, tr = setup(tmp_path)
    post(r, store, 20, BULL)
    tr.tick()
    r.tick()
    sid = store.action(20)["strategy_id"]
    leg = strat.view(sid)["legs"][0]
    assert leg["status"] == L.POSITION_ACTIVE
    post(r, store, 21, "🛑 STOP LOSS HIT | NIFTY 25000 CE\n💹 Live LTP: ₹135", reply_to=20)
    tr.tick()
    for _ in range(3):
        r.tick()
    assert r.trade(leg["id"])["status"] == L.EXITED and store.action(20)["channel_exit"] == 1


def test_rules_are_validated():
    from telegram_signals.trading import SignalRules
    with pytest.raises(ValueError):
        SignalRules.from_json({"mode": "yolo"})
    with pytest.raises(ValueError):
        SignalRules.from_json({"lots": {"NIFTY": 0}})
