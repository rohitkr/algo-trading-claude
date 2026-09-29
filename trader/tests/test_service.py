"""Increments 3-4: lifecycle, reconciliation, crash recovery, manual exits, SL/target/trailing/partial,
auto-exit and risk limits, all against the fake Kite (trader.paper.PaperExchange)."""
from __future__ import annotations

from datetime import datetime

import pytest

from trader import lifecycle as L
from trader.broker import KiteTraderBroker
from trader.config import TraderConfig
from trader.instruments import InstrumentService
from trader.market import ManualQuotes
from trader.paper import PaperExchange
from trader.repository import Repository
from trader.service import ActionError, TradeService

from .fakes import EXPIRY, Clock, loader

SYM = "NIFTY2692925000CE"


class Rig:
    """One 'process': service + fake Kite on files in tmp_path. restart() builds a fresh process."""

    def __init__(self, tmp_path, clock=None, auto=True, **cfg):
        self.tmp, self.clock, self.auto = tmp_path, clock or Clock(), auto
        self.cfg = TraderConfig(db_path=tmp_path / "t.sqlite", audit_dir=tmp_path / "logs",
                                paper_state=tmp_path / "paper.json", **cfg)
        self.quotes = ManualQuotes()
        self.quotes.set(SYM, 101.0)
        self._build()

    def _build(self):
        self.ex = PaperExchange(self.quotes.price_for, self.cfg.paper_state, clock=self.clock, auto_match=self.auto)
        self.repo = Repository(self.cfg.db_path, self.clock)
        self.svc = TradeService(self.cfg, self.repo, KiteTraderBroker(self.ex, live=False),
                                InstrumentService(loader, self.cfg.underlyings, self.clock), self.quotes, self.clock)

    def restart(self):
        calls = self.ex.calls
        self.repo.close()
        self._build()
        self.prev_calls = calls
        return self.svc.startup()

    def price(self, p):
        self.quotes.set(SYM, p)

    def tick(self, seconds=3):
        self.clock.advance(seconds)
        return self.svc.tick()

    def trade(self, tid):
        return self.repo.trade(tid)

    def places(self, kind=None):
        return [c for c in self.ex.calls if c[0] == "place"]

    def events(self, tid):
        return [e["event"] for e in reversed(self.repo.events(tid, limit=1000))]

    def open_trade(self, **kw):
        req = dict(underlying="NIFTY", expiry=EXPIRY.isoformat(), strike=25000, option_type="CE", side="BUY",
                   lots=1, entry_price=100, stop_loss=90, target=130)
        req.update(kw)
        p = self.svc.preview(req)
        assert p["ok"], p
        self.svc.confirm(p["trade_id"], p["token"])
        return p["trade_id"]


def test_buy_entry_sl_order_then_target(tmp_path):
    r = Rig(tmp_path)
    tid = r.open_trade()
    assert r.trade(tid)["status"] == L.ENTRY_PENDING          # LIMIT 100 below LTP 101: working, not filled
    r.tick()
    assert r.trade(tid)["filled_qty"] == 0                     # submitted != executed
    r.price(100)
    r.tick()
    t = r.trade(tid)
    assert (t["status"], t["filled_qty"], t["entry_avg_price"]) == (L.POSITION_ACTIVE, 65, 100)
    sl = [o for o in r.repo.orders(tid) if o["kind"] == "SL"][0]
    assert (sl["order_type"], sl["trigger_price"], sl["side"], sl["quantity"]) == ("SL", 90, "SELL", 65)
    assert sl["price"] < 90                                    # stop-limit below the trigger
    r.price(130)
    r.tick()
    t = r.trade(tid)
    assert t["status"] == L.EXITED and t["exit_reason"] == L.TARGET_HIT
    assert t["realized_pnl"] == pytest.approx(30 * 65)
    assert r.repo.orders(tid, "SL")[0]["status"] == "CANCELLED"   # SL taken off before the exit
    assert r.ex.net["NFO|%s|MIS" % SYM]["qty"] == 0


def test_sell_trade_resting_sl_fills(tmp_path):
    r = Rig(tmp_path)
    r.price(100)
    tid = r.open_trade(side="SELL", entry_price=100, stop_loss=110, target=80)
    r.tick()
    assert r.trade(tid)["status"] == L.POSITION_ACTIVE
    sl = r.repo.orders(tid, "SL")[0]
    assert (sl["side"], sl["trigger_price"]) == ("BUY", 110) and sl["price"] > 110
    r.price(111)
    r.tick()
    t = r.trade(tid)
    assert (t["status"], t["exit_reason"]) == (L.EXITED, L.STOP_LOSS_HIT)
    assert t["realized_pnl"] == pytest.approx(-11 * 65)
    assert len([c for c in r.places() if c[1] == SYM]) == 2   # entry + SL, no extra exit order


def test_validation_blocks_wrong_side_stops(tmp_path):
    r = Rig(tmp_path)
    base = dict(underlying="NIFTY", expiry=EXPIRY.isoformat(), strike=25000, option_type="CE", lots=1)
    p = r.svc.preview({**base, "side": "BUY", "entry_price": 150, "stop_loss": 160, "target": 195})
    assert not p["ok"] and any("below the entry" in e for e in p["errors"])
    p = r.svc.preview({**base, "side": "SELL", "entry_price": 150, "stop_loss": 140, "target": 120})
    assert not p["ok"] and any("above the entry" in e for e in p["errors"])
    p = r.svc.preview({**base, "side": "SELL", "entry_price": 150, "stop_loss": 160, "target": 170})
    assert not p["ok"] and any("target" in e for e in p["errors"])
    p = r.svc.preview({**base, "side": "BUY", "entry_price": 100.03, "stop_loss": 90, "target": 120})
    assert not p["ok"] and any("tick" in e for e in p["errors"])
    assert r.places() == []


def test_lot_size_from_instruments(tmp_path):
    r = Rig(tmp_path, max_order_value=0)
    r.quotes.set("SENSEX26O0181000CE", 300)
    p = r.svc.preview(dict(underlying="SENSEX", expiry="2026-10-01", strike=81000, option_type="CE", side="BUY",
                           lots=3, entry_price=300, stop_loss=290, target=320))
    assert p["ok"], p
    s = p["summary"]
    assert (s["exchange"], s["lot_size"], s["quantity"]) == ("BFO", 20, 60)


def test_double_confirm_places_one_entry(tmp_path):
    r = Rig(tmp_path)
    p = r.svc.preview(dict(underlying="NIFTY", expiry=EXPIRY.isoformat(), strike=25000, option_type="CE",
                           side="BUY", lots=1, entry_price=100, stop_loss=90))
    r.svc.confirm(p["trade_id"], p["token"])
    with pytest.raises(ActionError):
        r.svc.confirm(p["trade_id"], p["token"])
    assert len(r.places()) == 1


def test_restart_after_submit_before_response_no_duplicate(tmp_path):
    r = Rig(tmp_path)
    r.ex.raise_after_place = 1                   # Kite accepted the entry, the response never arrived
    tid = r.open_trade()
    t = r.trade(tid)
    assert t["status"] == L.ENTRY_ORDER_PLACED and r.repo.orders(tid)[0]["status"] == "UNCERTAIN"
    r.restart()                                  # process crash + restart
    t = r.trade(tid)
    assert t["status"] == L.ENTRY_PENDING and t["entry_order_id"]
    assert r.places() == []                      # the new process placed nothing
    assert "ORDER_RECOVERED" in r.events(tid)
    r.price(100)
    r.tick()
    assert r.trade(tid)["status"] == L.POSITION_ACTIVE
    assert len(r.ex.orders_) == 2                # one entry + one SL, ever


def test_crash_before_send_marks_entry_rejected_never_resent(tmp_path):
    r = Rig(tmp_path, order_lookup_grace_s=10)
    r.ex.raise_before_place = 1
    tid = r.open_trade()
    r.restart()
    r.tick(5)
    r.tick(11)
    t = r.trade(tid)
    assert t["status"] == L.REJECTED and "never reached" in t["error"]
    assert len(r.ex.orders_) == 0


def test_partial_entry_fills_sl_follows_filled_qty(tmp_path):
    r = Rig(tmp_path, auto=False)
    r.price(100)
    tid = r.open_trade(lots=2)
    entry = r.repo.orders(tid, "ENTRY")[0]
    r.ex.fill(entry["broker_order_id"], 65, 100)
    r.tick()
    t = r.trade(tid)
    assert (t["status"], t["filled_qty"], t["position_status"]) == (L.ENTRY_PENDING, 65, "PARTIAL")
    sl = r.repo.orders(tid, "SL")[0]
    assert sl["quantity"] == 65                   # protects what is filled
    r.ex.fill(entry["broker_order_id"], 65, 99)
    r.tick()
    t = r.trade(tid)
    assert (t["status"], t["filled_qty"], t["entry_avg_price"]) == (L.POSITION_ACTIVE, 130, 99.5)
    assert r.repo.orders(tid, "SL")[0]["quantity"] == 130
    assert len(r.repo.orders(tid, "SL")) == 1     # modified, not re-placed


def test_partial_fill_then_cancel_manages_filled_part(tmp_path):
    r = Rig(tmp_path, auto=False)
    r.price(100)
    tid = r.open_trade(lots=2)
    entry = r.repo.orders(tid, "ENTRY")[0]
    r.ex.fill(entry["broker_order_id"], 65, 100)
    r.tick()
    tok = r.svc.prepare(tid, "CANCEL")["token"]
    r.svc.cancel_entry(tid, tok)
    r.tick()
    t = r.trade(tid)
    assert (t["status"], t["filled_qty"], t["open_qty"]) == (L.POSITION_ACTIVE, 65, 65)


def test_entry_rejected(tmp_path):
    r = Rig(tmp_path)
    r.ex.reject_symbols.add(SYM)
    tid = r.open_trade()
    r.tick()
    t = r.trade(tid)
    assert t["status"] == L.REJECTED and "RMS" in t["error"]
    assert not r.repo.orders(tid, "SL")


def test_entry_cancelled_before_fill(tmp_path):
    r = Rig(tmp_path)
    tid = r.open_trade()
    r.svc.cancel_entry(tid, r.svc.prepare(tid, "CANCEL")["token"])
    assert r.trade(tid)["status"] == L.CANCELLED


def _active(r, **kw):
    r.price(100)
    tid = r.open_trade(**kw)
    r.tick()
    assert r.trade(tid)["status"] == L.POSITION_ACTIVE
    return tid


def test_manual_exit_detected_no_exit_order(tmp_path):
    r = Rig(tmp_path)
    tid = _active(r)
    n = len(r.places())
    r.ex.set_position("NFO", SYM, "MIS", 0)      # Rohit squares off in the Kite app
    r.tick()
    assert r.trade(tid)["status"] == L.POSITION_ACTIVE and r.trade(tid)["mismatch_count"] == 1
    r.price(130)                                 # target reached while mismatched: nothing may be sent
    r.tick()
    t = r.trade(tid)
    assert t["status"] == L.MANUALLY_EXITED and t["exit_reason"] == L.MANUAL_EXIT
    assert len(r.places()) == n                  # no exit order
    assert r.repo.orders(tid, "SL")[0]["status"] == "CANCELLED"   # our resting SL is taken off
    r.tick()
    r.restart()
    r.tick()
    assert len(r.places()) == 0 and r.trade(tid)["status"] == L.MANUALLY_EXITED


def test_manual_exit_while_down_then_sl_event_after_restart(tmp_path):
    r = Rig(tmp_path)
    tid = _active(r, side="SELL", entry_price=100, stop_loss=110, target=80)
    r.ex.cancel_order("regular", r.repo.orders(tid, "SL")[0]["broker_order_id"])
    r.ex.set_position("NFO", SYM, "MIS", 0)      # manual exit in Kite (SL cancelled there too)
    r.price(115)                                 # stop would trigger
    r.restart()
    r.tick()
    t = r.trade(tid)
    assert t["status"] == L.MANUALLY_EXITED
    assert r.places() == []                      # no exit, no re-entry


def test_user_exit_after_manual_exit_places_nothing(tmp_path):
    r = Rig(tmp_path, reconcile_confirmations=1)
    tid = _active(r)
    n = len(r.places())
    r.ex.set_position("NFO", SYM, "MIS", 0)
    r.svc.request_exit(tid, r.svc.prepare(tid, "EXIT")["token"])
    assert r.trade(tid)["status"] == L.MANUALLY_EXITED
    assert len(r.places()) == n


def test_manual_partial_exit_adopts_and_resizes_sl(tmp_path):
    r = Rig(tmp_path)
    tid = _active(r, lots=3)
    r.ex.set_position("NFO", SYM, "MIS", 65)
    r.tick()
    r.tick()
    t = r.trade(tid)
    assert (t["status"], t["open_qty"], t["outside_qty"]) == (L.POSITION_ACTIVE, 65, 130)
    sl = r.repo.orders(tid, "SL")[0]
    assert sl["quantity"] == 65
    assert "MANUAL_PARTIAL_EXIT_DETECTED" in r.events(tid)


def test_unexplained_position_stops_managing(tmp_path):
    r = Rig(tmp_path)
    tid = _active(r)
    r.ex.set_position("NFO", SYM, "MIS", 195)    # more than we hold
    r.tick()
    r.tick()
    assert r.trade(tid)["status"] == L.UNKNOWN
    n = len(r.places())
    r.price(130)
    r.tick()
    assert len(r.places()) == n
    r.ex.set_position("NFO", SYM, "MIS", 65)     # fixed by hand in Kite
    r.tick()
    assert r.trade(tid)["status"] == L.POSITION_ACTIVE


def test_position_lag_is_tolerated(tmp_path):
    r = Rig(tmp_path)
    tid = _active(r)
    key = f"NFO|{SYM}|MIS"
    r.ex.net[key]["qty"] = 0                     # positions lag one sync
    r.tick()
    r.ex.net[key]["qty"] = 65
    r.tick()
    t = r.trade(tid)
    assert t["status"] == L.POSITION_ACTIVE and t["mismatch_count"] == 0


def test_trailing_sl_moves_and_hits(tmp_path):
    r = Rig(tmp_path)
    tid = _active(r, trail_enabled=True, trail_type="POINTS", trail_value=10, trail_step=1, target=200)
    r.price(112)
    r.tick()
    t = r.trade(tid)
    assert t["current_sl"] == 102 and t["initial_sl"] == 90
    sl = r.repo.orders(tid, "SL")[0]
    assert sl["trigger_price"] == 102
    r.price(112.5)
    r.tick()
    assert r.trade(tid)["current_sl"] == 102      # below the 1-point step: unchanged
    r.price(120)
    r.tick()
    assert r.trade(tid)["current_sl"] == 110
    assert "TRAILING_SL_UPDATED" in r.events(tid)
    r.price(109)
    r.tick()
    t = r.trade(tid)
    assert (t["status"], t["exit_reason"]) == (L.EXITED, L.TRAILING_SL_HIT)
    assert len(r.repo.orders(tid, "SL")) == 1     # trailed by modification


def test_trailing_percent_for_sell(tmp_path):
    r = Rig(tmp_path)
    tid = _active(r, side="SELL", entry_price=100, stop_loss=120, target=50, trail_enabled=True,
                  trail_type="PERCENT", trail_value=10)
    r.price(80)
    r.tick()
    assert r.trade(tid)["current_sl"] == 88       # 80 * 1.10


def test_partial_booking_then_remaining_managed(tmp_path):
    r = Rig(tmp_path)
    tid = _active(r, lots=2, partial_enabled=True, partial_lots=1, partial_price=110, target=120)
    r.price(110)
    r.tick()
    t = r.trade(tid)
    assert t["open_qty"] == 65
    part = r.repo.orders(tid, "PARTIAL")[0]
    assert (part["quantity"], part["status"]) == (65, "COMPLETE")
    assert r.repo.orders(tid, "SL")[0]["quantity"] == 65          # SL shrunk to the remainder
    r.tick()
    assert r.trade(tid)["partial_done"] == 1
    r.price(115)
    r.tick()
    assert r.trade(tid)["status"] == L.POSITION_ACTIVE
    r.price(120)
    r.tick()
    t = r.trade(tid)
    assert (t["status"], t["exit_reason"]) == (L.EXITED, L.TARGET_HIT)
    assert t["realized_pnl"] == pytest.approx(65 * 10 + 65 * 20)
    assert r.ex.net[f"NFO|{SYM}|MIS"]["qty"] == 0


def test_partial_then_stop_on_remainder(tmp_path):
    r = Rig(tmp_path)
    tid = _active(r, lots=2, partial_enabled=True, partial_lots=1, partial_price=110, target=120)
    r.price(110)
    r.tick()
    r.tick()
    r.price(89)
    r.tick()
    t = r.trade(tid)
    assert (t["status"], t["exit_reason"]) == (L.EXITED, L.STOP_LOSS_HIT)
    assert r.ex.net[f"NFO|{SYM}|MIS"]["qty"] == 0


def test_auto_exit_time(tmp_path):
    clock = Clock(datetime(2026, 9, 28, 10, 0))
    r = Rig(tmp_path, clock=clock)
    tid = _active(r, auto_exit_time="10:05")
    r.price(104)
    r.tick(60)
    assert r.trade(tid)["status"] == L.POSITION_ACTIVE
    clock.now = datetime(2026, 9, 28, 10, 5)
    r.svc.tick()
    t = r.trade(tid)
    assert (t["status"], t["exit_reason"]) == (L.EXITED, L.AUTO_EXIT)
    assert t["realized_pnl"] == pytest.approx(4 * 65)


def test_square_off_time_cancels_unfilled_entry(tmp_path):
    clock = Clock(datetime(2026, 9, 28, 14, 59))
    r = Rig(tmp_path, clock=clock)
    tid = r.open_trade()
    clock.now = datetime(2026, 9, 28, 15, 15)
    r.svc.tick()
    assert r.trade(tid)["status"] == L.CANCELLED


def test_exit_reprices_until_filled(tmp_path):
    r = Rig(tmp_path, auto=False, exit_reprice_s=10)
    r.price(100)
    tid = r.open_trade()
    r.ex.fill(r.repo.orders(tid, "ENTRY")[0]["broker_order_id"], None, 100)
    r.tick()
    sl = r.repo.orders(tid, "SL")[0]
    r.svc.request_exit(tid, r.svc.prepare(tid, "EXIT")["token"])
    assert r.repo.order(sl["id"])["status"] == "CANCELLED"
    ex = r.repo.orders(tid, "EXIT")[0]
    assert r.trade(tid)["status"] == L.EXIT_PENDING
    r.price(95)
    r.tick(11)
    assert r.repo.order(ex["id"])["price"] == pytest.approx(93.1)
    r.ex.fill(ex["broker_order_id"], None, 95)
    r.tick()
    t = r.trade(tid)
    assert (t["status"], t["exit_reason"]) == (L.EXITED, L.USER_EXIT)
    assert len(r.repo.orders(tid, "EXIT")) == 1


def test_exit_response_lost_then_restart_no_duplicate_exit(tmp_path):
    r = Rig(tmp_path, auto=False)
    r.price(100)
    tid = r.open_trade()
    r.ex.fill(r.repo.orders(tid, "ENTRY")[0]["broker_order_id"], None, 100)
    r.tick()
    r.ex.raise_after_place = 1
    r.svc.request_exit(tid, r.svc.prepare(tid, "EXIT")["token"])
    assert r.repo.orders(tid, "EXIT")[0]["status"] == "UNCERTAIN"
    r.restart()
    r.tick()
    assert len(r.places()) == 0 and len(r.repo.orders(tid, "EXIT")) == 1
    ex = r.repo.orders(tid, "EXIT")[0]
    r.ex.fill(ex["broker_order_id"], None, 99)
    r.tick()
    assert r.trade(tid)["status"] == L.EXITED


def test_sl_cancelled_outside_falls_back_to_software_stop(tmp_path):
    r = Rig(tmp_path)
    tid = _active(r)
    r.ex.cancel_order("regular", r.repo.orders(tid, "SL")[0]["broker_order_id"])
    r.tick()
    t = r.trade(tid)
    assert t["sl_software_only"] == 1 and len(r.repo.orders(tid, "SL")) == 1   # not re-placed against the user
    r.price(89)
    r.tick()
    t = r.trade(tid)
    assert (t["status"], t["exit_reason"]) == (L.EXITED, L.STOP_LOSS_HIT)


def test_sl_limit_not_filling_in_gap_is_replaced_by_exit(tmp_path):
    r = Rig(tmp_path, stop_grace_s=5)
    tid = _active(r)
    r.price(80)                  # gaps through trigger 90 and the SL limit (85.5): resting SL cannot fill
    r.tick()
    assert r.trade(tid)["status"] == L.POSITION_ACTIVE
    r.tick(6)
    t = r.trade(tid)
    assert (t["status"], t["exit_reason"]) == (L.EXITED, L.STOP_LOSS_HIT)
    assert r.ex.net[f"NFO|{SYM}|MIS"]["qty"] == 0


def test_risk_limits_block_new_trades(tmp_path):
    r = Rig(tmp_path, max_open_trades=1, max_loss_per_trade=500)
    base = dict(underlying="NIFTY", expiry=EXPIRY.isoformat(), strike=25000, option_type="CE", side="BUY", lots=1)
    p = r.svc.preview({**base, "entry_price": 100, "stop_loss": 90})
    assert not p["ok"] and any(c["name"] == "max_loss_per_trade" and not c["passed"] for c in p["risk"])
    r2 = Rig(tmp_path / "b", max_open_trades=1)
    r2.open_trade()
    r2.quotes.set("NIFTY2692925100CE", 80)
    p = r2.svc.preview({**base, "strike": 25100, "entry_price": 80, "stop_loss": 75})
    assert not p["ok"] and any(c["name"] == "max_open_trades" and not c["passed"] for c in p["risk"])


def test_risk_window_lots_and_duplicate_symbol(tmp_path):
    clock = Clock(datetime(2026, 9, 28, 15, 5))
    r = Rig(tmp_path, clock=clock, max_lots_per_trade=2)
    base = dict(underlying="NIFTY", expiry=EXPIRY.isoformat(), strike=25000, option_type="CE", side="BUY",
                entry_price=100, stop_loss=95)
    p = r.svc.preview({**base, "lots": 3})
    failed = {c["name"] for c in p["risk"] if not c["passed"]}
    assert {"trading_end", "max_lots_per_trade"} <= failed
    clock.now = datetime(2026, 9, 28, 11, 0)
    r.ex.manual_order("NFO", SYM, "MIS", "BUY", 65, 100)          # Rohit already holds this symbol
    p = r.svc.preview({**base, "lots": 1})
    assert "no_outside_position" in {c["name"] for c in p["risk"] if not c["passed"]}


def test_daily_loss_halts_new_trades(tmp_path):
    r = Rig(tmp_path, max_daily_loss=500, max_loss_per_trade=5000)
    tid = _active(r)
    r.price(91)
    r.tick()                                           # unrealised -585
    assert r.svc.halted()
    r.quotes.set("NIFTY2692925100CE", 80)
    p = r.svc.preview(dict(underlying="NIFTY", expiry=EXPIRY.isoformat(), strike=25100, option_type="CE",
                           side="BUY", lots=1, entry_price=80, stop_loss=75))
    assert not p["ok"] and any(c["name"] in ("not_halted", "max_daily_loss") and not c["passed"] for c in p["risk"])
    assert r.trade(tid)["status"] == L.POSITION_ACTIVE       # open trades keep their stops


def test_confirm_rechecks_risk(tmp_path):
    r = Rig(tmp_path)
    p = r.svc.preview(dict(underlying="NIFTY", expiry=EXPIRY.isoformat(), strike=25000, option_type="CE",
                           side="BUY", lots=1, entry_price=100, stop_loss=90))
    r.svc.halt("test halt")
    with pytest.raises(ActionError):
        r.svc.confirm(p["trade_id"], p["token"])
    assert r.places() == [] and r.trade(p["trade_id"])["status"] == L.EXPIRED


def test_broker_read_failure_takes_no_action(tmp_path):
    r = Rig(tmp_path)
    tid = _active(r)
    r.ex.fail_reads = 1
    r.price(130)
    res = r.tick()
    assert not res["ok"] and r.trade(tid)["status"] == L.POSITION_ACTIVE
    r.tick()
    assert r.trade(tid)["status"] == L.EXITED


def test_audit_trail_reconstructs_trade(tmp_path):
    r = Rig(tmp_path)
    tid = _active(r)
    r.price(130)
    r.tick()
    ev = r.events(tid)
    for e in ("TRADE_CREATED", "VALIDATION_PASSED", "RISK_CHECK", "ENTRY_CONFIRMED", "ENTRY_ORDER_PLACED",
              "ENTRY_EXECUTED", "SL_CREATED", "TARGET_REACHED", "EXIT_ORDER_PLACED", "TRADE_EXITED"):
        assert e in ev, e


def test_kite_refusal_reason_is_shown(tmp_path):
    r = Rig(tmp_path, order_lookup_grace_s=10)
    r.ex.raise_before_place = 1                 # e.g. PermissionException: No IPs configured for this app
    tid = r.open_trade()
    r.tick(5)
    r.tick(11)
    t = r.trade(tid)
    assert t["status"] == L.REJECTED and "simulated network error" in t["error"] and "never reached" in t["error"]


def test_manual_partial_exit_books_qty_and_resizes_sl(tmp_path):
    r = Rig(tmp_path)
    tid = _active(r, lots=3)                          # 195 qty total
    p = r.svc.prepare_partial_exit(tid, 65)            # book 1 of 3 lots
    assert p["ok"] if "ok" in p else True
    assert p["qty"] == 65 and p["open_qty"] == 195
    res = r.svc.confirm_partial_exit(tid, p["token"])
    assert res["ok"]
    t = r.trade(tid)
    assert t["status"] == L.POSITION_ACTIVE            # remainder stays open and managed
    assert t["pending_partial_qty"] is None             # cleared once the order was placed
    part = r.repo.orders(tid, "PARTIAL")[0]
    assert (part["quantity"], part["status"], part["purpose"]) == (65, "COMPLETE", "USER_PARTIAL_EXIT")
    t2 = r.trade(tid)
    assert (t2["open_qty"], t2["exited_qty"]) == (130, 65)
    sl = [o for o in r.repo.orders(tid, "SL") if is_working_status(o)]
    assert sl and sl[0]["quantity"] == 130              # SL resized to protect only what remains


def is_working_status(o):
    return o["status"] not in ("COMPLETE", "CANCELLED", "REJECTED", "CANCELLED AMO", "EXPIRED", "NOT_PLACED")


def test_prepare_partial_exit_rejects_bad_qty(tmp_path):
    r = Rig(tmp_path)
    tid = _active(r, lots=3)
    with pytest.raises(ActionError, match="multiple of the lot size"):
        r.svc.prepare_partial_exit(tid, 40)             # not a multiple of 65
    with pytest.raises(ActionError, match="less than the open quantity"):
        r.svc.prepare_partial_exit(tid, 195)             # the whole position - must use Exit instead
    with pytest.raises(ActionError, match="less than the open quantity"):
        r.svc.prepare_partial_exit(tid, 260)             # more than the whole position


def test_manual_partial_exit_blocked_once_a_full_exit_has_started(tmp_path):
    r = Rig(tmp_path, reconcile_confirmations=1)
    tid = _active(r, lots=3)
    r.svc.request_exit(tid, r.svc.prepare(tid, "EXIT")["token"])   # PAPER auto-fills: this may finish immediately
    with pytest.raises(ActionError):                              # "already in progress" or "nothing to exit"
        r.svc.prepare_partial_exit(tid, 65)


def test_manual_partial_exit_then_remaining_position_still_stops_out(tmp_path):
    r = Rig(tmp_path)
    tid = _active(r, lots=3, stop_loss=90)
    p = r.svc.prepare_partial_exit(tid, 65)
    r.svc.confirm_partial_exit(tid, p["token"])
    assert (r.trade(tid)["open_qty"], r.trade(tid)["status"]) == (130, L.POSITION_ACTIVE)
    r.price(89)                                          # breach the stop on the remaining 130
    r.tick()
    t = r.trade(tid)
    assert (t["status"], t["exit_reason"], t["open_qty"]) == (L.EXITED, L.STOP_LOSS_HIT, 0)
    assert t["exited_qty"] == 195                        # 65 booked manually + 130 stopped out = the whole position


def test_refresh_ltp_updates_last_ltp_and_unrealized_pnl(tmp_path):
    r = Rig(tmp_path)
    tid = _active(r)                    # entry_price=100, side BUY
    assert r.trade(tid)["last_ltp"] == 100    # set once by the entry fill tick
    r.quotes.set(SYM, 111)              # a new price the regular tick hasn't seen yet (no r.tick() call)
    res = r.svc.refresh_ltp(tid)
    assert res["last_ltp"] == 111
    assert res["unrealized_pnl"] == pytest.approx(11 * 65)


def test_refresh_ltp_rejects_closed_trade(tmp_path):
    r = Rig(tmp_path)
    tid = r.open_trade()
    r.svc.cancel_entry(tid, r.svc.prepare(tid, "CANCEL")["token"])
    assert r.trade(tid)["status"] == L.CANCELLED
    with pytest.raises(ActionError):
        r.svc.refresh_ltp(tid)


def test_pending_unfilled_entry_still_shows_a_live_ltp(tmp_path):
    r = Rig(tmp_path)
    r.price(90)                          # BUY limit 100 stays working: LTP 90 <= entry, wait - BUY fills when LTP<=limit
    tid = r.open_trade(entry_price=80, stop_loss=70, target=100)   # limit 80, LTP 90: BUY stays unfilled (90 > 80)
    r.tick()
    t = r.trade(tid)
    assert t["status"] == L.ENTRY_PENDING and t["filled_qty"] == 0
    assert t["last_ltp"] == 90           # shown even though nothing has filled
    assert t["unrealized_pnl"] in (None, 0)   # no P&L yet - nothing to compute it from


def test_refresh_ltp_works_on_a_pending_unfilled_trade(tmp_path):
    r = Rig(tmp_path)
    r.price(90)
    tid = r.open_trade(entry_price=80, stop_loss=70, target=100)
    assert r.trade(tid)["status"] == L.ENTRY_PENDING
    r.price(95)
    res = r.svc.refresh_ltp(tid)
    assert res["last_ltp"] == 95
