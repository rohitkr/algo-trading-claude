"""Hedged-variant overlay, margin estimates and trade -> OrderIntent conversion (offline)."""
from datetime import date, datetime

import pandas as pd
import pytest

from backtest.compare import markdown_table, summarize
from backtest.engine import CostModel, Trade, metrics
from backtest.hedged import HedgedStrategy, HedgeParams, combine_positions, position_id, wing_strike
from backtest.margin import MarginModel, max_loss, pair_legs, peak_margin
from backtest.signals import trades_to_intents
from strategy_signals import Action, LegRole

D = date(2026, 9, 1)
T0, T1 = datetime(2026, 9, 1, 11, 30), datetime(2026, 9, 1, 14, 0)


def closed_short(tid="PB-0901", right="PUT", strike=25100.0, entry=150.0, exit_=90.0, qty=325):
    t = Trade("S", tid, str(D), str(D), strike, right, "SELL", qty, T0, entry, spot_entry=25000.0)
    return t.close(T1, exit_, "expiry-day exit", CostModel(), 25050.0)


class FakeCal:
    def trading_days(self, a, b):
        return [a]


class FakeFeed:
    cal = FakeCal()

    def __init__(self, book):
        self.book, self.requests = book, []

    def option(self, expiry, strike, right, days):
        self.requests.append((expiry, strike, right))
        prices = self.book.get((strike, right))
        if prices is None:
            return pd.DataFrame(columns=["open", "high", "low", "close"])
        return pd.DataFrame({"close": prices}, index=pd.DatetimeIndex([T0, T1]))


class FakeBase:
    name, costs = "S", CostModel()

    def __init__(self, feed, trades):
        self.feed, self._trades, self.grid = feed, trades, {"x": 1}

    def run(self, start, end):
        return list(self._trades)


def test_wing_strike_is_further_otm():
    assert wing_strike(25100, "PUT", 200) == 24900
    assert wing_strike(24900, "CALL", 300) == 25200


def test_hedged_overlay_keeps_short_and_adds_wing():
    short = closed_short()
    feed = FakeFeed({(24900.0, "PUT"): [40.0, 20.0]})
    h = HedgedStrategy(FakeBase(feed, [short]), HedgeParams(200))
    trades = h.run(D, D)
    assert h.name == "S + 200pt wing" and h.grid == {"x": 1}
    s, w = trades
    assert (s.entry_ts, s.exit_ts, s.entry_price, s.exit_price, s.net_pnl) == \
           (short.entry_ts, short.exit_ts, short.entry_price, short.exit_price, short.net_pnl)
    assert s.strategy == h.name and short.strategy == "S"          # original untouched
    assert (w.side, w.strike, w.entry_ts, w.exit_ts, w.entry_price, w.exit_price) == \
           ("BUY", 24900, T0, T1, 40.0, 20.0)
    assert w.gross_pnl == pytest.approx(-20 * 325)
    assert position_id(w) == position_id(s) == "PB-0901"

    [pos] = combine_positions(trades)
    assert pos.net_pnl == pytest.approx(s.net_pnl + w.net_pnl)
    assert metrics(combine_positions(trades)).trades == 1


def test_missing_wing_is_reported_and_left_unhedged():
    h = HedgedStrategy(FakeBase(FakeFeed({}), [closed_short()]), HedgeParams(200))
    trades = h.run(D, D)
    assert len(trades) == 1 and h.missing[0]["missing"] == "entry"
    assert summarize("S", "hedged 200pt", trades)["unhedged_positions"] == 1


def test_margin_model_naked_vs_hedged():
    short = closed_short()
    wing = Trade("S", "PB-0901~W", str(D), str(D), 24900.0, "PUT", "BUY", 325, T0, 40.0).close(T1, 20.0, "x", CostModel())
    m = MarginModel(span_pct=9, exposure_pct=2)
    naked = m.book([(short, None)])
    hedged = m.book([(short, wing)])
    assert naked["margin"] == pytest.approx(0.11 * 25000 * 325)
    assert hedged["span"] == pytest.approx(200 * 325)
    assert hedged["capital"] == pytest.approx(200 * 325 + 0.02 * 25000 * 325 + 40 * 325)
    # a straddle only pays SPAN on the larger side
    call = closed_short("ZD-C", "CALL", 24900.0)
    assert m.book([(short, None), (call, None)])["span"] == pytest.approx(0.09 * 25000 * 325)
    assert peak_margin([short, wing])["capital"] == hedged["capital"]
    assert max_loss(short, None) is None
    assert max_loss(short, wing) == pytest.approx((200 - 110) * 325 + short.costs + wing.costs)
    assert pair_legs([short, wing]) == [(short, wing)]


def test_trades_to_intents_orders_legs_and_events():
    short = closed_short()
    wing = Trade("S", "PB-0901~W", str(D), str(D), 24900.0, "PUT", "BUY", 325, T0, 40.0).close(T1, 20.0, "x", CostModel())
    entry, exit_ = trades_to_intents([wing, short])
    assert (entry.action, exit_.action) == (Action.ENTRY, Action.EXIT)
    assert entry.position_id == exit_.position_id == "PB-0901"
    assert [l.role for l in entry.legs] == [LegRole.MAIN, LegRole.HEDGE] and entry.is_hedged
    assert entry.hedges[0].strike == 24900 and entry.legs[0].quantity == 325
    assert exit_.legs[1].ref_price == 20.0 and exit_.ts == T1


def test_summary_and_table_render():
    rows = [summarize("S", "naked", [closed_short()])]
    assert rows[0]["max_loss_per_trade"] is None and rows[0]["trades"] == 1
    assert "unbounded" in markdown_table(rows)


def test_strategy_side_never_imports_zerodha():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    for pkg in ("backtest", "strategy_signals", "trading_data"):
        for f in (root / pkg).rglob("*.py"):
            text = f.read_text()
            assert "import zerodha" not in text and "from zerodha" not in text, f
