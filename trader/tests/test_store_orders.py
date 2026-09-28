"""Increments 1-2: schema, lifecycle state machine, idempotent order placement."""
from __future__ import annotations

import sqlite3

import pytest

from trader import lifecycle as L
from trader.broker import KiteTraderBroker
from trader.orders import OrderPlacer
from trader.paper import PaperExchange
from trader.repository import Repository

from .fakes import Clock


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    repo = Repository(tmp_path / "t.sqlite", clock)
    prices = {"NIFTY2692925000CE": 100.0}
    ex = PaperExchange(lambda e, s: prices.get(s), tmp_path / "paper.json", clock=clock, auto_match=False)
    events = []
    audit = lambda tid, ev, lvl, d: (events.append((tid, ev, d)), repo.event(tid, ev, lvl, d))  # noqa: E731
    placer = OrderPlacer(repo, KiteTraderBroker(ex, live=False), audit, grace_s=15, clock=clock)
    tid = repo.insert_trade(dict(mode="PAPER", trade_date="2026-09-28", underlying="NIFTY", exchange="NFO",
                                 tradingsymbol="NIFTY2692925000CE", expiry="2026-09-29", strike=25000,
                                 option_type="CE", side="BUY", product="MIS", lot_size=65, tick_size=0.05, lots=1,
                                 quantity=65, entry_price=100, initial_sl=90, current_sl=90, status=L.DRAFT))
    return clock, repo, ex, placer, tid, events


def test_state_machine_rejects_illegal_transitions(env):
    clock, repo, ex, placer, tid, _ = env
    assert repo.set_status(tid, L.READY, "VALIDATION_PASSED")
    with pytest.raises(L.IllegalTransition):
        repo.set_status(tid, L.EXITED, "BOGUS")
    # compare-and-set: a second confirm of the same READY trade does nothing
    assert repo.set_status(tid, L.ENTRY_ORDER_PLACED, "CONFIRMED", expect=L.READY)
    assert not repo.set_status(tid, L.ENTRY_ORDER_PLACED, "CONFIRMED", expect=L.READY)
    evs = [e["event"] for e in repo.events(tid)]
    assert evs.count("CONFIRMED") == 1


def test_events_are_append_only(env):
    _, repo, *_ = env
    repo.event(None, "X")
    with pytest.raises(sqlite3.DatabaseError):
        repo.conn.execute("DELETE FROM trade_events")
    with pytest.raises(sqlite3.DatabaseError):
        repo.conn.execute("UPDATE trade_events SET event='Y'")


def test_tokens_single_use_and_expire(env):
    clock, repo, _, _, tid, _ = env
    tok = repo.issue_token(tid, "CONFIRM", clock.advance(0) .replace(minute=2))
    assert not repo.consume_token(tok, tid, "EXIT")
    assert repo.consume_token(tok, tid, "CONFIRM")
    assert not repo.consume_token(tok, tid, "CONFIRM")
    tok2 = repo.issue_token(tid, "CONFIRM", clock.now)
    clock.advance(1)
    assert not repo.consume_token(tok2, tid, "CONFIRM")


def test_tag_unique_and_short(env):
    _, repo, _, placer, tid, _ = env
    t = repo.trade(tid)
    placer.place(t, "ENTRY", "BUY", 65, "LIMIT", 100)
    placer.place(t, "SL", "SELL", 65, "SL", 85, 90)
    tags = [o["tag"] for o in repo.orders(tid)]
    assert len(set(tags)) == 2 and all(len(x) <= 20 and x.isalnum() for x in tags)


def test_duplicate_of_working_kind_is_refused(env):
    _, repo, ex, placer, tid, events = env
    t = repo.trade(tid)
    assert placer.place(t, "ENTRY", "BUY", 65, "LIMIT", 95) is not None
    assert placer.place(t, "ENTRY", "BUY", 65, "LIMIT", 95) is None
    assert len([c for c in ex.calls if c[0] == "place"]) == 1
    assert any(e[1] == "DUPLICATE_PREVENTED" for e in events)


def test_lost_response_is_recovered_by_tag_not_replaced(env, tmp_path):
    clock, repo, ex, placer, tid, _ = env
    ex.raise_after_place = 1                     # Kite created the order but the response was lost
    row = placer.place(repo.trade(tid), "ENTRY", "BUY", 65, "LIMIT", 95)
    assert row["status"] == "UNCERTAIN" and row["broker_order_id"] is None
    # a retry is refused while the first is unresolved
    assert placer.place(repo.trade(tid), "ENTRY", "BUY", 65, "LIMIT", 95) is None
    # "restart": new process objects on the same files
    repo2 = Repository(tmp_path / "t.sqlite", clock)
    ex2 = PaperExchange(lambda e, s: 100.0, tmp_path / "paper.json", clock=clock, auto_match=False)
    p2 = OrderPlacer(repo2, KiteTraderBroker(ex2, live=False), lambda *a: None, 15, clock)
    p2.sync(KiteTraderBroker(ex2, live=False).snapshot(clock()))
    o = repo2.orders(tid)[0]
    assert o["broker_order_id"] and o["status"] == "OPEN"
    assert len([c for c in ex2.calls if c[0] == "place"]) == 0 and len(ex2.orders_) == 1


def test_crash_before_send_becomes_not_placed_after_grace(env):
    clock, repo, ex, placer, tid, _ = env
    ex.raise_before_place = 1                    # nothing reached Kite
    placer.place(repo.trade(tid), "ENTRY", "BUY", 65, "LIMIT", 95)
    broker = KiteTraderBroker(ex, live=False)
    placer.sync(broker.snapshot(clock()))
    assert repo.orders(tid)[0]["status"] == "UNCERTAIN"
    clock.advance(10)
    placer.sync(broker.snapshot(clock()))
    assert repo.orders(tid)[0]["status"] == "UNCERTAIN"       # still inside the grace period
    clock.advance(10)
    placer.sync(broker.snapshot(clock()))
    assert repo.orders(tid)[0]["status"] == "NOT_PLACED"
    assert placer.place(repo.trade(tid), "ENTRY", "BUY", 65, "LIMIT", 95) is not None   # now allowed


def test_order_book_lag_does_not_cause_not_placed(env):
    clock, repo, ex, placer, tid, _ = env
    ex.hide_new_orders = 1
    ex.raise_after_place = 1
    placer.place(repo.trade(tid), "ENTRY", "BUY", 65, "LIMIT", 95)
    broker = KiteTraderBroker(ex, live=False)
    placer.sync(broker.snapshot(clock()))            # hidden once
    clock.advance(3)
    placer.sync(broker.snapshot(clock()))
    assert repo.orders(tid)[0]["status"] == "OPEN"


def test_sync_copies_partial_fills(env):
    clock, repo, ex, placer, tid, _ = env
    row = placer.place(repo.trade(tid), "ENTRY", "BUY", 130, "LIMIT", 95)
    ex.fill(row["broker_order_id"], 65, 95)
    placer.sync(KiteTraderBroker(ex, live=False).snapshot(clock()))
    o = repo.order(row["id"])
    assert (o["status"], o["filled_qty"], o["avg_price"]) == ("OPEN", 65, 95)
