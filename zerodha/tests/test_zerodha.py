"""Offline tests for the zerodha package (no network, no kiteconnect needed)."""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from strategy_signals import Action, LegRole, OptionLeg, OrderIntent  # noqa: E402
from zerodha import (Executor, InstrumentBook, KiteBroker, OrderManager, OrderRequest, PaperBroker,  # noqa: E402
                     ZerodhaConfig, estimate_basket_margin)
from zerodha.auth import (IST, KiteSession, LoginRequired, access_token, exchange_request_token,  # noqa: E402
                          extract_request_token, load_session, login_url, save_session)
from zerodha.orders import OrderFailed, round_to_tick, slice_quantity  # noqa: E402

EXP = date(2026, 10, 6)
LOT = 65


def row(strike, t, lot=LOT):
    sym = f"NIFTY26O06{strike}{t}"
    return {"instrument_token": strike * 10 + (1 if t == "CE" else 2), "exchange_token": "1", "tradingsymbol": sym,
            "name": "NIFTY", "last_price": 0, "expiry": EXP.isoformat(), "strike": str(strike), "tick_size": "0.05",
            "lot_size": str(lot), "instrument_type": t, "segment": "NFO-OPT", "exchange": "NFO"}


ROWS = [row(k, t) for k in range(24500, 25600, 50) for t in ("CE", "PE")] + [
    {**row(0, "CE"), "tradingsymbol": "NIFTY26OCTFUT", "instrument_type": "FUT", "strike": "0"}]


@pytest.fixture
def book():
    return InstrumentBook(ROWS)


def cfg(**kw):
    base = dict(api_key="k", api_secret="s", dry_run=False, fill_timeout_s=0, poll_interval_s=0, max_reprices=2)
    base.update(kw)
    return ZerodhaConfig(**base)


def prices(**overrides):
    p = {f"NFO:{r['tradingsymbol']}": 100.0 for r in ROWS}
    p.update({f"NFO:NIFTY26O06{k}": v for k, v in overrides.items()})
    return p


def put_spread(pid="P1", qty=325, action=Action.ENTRY):
    legs = (OptionLeg("NIFTY", EXP, 25100, "PUT", "SELL", qty, LegRole.MAIN),
            OptionLeg("NIFTY", EXP, 24900, "PUT", "BUY", qty, LegRole.HEDGE))
    return OrderIntent(f"{pid}:{action.value.lower()}", pid, action, legs, datetime(2026, 10, 1, 11, 30))


# -- config / auth ---------------------------------------------------------------------------
def test_config_defaults_are_safe():
    c = ZerodhaConfig.from_env(environ={}, env_file=None)
    assert c.dry_run is True and c.product == "NRML" and c.order_type == "LIMIT"
    assert "secret" not in repr(ZerodhaConfig(api_secret="secret", access_token="tok"))


def test_config_from_env_and_validation(tmp_path):
    f = tmp_path / ".env"
    f.write_text('KITE_API_KEY="abc"\nKITE_DRY_RUN=0\n# comment\nKITE_PRODUCT=mis\n')
    c = ZerodhaConfig.from_env(environ={"KITE_FREEZE_QTY": "975"}, env_file=f)
    assert (c.api_key, c.dry_run, c.product, c.freeze_qty) == ("abc", False, "MIS", 975)
    with pytest.raises(ValueError):
        ZerodhaConfig(product="CNC")


def test_login_url_and_request_token():
    assert login_url(cfg()) == "https://kite.zerodha.com/connect/login?v=3&api_key=k"
    assert extract_request_token("http://localhost:3000/?action=login&type=login&status=success&request_token=RT1") == "RT1"
    assert extract_request_token("RT2") == "RT2"
    with pytest.raises(ValueError):
        extract_request_token("http://localhost:3000/?status=cancelled&request_token=x")
    with pytest.raises(RuntimeError):
        login_url(ZerodhaConfig())


def test_session_expiry_and_storage(tmp_path):
    created = datetime(2026, 9, 28, 8, 0, tzinfo=IST)
    s = KiteSession("tok", "AB1234", created.isoformat())
    assert s.expires_at() == datetime(2026, 9, 29, 6, 0, tzinfo=IST)
    assert s.is_valid(created + timedelta(hours=12)) and not s.is_valid(created + timedelta(hours=23))
    early = KiteSession("tok", "AB1234", datetime(2026, 9, 28, 5, 0, tzinfo=IST).isoformat())
    assert early.expires_at() == datetime(2026, 9, 28, 6, 0, tzinfo=IST)

    path = tmp_path / "sess.json"
    save_session(path, s)
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert load_session(path) == s
    c = cfg(token_file=path)
    assert access_token(c, now=created + timedelta(hours=1)) == "tok"
    with pytest.raises(LoginRequired):
        access_token(c, now=created + timedelta(days=1))
    assert access_token(cfg(access_token="env-tok")) == "env-tok"


def test_exchange_request_token_with_fake_kite():
    class FakeKite:
        def generate_session(self, rt, api_secret):
            assert (rt, api_secret) == ("RT", "s")
            return {"access_token": "AT", "user_id": "AB1234"}
    s = exchange_request_token(cfg(), "RT", kite=FakeKite(), now=datetime(2026, 9, 28, 9, tzinfo=IST))
    assert (s.access_token, s.user_id) == ("AT", "AB1234")


# -- instruments -----------------------------------------------------------------------------
def test_instrument_lookup(book, tmp_path):
    i = book.option("NIFTY", EXP, 25100, "PUT")
    assert (i.tradingsymbol, i.lot_size, i.tick_size, i.key) == ("NIFTY26O0625100PE", 65, 0.05, "NFO:NIFTY26O0625100PE")
    assert len(book) == 44 and book.expiries("NIFTY") == [EXP]
    with pytest.raises(KeyError, match="nearest strikes"):
        book.option("NIFTY", EXP, 25125, "PUT")

    class FakeKite:
        calls = 0
        def instruments(self, exchange):
            FakeKite.calls += 1
            return ROWS
    b1 = InstrumentBook.from_kite(FakeKite(), cache_dir=tmp_path, today=date(2026, 10, 1))
    b2 = InstrumentBook.from_kite(FakeKite(), cache_dir=tmp_path, today=date(2026, 10, 1))
    assert FakeKite.calls == 1 and len(b1) == len(b2) == 44


# -- orders ----------------------------------------------------------------------------------
def test_tick_rounding_and_slicing():
    assert round_to_tick(101.02, 0.05, "BUY") == 101.05
    assert round_to_tick(101.02, 0.05, "SELL") == 101.0
    assert slice_quantity(325, 65, 1800) == [325]
    assert slice_quantity(1950, 65, 1800) == [1755, 195]
    with pytest.raises(ValueError):
        slice_quantity(100, 65, 1800)


def test_limit_order_reprices_until_filled(book):
    ticks = iter([100.0, 104.0, 104.0, 104.0])          # price runs away after the first quote
    broker = PaperBroker(price_fn=lambda k: 104.0)
    om = OrderManager(broker, cfg(limit_buffer_pct=1.0), sleep=lambda s: None)
    broker.price_fn = lambda k: next(ticks, 104.0)
    fills = om.execute(book.option("NIFTY", EXP, 24900, "PUT"), "BUY", 65)
    assert fills[0].status == "COMPLETE" and any(e[0] == "modify" for e in broker.log)


def test_unfilled_order_is_cancelled(book):
    broker = PaperBroker(prices=prices())
    om = OrderManager(broker, cfg(limit_buffer_pct=-5.0), sleep=lambda s: None)   # never marketable
    with pytest.raises(OrderFailed):
        om.execute(book.option("NIFTY", EXP, 24900, "PUT"), "BUY", 65)
    assert broker.log[-1][0] == "cancel"


# -- executor --------------------------------------------------------------------------------
def test_entry_buys_hedge_first_and_exit_closes_it_last(book):
    broker = PaperBroker(prices=prices())
    ex = Executor(broker, book, cfg())
    rep = ex.handle(put_spread())
    assert rep.ok and [p.role for p in rep.plan] == ["HEDGE", "MAIN"]
    placed = [(e[2], e[3]) for e in broker.log if e[0] == "place"]
    assert placed == [("BUY", "NIFTY26O0624900PE"), ("SELL", "NIFTY26O0625100PE")]
    assert broker.positions == {"NIFTY26O0624900PE": 325, "NIFTY26O0625100PE": -325}

    rep = ex.handle(put_spread(action=Action.EXIT))
    assert rep.ok
    placed = [(e[2], e[3]) for e in broker.log if e[0] == "place"][2:]
    assert placed == [("BUY", "NIFTY26O0625100PE"), ("SELL", "NIFTY26O0624900PE")]
    assert all(q == 0 for q in broker.positions.values()) and not ex.positions


def test_rejected_hedge_means_no_short_is_sold(book):
    broker = PaperBroker(prices=prices(), reject_symbols={"NIFTY26O0624900PE"})
    rep = Executor(broker, book, cfg()).handle(put_spread())
    assert not rep.ok and "no short was sold" in rep.message
    assert not any(e[0] == "place" and e[2] == "SELL" for e in broker.log)


def test_rejected_main_leg_unwinds_hedge(book):
    broker = PaperBroker(prices=prices(), reject_symbols={"NIFTY26O0625100PE"})
    rep = Executor(broker, book, cfg()).handle(put_spread())
    assert not rep.ok and len(rep.unwound) == 1 and rep.unwound[0].side == "SELL"
    assert all(q == 0 for q in broker.positions.values())


def test_failed_main_exit_keeps_hedge(book):
    broker = PaperBroker(prices=prices())
    ex = Executor(broker, book, cfg())
    ex.handle(put_spread())
    broker.reject_symbols.add("NIFTY26O0625100PE")
    rep = ex.handle(put_spread(action=Action.EXIT))
    assert not rep.ok and "hedges kept open" in rep.message
    assert broker.positions["NIFTY26O0624900PE"] == 325
    assert [o.leg.role for o in ex.positions["P1"]] == [LegRole.MAIN, LegRole.HEDGE]


def test_margin_check_blocks_entry(book):
    broker = PaperBroker(prices=prices(), funds=50_000,
                         margin_fn=lambda reqs: estimate_basket_margin(reqs, book, spot=25000))
    rep = Executor(broker, book, cfg()).handle(put_spread())
    assert not rep.ok and "insufficient margin" in rep.message
    assert not any(e[0] == "place" for e in broker.log)


def test_dry_run_places_nothing_and_is_idempotent(book):
    broker = PaperBroker(prices=prices())
    ex = Executor(broker, book, cfg(dry_run=True))
    rep = ex.handle(put_spread())
    assert rep.ok and rep.dry_run and len(rep.plan) == 2 and not broker.log
    live = Executor(PaperBroker(prices=prices()), book, cfg())
    assert live.handle(put_spread()) is live.handle(put_spread())


def test_estimated_margin_hedged_far_below_naked(book):
    sell = OrderRequest("NIFTY26O0625100PE", "SELL", 325, price=150)
    buy = OrderRequest("NIFTY26O0624900PE", "BUY", 325, price=40)
    naked = estimate_basket_margin([sell], book, spot=25000)
    hedged = estimate_basket_margin([sell, buy], book, spot=25000)
    assert naked == pytest.approx(0.11 * 25000 * 325)
    assert hedged == pytest.approx(200 * 325 + 0.02 * 25000 * 325 + 40 * 325)
    assert hedged < naked / 2


# -- live adapter with a fake KiteConnect ------------------------------------------------------
def test_kite_broker_maps_calls():
    calls = []

    class FakeKite:
        def place_order(self, **kw):
            calls.append(("place", kw)); return 123
        def modify_order(self, **kw):
            calls.append(("modify", kw)); return 123
        def cancel_order(self, **kw):
            calls.append(("cancel", kw)); return 123
        def order_history(self, oid):
            return [{"status": "OPEN"}, {"status": "COMPLETE", "filled_quantity": 65, "average_price": 101.5}]
        def ltp(self, keys):
            return {k: {"last_price": 99.5} for k in keys}
        def basket_order_margins(self, orders, consider_positions=True, mode=None):
            calls.append(("basket", orders, mode)); return {"initial": {"total": 900}, "final": {"total": 300}}
        def margins(self, segment):
            return {"net": 5000.0}

    b = KiteBroker(FakeKite())
    req = OrderRequest("NIFTY26O0625100PE", "SELL", 65, price=100.0, tag="algo")
    assert b.place_order(req) == "123"
    assert calls[0][1] == {"variety": "regular", "exchange": "NFO", "tradingsymbol": "NIFTY26O0625100PE",
                           "transaction_type": "SELL", "quantity": 65, "product": "NRML", "order_type": "LIMIT",
                           "price": 100.0, "tag": "algo"}
    b.modify_order("123", price=98.0)
    assert calls[1][1] == {"variety": "regular", "order_id": "123", "price": 98.0}
    st = b.order_status("123")
    assert (st.status, st.filled_quantity, st.average_price, st.done) == ("COMPLETE", 65, 101.5, True)
    assert b.ltp(["NFO:X"]) == {"NFO:X": 99.5}
    assert b.basket_margin([req]) == 300.0 and calls[-1][2] == "compact"
    assert b.available_margin() == 5000.0


# -- packaging -------------------------------------------------------------------------------
def test_package_is_self_contained():
    """zerodha must import without kiteconnect and without any other repo package except strategy_signals."""
    code = ("import sys; sys.modules['kiteconnect'] = None; import zerodha, zerodha.__main__; "
            "bad = sorted(m for m, v in sys.modules.items() if v is not None and m.split('.')[0] in "
            "('backtest', 'trading_data', 'kiteconnect', 'pandas', 'duckdb')); print(bad)")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"
    for f in (ROOT / "zerodha").glob("*.py"):
        text = f.read_text()
        assert "from backtest" not in text and "import trading_data" not in text and "from trading_data" not in text
