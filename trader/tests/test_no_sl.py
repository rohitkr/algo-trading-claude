"""2026-10-23: a stop-loss only when YOU set one. A blank SL = no stop: no SL order at Zerodha, never stopped out;
Edit can add one later or remove one (its SL order is cancelled). Old databases are migrated in place."""
from __future__ import annotations

import sqlite3

from trader import lifecycle as L
from trader.broker import is_working
from trader.repository import Repository
from trader.strategy import StrategyService

from .fakes import Clock
from .test_service import EXPIRY, Rig


def leg(tmp_path, side="BUY", sl=None):
    r = Rig(tmp_path)
    r.price(100)
    strat = StrategyService(r.svc, r.repo)
    r.svc.extra_tick = strat.tick
    res = strat.create_and_trade({"config": {"order_type": "MIS"}, "legs": [dict(
        underlying="NIFTY", expiry=EXPIRY.isoformat(), strike=25000, option_type="CE", side=side, lots=2,
        entry_price=100, sl_value=sl, sl_type="POINTS", tp_value=None, tp_type="POINTS")]})
    assert res["ok"], res
    r.tick()
    return r, res["confirmed"][0]


def sl_working(r, tid):
    return [o for o in r.repo.orders(tid, "SL") if is_working(o["status"])]


def test_no_sl_leg_runs_without_any_sl_order_and_is_never_stopped(tmp_path):
    for side, crash in (("BUY", 1.0), ("SELL", 400.0)):
        r, tid = leg(tmp_path / side, side)
        t = r.trade(tid)
        assert t["status"] == L.POSITION_ACTIVE and t["current_sl"] is None
        assert r.repo.orders(tid, "SL") == []
        r.price(crash)                                       # any move: nothing exits it
        for _ in range(3):
            r.tick()
        assert r.trade(tid)["status"] == L.POSITION_ACTIVE and r.repo.orders(tid, "EXIT") == []


def test_edit_adds_a_stop_later_then_removes_it(tmp_path):
    r, tid = leg(tmp_path)
    plan = r.svc.prepare_edit(tid, {"stop_loss": 90})
    assert plan["ok"], plan
    r.svc.apply_edit(tid, plan["token"])
    r.tick()
    w = sl_working(r, tid)
    assert len(w) == 1 and w[0]["trigger_price"] == 90
    plan = r.svc.prepare_edit(tid, {"stop_loss": ""})       # blank = remove the stop
    assert plan["ok"], plan
    r.svc.apply_edit(tid, plan["token"])
    r.tick(); r.tick()
    assert r.trade(tid)["current_sl"] is None and sl_working(r, tid) == []
    assert not r.trade(tid)["sl_software_only"]
    r.price(50); r.tick()
    assert r.trade(tid)["status"] == L.POSITION_ACTIVE


def test_a_leg_with_a_stop_still_gets_it(tmp_path):
    r, tid = leg(tmp_path, sl=10)
    assert r.trade(tid)["current_sl"] == 90 and len(sl_working(r, tid)) == 1


def test_trailing_on_a_no_sl_leg_sets_the_first_stop(tmp_path):
    r, tid = leg(tmp_path)
    r.repo.update_trade(tid, trail_enabled=1, trail_type="POINTS", trail_value=5, trail_step=0.05)
    r.price(110)
    r.tick()
    assert r.trade(tid)["current_sl"] == 105


def test_old_database_is_migrated_in_place(tmp_path):
    db = tmp_path / "old.sqlite"
    Repository(db, Clock()).close()
    c = sqlite3.connect(db)                                  # make it look like a pre-2026-10-23 database
    sql = c.execute("SELECT sql FROM sqlite_master WHERE name='trades'").fetchone()[0]
    old = sql.replace("initial_sl          REAL,", "initial_sl          REAL NOT NULL,").replace(
        "current_sl          REAL,", "current_sl          REAL NOT NULL,")
    v = c.execute("PRAGMA schema_version").fetchone()[0]
    c.execute("PRAGMA writable_schema=ON")
    c.execute("UPDATE sqlite_master SET sql=? WHERE name='trades'", (old,))
    c.execute(f"PRAGMA schema_version={v + 1}")
    c.commit(); c.close()
    c = sqlite3.connect(db)
    assert {r[1]: r[3] for r in c.execute("PRAGMA table_info(trades)")}["current_sl"] == 1
    c.close()
    repo = Repository(db, Clock())
    cols = {r["name"]: r["notnull"] for r in repo.conn.execute("PRAGMA table_info(trades)")}
    assert cols["current_sl"] == 0 and cols["initial_sl"] == 0
    assert repo.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
