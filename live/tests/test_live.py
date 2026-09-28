"""Offline tests for the live engine (no network, no Breeze, no Kite)."""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backtest.data import DataFeed  # noqa: E402
from backtest.strategies import RangeBreakoutParams, ZeroDteParams  # noqa: E402
from live.audit import AuditLog  # noqa: E402
from live.config import EngineConfig  # noqa: E402
from live.engine import TradingEngine  # noqa: E402
from live.interfaces import Signal  # noqa: E402
from live.risk import RiskContext, RiskManager  # noqa: E402
from live.selection import ContractSelector  # noqa: E402
from live.state import EngineState  # noqa: E402
from live.strategies import PositionalBreakout, ZeroDteStraddle  # noqa: E402
from strategy_signals import Action, LegRole, OptionLeg, OrderIntent  # noqa: E402
from trading_data.config import load_settings  # noqa: E402
from trading_data.storage import OptionContract  # noqa: E402
from zerodha import PaperBroker, ZerodhaConfig  # noqa: E402
from zerodha.execution import LiveTradingNotEnabled, ZerodhaExecutionBroker, build_live_broker  # noqa: E402
from zerodha.instruments import SyntheticInstrumentBook  # noqa: E402

MON, TUE = date(2026, 9, 28), date(2026, 9, 29)          # TUE is a NIFTY weekly expiry
M = timedelta(minutes=1)
FAKE_ENV = {"BREEZE_API_KEY": "k", "BREEZE_API_SECRET": "s", "ICICI_USER_ID": "u", "ICICI_PASSWORD": "p"}


@pytest.fixture(scope="module")
def selector():
    settings = load_settings(env_path=None, environ=dict(FAKE_ENV))
    return ContractSelector(DataFeed(settings, None, "NIFTY"))


def day_bars(d: date, path: dict[time, float], default: float) -> pd.DataFrame:
    """1-minute bars 09:15-15:29; `path` sets the close from that minute on (step function)."""
    rows, px = [], default
    t = datetime.combine(d, time(9, 15))
    while t.time() <= time(15, 29):
        px = path.get(t.time(), px)
        rows.append((t, px, px + 1, px - 1, px))
        t += M
    return pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close"]).set_index("ts")


class FakeMarket:
    """Spot bars per day; option price = intrinsic + 50 at the latest completed spot close."""

    def __init__(self, spot: dict[date, pd.DataFrame], option_frames: dict | None = None):
        self.spot, self.option_frames = spot, option_frames or {}
        self.errors = 0

    def spot_bars(self, day, now):
        df = self.spot.get(day)
        return df[df.index + M <= now] if df is not None else pd.DataFrame(columns=["open", "high", "low", "close"])

    def _spot_now(self, now):
        for d in sorted(self.spot, reverse=True):
            done = self.spot_bars(d, now)
            if len(done):
                return float(done["close"].iloc[-1])
        return None

    def option_bars(self, c, day, now):
        df = self.option_frames.get((c.strike, c.right, day))
        if df is None:
            return pd.DataFrame(columns=["open", "high", "low", "close"])
        return df[df.index + M <= now]

    def option_price(self, c, now, fresh=False):
        bars = self.option_bars(c, now.date(), now)
        if len(bars):
            return float(bars["close"].iloc[-1])
        s = self._spot_now(now)
        if s is None:
            return None
        intrinsic = max(0.0, c.strike - s) if c.right == "PUT" else max(0.0, s - c.strike)
        return round(intrinsic + 50, 2)

    def api_budget_remaining(self):
        return None


def ctx(market, selector, now):
    from live.interfaces import StrategyContext
    return StrategyContext(now, market, selector)


def poll_through(strat, market, selector, start: datetime, end: datetime, confirm=True):
    out, t = [], start
    while t <= end:
        sigs = strat.on_poll(ctx(market, selector, t))
        for s in sigs:
            if s.action is Action.ENTRY:
                strat.on_entry_result(s, confirm)
        out += [(t, s) for s in sigs]
        t += M
    return out


# -- strategies ------------------------------------------------------------------------------
def positional_market():
    mon = day_bars(MON, {time(9, 15): 25000, time(9, 30): 25100, time(9, 45): 25050,
                         time(11, 30): 25120,          # close above the 25101 high -> UP
                         time(13, 0): 24990,           # <= 25120 * 0.995 = 24994.4 -> stop
                         time(14, 0): 25125},          # back at the entry level -> re-entry
                   25050)
    tue = day_bars(TUE, {}, 25200)
    return FakeMarket({MON: mon, TUE: tue})


def test_positional_breakout_stop_reentry_and_expiry_exit(selector):
    m = positional_market()
    s = PositionalBreakout(RangeBreakoutParams())
    sigs = poll_through(s, m, selector, datetime.combine(MON, time(9, 16)), datetime.combine(TUE, time(15, 30)))
    kinds = [(x.action.value, x.position_id, x.ts.strftime("%m-%d %H:%M"), x.reason.split(":")[0]) for _, x in sigs]
    assert kinds == [("ENTRY", "PB-20260928", "09-28 11:30", "close above 2h range [24999.00, 25101.00]"),
                     ("EXIT", "PB-20260928", "09-28 13:00", "stop"),
                     ("ENTRY", "PB-20260928R", "09-28 14:00", "re-entry at cost"),
                     ("EXIT", "PB-20260928R", "09-29 15:15", "expiry-day exit")]
    entry = sigs[0][1]
    assert (entry.contract.right, entry.contract.strike, entry.contract.expiry) == ("PUT", 25200.0, TUE)
    assert entry.stop["basis"] == "spot" and entry.stop["level"] == pytest.approx(25120 * 0.995, abs=0.01)
    assert sigs[0][0] == datetime.combine(MON, time(11, 31))      # acted on once the 11:30 bar completed


def test_positional_refused_entry_leaves_no_position(selector):
    m = positional_market()
    s = PositionalBreakout(RangeBreakoutParams())
    sigs = poll_through(s, m, selector, datetime.combine(MON, time(9, 16)), datetime.combine(MON, time(15, 30)),
                        confirm=False)
    assert [x.action for _, x in sigs] == [Action.ENTRY]          # no stop / re-entry for a refused entry
    assert s.get_state()["pos"] is None


def test_positional_state_survives_restart(selector):
    m = positional_market()
    s = PositionalBreakout(RangeBreakoutParams())
    poll_through(s, m, selector, datetime.combine(MON, time(9, 16)), datetime.combine(MON, time(11, 40)))
    s2 = PositionalBreakout(RangeBreakoutParams())
    s2.set_state(json.loads(json.dumps(s.get_state())))
    sigs = poll_through(s2, m, selector, datetime.combine(MON, time(11, 41)), datetime.combine(MON, time(13, 5)))
    assert [(x.action, x.reason.split(":")[0]) for _, x in sigs] == [(Action.EXIT, "stop")]


def zerodte_market():
    spot = day_bars(TUE, {}, 25030)                           # ATM 25050: CALL 24950, PUT 25150
    call = day_bars(TUE, {}, 120.0)
    put = day_bars(TUE, {time(12, 0): 160.0, time(12, 30): 118.0}, 120.0)   # 12:00 high 161 >= 156 -> stop
    frames = {(24950.0, "CALL", TUE): call, (25150.0, "PUT", TUE): put}
    return FakeMarket({TUE: spot}, frames)


def test_zerodte_entry_stop_reentry_time_exit(selector):
    m = zerodte_market()
    s = ZeroDteStraddle(ZeroDteParams(), lambda d: (time(11, 30), {"source": "test"}), quote_stops=False)
    sigs = poll_through(s, m, selector, datetime.combine(TUE, time(9, 16)), datetime.combine(TUE, time(15, 20)))
    got = [(x.action.value, x.position_id, x.ts.strftime("%H:%M"), x.reason.split(" (")[0]) for _, x in sigs]
    assert got == [("ENTRY", "ZD-20260929-1130-C", "11:30", "ITM 100 pts, entry 11:30"),
                   ("ENTRY", "ZD-20260929-1130-P", "11:30", "ITM 100 pts, entry 11:30"),
                   ("EXIT", "ZD-20260929-1130-P", "12:00", "stop: premium +30%"),
                   ("ENTRY", "ZD-20260929-1130-PR", "12:30", "re-entry at cost"),
                   ("EXIT", "ZD-20260929-1130-C", "15:15", "time exit 15:15"),
                   ("EXIT", "ZD-20260929-1130-PR", "15:15", "time exit 15:15")]
    stop = [x for _, x in sigs if x.action is Action.EXIT][0]
    assert stop.ref_price == pytest.approx(max(120 * 1.3, 160.0))      # gap through the stop: filled at the open
    assert not s.on_poll(ctx(m, selector, datetime.combine(MON, time(11, 31))))   # not an expiry day


def test_zerodte_refuses_without_history(selector):
    s = ZeroDteStraddle(ZeroDteParams(), lambda d: (None, {"reason": "not enough stored history"}))
    assert s.on_poll(ctx(zerodte_market(), selector, datetime.combine(TUE, time(11, 31)))) == []
    assert s.notes[0]["decision"] == "no_trade_day"


# -- risk ------------------------------------------------------------------------------------
def rc(**kw):
    base = dict(now=datetime.combine(MON, time(11, 31)), session=(time(9, 15), time(15, 29)), halted=None,
                data_lag_s=5, api_budget=4000, open_positions=0, trades_today=0, daily_pnl=0.0,
                reentries_for_root=0, signal_blocked=None, already_sent=False, contract_already_open=False, lot_size=65,
                risk_per_unit=125.0, margin_per_lot=180_000.0, available_margin=5_000_000.0)
    base.update(kw)
    return RiskContext(**base)


def sig(**kw):
    c = OptionContract("NIFTY", "NFO", TUE, 25200, "PUT")
    base = dict(strategy="positional", action=Action.ENTRY, position_id="PB-1", contract=c,
                ts=datetime.combine(MON, time(11, 30)), spot=25120.0, reason="t", lots=5, ref_price=150.0,
                stop={"basis": "spot", "level": 24994.4})
    base.update(kw)
    return Signal(**base)


def test_risk_sizing_and_limits():
    cfg = EngineConfig(max_risk_per_trade=30_000, capital=10_000_000)
    d = RiskManager(cfg).evaluate_entry(sig(), rc())
    assert d.ok and d.lots == 3 and d.metrics["binding_limit"] == "max_risk"      # 125 x 65 = 8125/lot
    d = RiskManager(EngineConfig(capital=500_000)).evaluate_entry(sig(), rc())
    assert d.lots == 2 and d.metrics["binding_limit"] == "margin"                  # 400k usable / 180k
    d = RiskManager(EngineConfig(max_risk_per_trade=1000)).evaluate_entry(sig(), rc())
    assert not d.ok and "position_size" in [c.name for c in d.failed]


@pytest.mark.parametrize("kw,check", [
    (dict(daily_pnl=-60_000), "daily_loss"), (dict(trades_today=6), "max_trades_per_day"),
    (dict(open_positions=3), "max_open_positions"), (dict(already_sent=True), "duplicate_intent"),
    (dict(contract_already_open=True), "duplicate_contract"), (dict(halted="x"), "not_halted"),
    (dict(now=datetime.combine(MON, time(15, 40))), "market_hours"), (dict(data_lag_s=900), "data_fresh"),
    (dict(api_budget=10), "api_budget"), (dict(session=None), "market_hours"),
    (dict(margin_per_lot=None), "margin_known"),
])
def test_risk_blocks(kw, check):
    d = RiskManager(EngineConfig()).evaluate_entry(sig(), rc(**kw))
    assert not d.ok and check in [c.name for c in d.failed]


def test_risk_reentry_limit_and_stale_signal():
    r = RiskManager(EngineConfig())
    assert "reentry_limit" in [c.name for c in r.evaluate_entry(sig(is_reentry=True), rc(reentries_for_root=1)).failed]
    old = sig(ts=datetime.combine(MON, time(11, 0)))
    assert "signal_fresh" in [c.name for c in r.evaluate_entry(old, rc()).failed]


# -- engine (paper execution through the real Zerodha order path) ------------------------------
def pcfg(**kw):
    """PAPER config for the single-instance tests: intraday + naked (explicit, since positional now
    defaults to HEDGED and holding to expiry)."""
    base = dict(mode="PAPER", intraday_only=True, position_mode="NAKED")
    base.update(kw)
    return EngineConfig(**base)


class Harness:
    def __init__(self, tmp_path, selector, market, cfg=None, strategies=None):
        self.cfg = cfg or pcfg(kill_file=tmp_path / "KILL", state_dir=tmp_path,
                                       audit_dir=tmp_path / "audit", capital=10_000_000, paper_slippage_points=0)
        self.now = datetime.combine(MON, time(9, 0))
        self.book = SyntheticInstrumentBook(65)
        self.paper = PaperBroker(funds=self.cfg.capital, price_fn=self._price, margin_fn=lambda reqs: 150_000.0,
                                 slippage=self.cfg.paper_slippage_points)
        zc = ZerodhaConfig(dry_run=False, fill_timeout_s=0, poll_interval_s=0, max_reprices=1)
        self.broker = ZerodhaExecutionBroker(self.paper, self.book, zc, live=False)
        self.market, self.selector = market, selector
        self.state = EngineState.load(tmp_path / "state.json")
        intraday = self.cfg.force_exit_time if self.cfg.intraday_only else None
        self.engine = TradingEngine(self.cfg, market, self.broker, selector,
                                    strategies or [PositionalBreakout(RangeBreakoutParams(), intraday_exit=intraday)],
                                    RiskManager(self.cfg), self.state, AuditLog(self.cfg.audit_dir, "PAPER"))

    def _price(self, key):
        i = self.book.by_symbol(key.split(":")[1])
        return self.market.option_price(self.selector.contract(i.expiry, i.strike, i.right), self.now)

    def run(self, start: datetime, end: datetime):
        self.now = start
        while self.now <= end:
            self.engine.step(self.now)
            self.now += M

    def audit(self, event=None):
        recs = [json.loads(l) for p in sorted(self.cfg.audit_dir.glob("*.jsonl")) for l in p.read_text().splitlines()]
        return [r for r in recs if event is None or r["event"] == event]


def test_engine_paper_round_trip_with_audit(tmp_path, selector):
    h = Harness(tmp_path, selector, positional_market())
    h.run(datetime.combine(MON, time(9, 16)), datetime.combine(TUE, time(15, 20)))
    closed = h.audit("position_closed")
    assert [c["position_id"] for c in closed] == ["PB-20260928", "PB-20260928R"]
    first = closed[0]
    assert first["exit_reason"].startswith("stop") and first["quantity"] == 325 and first["side"] == "SELL"
    assert first["entry_price"] == 130.0 and first["exit_price"] == 260.0          # 25200 PUT: intrinsic + 50
    assert first["gross_pnl"] == pytest.approx((130.0 - 260.0) * 325)
    opened = h.audit("position_opened")[0]
    for field in ("underlying_price", "contract", "strike", "expiry", "entry_price", "stop", "quantity", "order_ids"):
        assert opened[field] not in (None, [], "")
    checks = h.audit("risk_check")[0]["checks"]
    assert all(c["passed"] for c in checks) and len(checks) >= 15
    orders = h.audit("order")
    assert orders[0]["fills"][0]["order_ids"] and orders[0]["fills"][0]["statuses"] == ["COMPLETE"]
    assert not h.state.positions and h.paper.positions() == {}
    assert h.state.halted is None
    assert (tmp_path / "audit" / "trades_paper.csv").read_text().count("\n") == 3


def test_engine_hedged_buys_wing_first_and_reports_max_loss(tmp_path, selector):
    cfg = pcfg(position_mode="HEDGED", hedge_width=200, kill_file=tmp_path / "KILL",
                       state_dir=tmp_path, audit_dir=tmp_path / "audit", capital=10_000_000, paper_slippage_points=0)
    h = Harness(tmp_path, selector, positional_market(), cfg)
    h.run(datetime.combine(MON, time(9, 16)), datetime.combine(MON, time(11, 32)))
    places = [e for e in h.paper.log if e[0] == "place"]
    assert [(e[2], e[3]) for e in places] == [("BUY", "NIFTY260929P25000"), ("SELL", "NIFTY260929P25200")]
    pos = h.state.positions["PB-20260928"]
    assert pos["hedge"]["net_credit"] == pytest.approx(130.0 - 50.0)
    assert pos["hedge"]["max_loss"] == pytest.approx((200 - 80.0) * 325)
    assert h.paper.positions() == {"NIFTY260929P25000": 325, "NIFTY260929P25200": -325}


def test_engine_rejected_entry_opens_nothing(tmp_path, selector):
    h = Harness(tmp_path, selector, positional_market())
    h.paper.reject_symbols.add("NIFTY260929P25200")
    h.run(datetime.combine(MON, time(9, 16)), datetime.combine(MON, time(15, 0)))
    assert not h.state.positions and h.audit("order")[0]["ok"] is False
    assert h.audit("position_closed") == [] and h.paper.positions() == {}


def test_engine_partial_fill_is_unwound(tmp_path, selector):
    h = Harness(tmp_path, selector, positional_market())
    h.paper.max_fill_qty = 130                      # 2 of 5 lots fill, then the order is cancelled
    h.run(datetime.combine(MON, time(9, 16)), datetime.combine(MON, time(11, 32)))
    order = h.audit("order")[0]
    assert order["ok"] is False and order["unwound"] and not h.state.positions
    assert h.paper.positions() == {}


def test_engine_kill_switch_squares_off(tmp_path, selector):
    h = Harness(tmp_path, selector, positional_market())
    h.run(datetime.combine(MON, time(9, 16)), datetime.combine(MON, time(11, 35)))
    assert h.state.positions
    h.cfg.kill_file.write_text("stop")
    h.run(datetime.combine(MON, time(11, 36)), datetime.combine(MON, time(11, 40)))
    assert h.engine.stopped and not h.state.positions and h.paper.positions() == {}
    assert h.state.halted.startswith("square-off") and h.audit("square_off")


def test_engine_position_changed_outside_is_unmanaged_and_halts(tmp_path, selector):
    h = Harness(tmp_path, selector, positional_market())
    h.run(datetime.combine(MON, time(9, 16)), datetime.combine(MON, time(11, 35)))
    h.paper.net["NIFTY260929P25200"] = -650          # someone doubled the position in Kite
    res = h.engine.reconcile(datetime.combine(MON, time(11, 36)))
    assert res["pending_confirmation"] == ["default:PB-20260928"] and h.state.halted is None   # 1st sighting: wait
    orders = len(h.paper.log)
    h.run(datetime.combine(MON, time(11, 37)), datetime.combine(MON, time(15, 20)))
    assert h.state.halted.startswith("reconciliation: PB-20260928")
    assert "PB-20260928" not in h.state.positions and h.state.blocked["PB-20260928"] == "UNMANAGED"
    assert any(u["broker_qty"] == -650 for u in h.state.unmanaged.values())
    assert len(h.paper.log) == orders                # no stop, no re-entry, no force exit: never touched again
    assert h.paper.net["NIFTY260929P25200"] == -650


def test_engine_daily_loss_halts_new_entries(tmp_path, selector):
    cfg = pcfg(kill_file=tmp_path / "KILL", state_dir=tmp_path, audit_dir=tmp_path / "audit",
                       capital=10_000_000, paper_slippage_points=0, max_daily_loss=10_000)
    h = Harness(tmp_path, selector, positional_market(), cfg)
    h.run(datetime.combine(MON, time(9, 16)), datetime.combine(MON, time(15, 0)))
    assert h.state.halted.startswith("max daily loss")          # the stop lost 42,250
    assert [c["position_id"] for c in h.audit("position_closed")] == ["PB-20260928"]   # no re-entry
    blocked = h.audit("risk_check")[-1]
    assert not blocked["ok"] and any(c["check"] == "not_halted" and not c["passed"] for c in blocked["checks"])


def test_engine_state_restart_keeps_overnight_position_and_duplicate_guard(tmp_path, selector):
    h = Harness(tmp_path, selector, positional_market())
    h.run(datetime.combine(MON, time(9, 16)), datetime.combine(MON, time(11, 35)))
    h2 = Harness(tmp_path, selector, positional_market())
    assert "PB-20260928" in h2.state.positions and h2.state.was_sent("PB-20260928:entry")
    h2.book.option("NIFTY", TUE, 25200, "PUT")
    h2.paper.net.update({"NIFTY260929P25200": -325})     # the exchange still holds it
    h2.engine.strategies[0].set_state(h.state.strategies["positional"])
    h2.run(datetime.combine(MON, time(11, 36)), datetime.combine(MON, time(13, 5)))
    assert [c["exit_reason"].split(":")[0] for c in h2.audit("position_closed")] == ["stop"]


# -- safety switches -------------------------------------------------------------------------
def test_defaults_are_paper_and_unarmed(tmp_path):
    cfg = EngineConfig.from_env(env_file=None, environ={})
    assert cfg.mode == "PAPER" and not cfg.enable_live_trading and not cfg.live_armed
    assert cfg.strategy == "positional" and cfg.position_mode == "HEDGED" and cfg.hedge_width == 300
    assert cfg.carry_allowed                                             # positional holds to expiry by default
    z = EngineConfig.from_env(env_file=None, environ={"STRATEGY": "zerodte"})
    assert z.position_mode == "NAKED" and z.intraday_only
    with pytest.raises(ValueError):
        EngineConfig(mode="REAL")
    c = EngineConfig.from_env(env_file=None, environ={"POSITION_MODE": "hedged", "HEDGE_WIDTH": "300",
                                                       "LOT_SIZE": "75", "RISK_MAX_DAILY_LOSS": "12345 # note"})
    assert (c.position_mode, c.hedge_width, c.lot_size, c.max_daily_loss) == ("HEDGED", 300, 75, 12345.0)


def test_live_mode_is_not_wired_and_live_broker_needs_every_switch():
    from live.app import LiveModeNotWired, build
    with pytest.raises(LiveModeNotWired):
        build(EngineConfig(mode="LIVE", enable_live_trading=True))
    zc = ZerodhaConfig(api_key="k", dry_run=False)
    for kw in (dict(trading_mode="PAPER", enable_live_trading=True, cli_confirmed=True),
               dict(trading_mode="LIVE", enable_live_trading=False, cli_confirmed=True),
               dict(trading_mode="LIVE", enable_live_trading=True, cli_confirmed=False)):
        with pytest.raises(LiveTradingNotEnabled):
            build_live_broker(zc, price_fn=lambda k: {}, kite=object(), **kw)
    with pytest.raises(LiveTradingNotEnabled):
        build_live_broker(ZerodhaConfig(api_key="k"), trading_mode="LIVE", enable_live_trading=True,
                          cli_confirmed=True, price_fn=lambda k: {}, kite=object())      # KITE_DRY_RUN defaults to 1


def test_paper_adapter_refuses_kite_broker():
    from zerodha import KiteBroker
    with pytest.raises(ValueError):
        ZerodhaExecutionBroker(KiteBroker(object()), SyntheticInstrumentBook(65), ZerodhaConfig(), live=False)


def test_dry_run_plan_never_counts_as_open(selector):
    book = SyntheticInstrumentBook(65)
    paper = PaperBroker(prices={}, price_fn=lambda k: 100.0)
    b = ZerodhaExecutionBroker(paper, book, ZerodhaConfig(dry_run=True), live=False)
    leg = OptionLeg("NIFTY", TUE, 25200, "PUT", "SELL", 65, LegRole.MAIN)
    res = b.execute(OrderIntent("x:entry", "x", Action.ENTRY, (leg,)))
    assert not res.ok and "DRY_RUN" in res.message and paper.log == []


# -- Breeze market data (fake client) ----------------------------------------------------------
class FakeBudget:
    def remaining(self):
        return 1234


class FakeClient:
    def __init__(self, rows):
        self.rows, self.calls, self.budget = rows, [], FakeBudget()

    def fetch_candles(self, req, start, end):
        self.calls.append((req.stock_code, start, end))
        return [r for r in self.rows if start <= datetime.fromisoformat(r["datetime"]) <= end]

    def get_quote(self, req):
        return {"ltp": "123.45", "exchange_code": "NFO"}


def test_breeze_market_data_completed_bars_incremental_and_quotes():
    from trading_data.breeze.live import BreezeMarketData
    settings = load_settings(env_path=None, environ=dict(FAKE_ENV))
    rows = [{"datetime": f"2026-09-28 {h:02d}:{m:02d}:00", "open": 1, "high": 2, "low": 0.5, "close": 1.5,
             "volume": 0, "open_interest": 0} for h, m in [(9, 15), (9, 16), (9, 17), (9, 18)]]
    fc, clock = FakeClient(rows), [0.0]
    md = BreezeMarketData(fc, settings, "NIFTY", min_refetch_s=5, monotonic=lambda: clock[0])
    bars = md.spot_bars(MON, datetime(2026, 9, 28, 9, 17, 30))
    assert list(bars.index.strftime("%H:%M")) == ["09:15", "09:16"]          # 09:17 is still forming
    clock[0] = 10
    bars = md.spot_bars(MON, datetime(2026, 9, 28, 9, 19, 5))
    assert list(bars.index.strftime("%H:%M")) == ["09:15", "09:16", "09:17", "09:18"]
    assert fc.calls[1][1] == datetime(2026, 9, 28, 9, 17)                  # only asks for what it lacks
    n = len(fc.calls)
    md.spot_bars(MON, datetime(2026, 9, 28, 9, 19, 8))                       # nothing new is due: no call
    assert len(fc.calls) == n
    c = OptionContract("NIFTY", "NFO", TUE, 25200, "PUT")
    assert md.option_price(c, datetime(2026, 9, 28, 9, 19), fresh=True) == 123.45
    assert md.api_budget_remaining() == 1234


# -- parity with the backtest on stored data (skipped without DuckDB) -----------------------------
DB = ROOT / "data" / "market_data.duckdb"


@pytest.mark.skipif(not DB.exists(), reason="needs data/market_data.duckdb")
def test_replay_matches_backtest_on_stored_data(tmp_path):
    import subprocess
    out = subprocess.run([sys.executable, "-m", "live", "replay", "--start", "2026-08-24", "--end", "2026-09-10",
                          "--parity"], cwd=ROOT, capture_output=True, text=True, timeout=600)
    assert out.returncode == 0, out.stderr[-2000:]
    start = out.stdout.index('{\n  "live_positions"')
    summary = json.loads(out.stdout[start:out.stdout.index("}", start) + 1])
    assert summary["matched"] >= summary["backtest_trades"] - 1
    assert summary["same_exit_minute"] == summary["matched"] == summary["same_exit_reason"]


# -- round 2: intraday, manual exits, broker-truth reconciliation, profit cap ----------------------
def sym_net(h, strike=25200, right="PUT", expiry=TUE):
    return h.paper.net.get(h.book.option("NIFTY", expiry, strike, right).tradingsymbol, 0)


def test_intraday_force_exit_and_no_reentry_after(tmp_path, selector):
    m = positional_market()                       # stop 13:00, re-entry 14:00 -> open at 15:15
    h = Harness(tmp_path, selector, m)
    h.run(datetime.combine(MON, time(9, 16)), datetime.combine(TUE, time(15, 20)))
    closed = h.audit("position_closed")
    assert [(c["position_id"], c["exit_reason"]) for c in closed] == [
        ("PB-20260928", "stop: NIFTY 0.5% against"), ("PB-20260928R", "intraday exit")]
    assert closed[1]["exit_ts"].startswith("2026-09-28T15:15")      # same day, not the Tuesday expiry
    assert not h.state.positions and h.paper.positions() == {}


def test_signal_after_entry_end_is_ignored_not_opened(tmp_path, selector):
    mon = day_bars(MON, {time(9, 15): 25000, time(9, 30): 25100, time(9, 45): 25050, time(14, 50): 25120}, 25050)
    cfg = pcfg(kill_file=tmp_path / "KILL", state_dir=tmp_path, audit_dir=tmp_path / "audit",
                       capital=10_000_000, paper_slippage_points=0, entry_end_time=time(14, 45))
    h = Harness(tmp_path, selector, FakeMarket({MON: mon}), cfg)
    h.run(datetime.combine(MON, time(9, 16)), datetime.combine(MON, time(15, 20)))
    rc_ = h.audit("risk_check")
    assert len(rc_) == 1 and not rc_[0]["ok"]
    assert [c["check"] for c in rc_[0]["checks"] if not c["passed"]] == ["entry_window"]
    assert h.paper.log == [] and h.audit("position_opened") == []


def test_manual_exit_detected_no_orders_no_reentry(tmp_path, selector):
    h = Harness(tmp_path, selector, positional_market())
    h.run(datetime.combine(MON, time(9, 16)), datetime.combine(MON, time(12, 0)))
    orders = len(h.paper.log)
    h.paper.net[h.book.option("NIFTY", TUE, 25200, "PUT").tradingsymbol] = 0     # Rohit exits in Kite
    h.run(datetime.combine(MON, time(12, 1)), datetime.combine(MON, time(15, 20)))
    closed = h.audit("position_closed")
    assert [(c["position_id"], c["status"], c["exit_reason"]) for c in closed] == [
        ("PB-20260928", "MANUAL_EXIT", "MANUAL_EXIT")]
    assert closed[0]["exit_ts"].startswith("2026-09-28T12:02")          # 2nd consecutive check, not the 13:00 stop
    assert not closed[0]["exit_reason"].startswith("stop") and closed[0]["pnl_estimated"] is True
    assert len(h.paper.log) == orders                                   # no exit order, no 14:00 re-entry
    assert h.state.blocked == {"PB-20260928": "MANUAL_EXIT"} and h.state.halted is None
    assert h.engine.strategies[0].get_state()["waiting"] is None
    assert not [r for r in h.audit("signal") if r["position_id"] == "PB-20260928R"]


def test_manual_exit_needs_two_consecutive_sightings(tmp_path, selector):
    h = Harness(tmp_path, selector, positional_market())
    h.run(datetime.combine(MON, time(9, 16)), datetime.combine(MON, time(12, 0)))
    sym = h.book.option("NIFTY", TUE, 25200, "PUT").tradingsymbol
    h.paper.net[sym] = 0                                                # broker report lags a moment
    assert h.engine.reconcile(datetime.combine(MON, time(12, 0, 30)))["pending_confirmation"] == ["default:PB-20260928"]
    h.paper.net[sym] = -325                                             # ... and is back
    assert h.engine.reconcile(datetime.combine(MON, time(12, 0, 40)))["ok"]
    assert "PB-20260928" in h.state.positions


def test_reduced_quantity_is_adopted_and_still_managed(tmp_path, selector):
    h = Harness(tmp_path, selector, positional_market())
    h.run(datetime.combine(MON, time(9, 16)), datetime.combine(MON, time(12, 0)))
    h.paper.net[h.book.option("NIFTY", TUE, 25200, "PUT").tradingsymbol] = -130  # Rohit bought back 3 lots
    h.run(datetime.combine(MON, time(12, 1)), datetime.combine(MON, time(13, 5)))
    assert h.audit("adopted_broker_quantity")
    stop = h.audit("position_closed")[0]
    assert stop["exit_reason"].startswith("stop") and stop["quantity"] == 130
    assert sym_net(h) == 0 and h.state.halted is None


def test_unknown_broker_position_is_read_only_and_halts(tmp_path, selector):
    h = Harness(tmp_path, selector, positional_market())
    h.book.option("NIFTY", TUE, 24000, "CALL")
    h.paper.net["NIFTY260929C24000"] = 65                              # something the engine never opened
    res = h.engine.startup(datetime.combine(MON, time(9, 10)))
    acct = h.engine.account.state
    assert not res["ok"] and "BROKER NIFTY 2026-09-29 24000 CALL" in acct.unmanaged
    assert acct.halted.startswith("reconciliation: unknown broker position") and h.state.halted is None
    h.run(datetime.combine(MON, time(9, 16)), datetime.combine(MON, time(15, 20)))
    assert h.paper.net["NIFTY260929C24000"] == 65 and h.audit("position_opened") == []   # untouched, no trades


def test_startup_state_open_but_broker_flat_is_manual_exit(tmp_path, selector):
    h = Harness(tmp_path, selector, positional_market())
    h.run(datetime.combine(MON, time(9, 16)), datetime.combine(MON, time(11, 40)))
    h2 = Harness(tmp_path, selector, positional_market())               # restart; broker (fresh paper) is flat
    h2.engine.strategies[0].set_state(h.state.strategies["positional"])
    res = h2.engine.startup(datetime.combine(MON, time(11, 41)))
    assert res["events"] == [{"event": "MANUAL_EXIT", "instance": "default", "position_id": "PB-20260928"}]
    assert not h2.state.positions and h2.state.blocked["PB-20260928"] == "MANUAL_EXIT"
    assert h2.engine.strategies[0].get_state()["pos"] is None
    h2.run(datetime.combine(MON, time(11, 41)), datetime.combine(MON, time(15, 20)))
    assert h2.paper.log == []                                           # nothing sent for it afterwards


def test_startup_adopts_pending_entry_found_at_broker(tmp_path, selector):
    h = Harness(tmp_path, selector, positional_market())
    h.run(datetime.combine(MON, time(9, 16)), datetime.combine(MON, time(11, 40)))
    pos = h.state.positions.pop("PB-20260928")                         # simulate a crash before recording it
    h.state.pending_entries["PB-20260928"] = {
        "pid": "PB-20260928", "strategy": "positional", "ts": pos["entry_ts"], "wall": pos["entry_wall"],
        "spot": pos["entry_spot"], "reason": pos["reason"], "stop": pos["stop"], "is_reentry": False,
        "ref_price": 130.0, "wing_ref": None,
        "legs": [{"key": l["key"], "side": l["side"], "role": l["role"], "qty": l["qty"], "contract": l["contract"]}
                 for l in pos["legs"]]}
    h.state.save()
    h2 = Harness(tmp_path, selector, positional_market())
    h2.book.option("NIFTY", TUE, 25200, "PUT")
    h2.paper.net["NIFTY260929P25200"] = -325
    h2.engine.strategies[0].set_state(h.state.strategies["positional"])
    res = h2.engine.startup(datetime.combine(MON, time(11, 41)))
    assert res["events"] == [{"event": "adopted_pending_entry", "instance": "default", "position_id": "PB-20260928"}]
    p = h2.state.positions["PB-20260928"]
    assert p["entry_price_estimated"] and p["legs"][0]["qty"] == 325 and not h2.state.pending_entries
    h2.run(datetime.combine(MON, time(11, 42)), datetime.combine(MON, time(13, 5)))
    assert h2.audit("position_closed")[0]["exit_reason"].startswith("stop")     # managed normally


def test_startup_drops_pending_entry_when_broker_flat(tmp_path, selector):
    h = Harness(tmp_path, selector, positional_market())
    h.state.pending_entries["PB-X"] = {"pid": "PB-X", "strategy": "positional", "ts": "2026-09-28T11:30:00",
                                       "wall": "", "spot": 1, "reason": "", "stop": {}, "is_reentry": False,
                                       "ref_price": 1, "wing_ref": None,
                                       "legs": [{"key": ["NIFTY", "2026-09-29", 25200.0, "PUT"], "side": "SELL",
                                                 "role": "MAIN", "qty": 325,
                                                 "contract": {"underlying": "NIFTY", "exchange": "NFO",
                                                              "expiry": "2026-09-29", "strike": 25200.0,
                                                              "right": "PUT"}}]}
    res = h.engine.startup(datetime.combine(MON, time(9, 10)))
    assert res["events"] == [{"event": "pending_entry_dropped", "instance": "default", "position_id": "PB-X"}]
    assert not h.state.pending_entries and not h.state.positions and h.state.halted is None


def test_open_orders_at_startup_halt(tmp_path, selector):
    h = Harness(tmp_path, selector, positional_market())
    manual = {"req": type("R", (), {"tradingsymbol": "X", "tag": ""})(), "status": "OPEN", "quantity": 65,
              "filled": 0, "price": 1, "avg": 0, "msg": ""}
    h.paper.orders["P98"] = manual                                      # yours (no instance tag): ignored
    assert h.engine.startup(datetime.combine(MON, time(9, 10)))["ok"] and h.state.halted is None
    h.paper.orders["P99"] = dict(manual, req=type("R", (), {"tradingsymbol": "X", "tag": "default"})())
    assert not h.engine.startup(datetime.combine(MON, time(9, 11)))["ok"]
    assert "open order P99" in h.state.halted


def profit_market():
    return FakeMarket({MON: day_bars(MON, {time(9, 15): 25000, time(9, 30): 25100, time(9, 45): 25050,
                                           time(11, 30): 25120, time(12, 0): 25400}, 25050)})


def test_max_daily_profit_squares_off_and_ends_day(tmp_path, selector):
    cfg = pcfg(kill_file=tmp_path / "KILL", state_dir=tmp_path, audit_dir=tmp_path / "audit",
                       capital=10_000_000, paper_slippage_points=0, max_daily_profit_enabled=True,
                       max_daily_profit=20_000)
    h = Harness(tmp_path, selector, profit_market(), cfg)
    h.run(datetime.combine(MON, time(9, 16)), datetime.combine(MON, time(15, 20)))
    closed = h.audit("position_closed")
    assert len(closed) == 1 and "MAX_PROFIT_REACHED" in closed[0]["exit_reason"]
    assert closed[0]["gross_pnl"] == pytest.approx((130 - 50) * 325)            # 26,000 >= 20,000
    assert h.state.session_status == "MAX_PROFIT_REACHED" and h.state.halted == "MAX_PROFIT_REACHED"
    cap = h.audit("max_daily_profit")[0]
    assert cap["pnl"] == pytest.approx(26_000)                                  # realised + unrealised MTM
    assert h.paper.positions() == {}
    h.engine._roll_day(TUE, datetime.combine(TUE, time(9, 15)))                 # next day trades again
    assert h.state.halted is None and h.state.session_status is None


def test_daily_caps_share_one_pnl_and_can_be_disabled(tmp_path, selector):
    cfg = pcfg(kill_file=tmp_path / "KILL", state_dir=tmp_path, audit_dir=tmp_path / "audit",
                       capital=10_000_000, paper_slippage_points=0, max_daily_loss_enabled=False,
                       max_daily_loss=1000)
    h = Harness(tmp_path, selector, positional_market(), cfg)
    h.run(datetime.combine(MON, time(9, 16)), datetime.combine(MON, time(13, 30)))
    assert h.engine.daily_pnl() < -1000 and h.state.halted is None               # loss cap disabled
    cfg2 = replace(cfg, max_daily_loss_enabled=True, state_dir=tmp_path / "b", audit_dir=tmp_path / "b" / "audit")
    h2 = Harness(tmp_path / "b", selector, positional_market(), cfg2)
    h2.run(datetime.combine(MON, time(9, 16)), datetime.combine(MON, time(12, 30)))
    assert h2.state.halted is None
    h2.run(datetime.combine(MON, time(12, 31)), datetime.combine(MON, time(13, 30)))
    assert h2.state.halted.startswith("max daily loss") and h2.state.session_status == "MAX_LOSS_REACHED"


def test_new_config_keys_and_validation():
    c = EngineConfig.from_env(env_file=None, environ={
        "ENTRY_START_TIME": "09:30", "ENTRY_END_TIME": "14:30", "FORCE_EXIT_TIME": "15:10", "INTRADAY_ONLY": "false",
        "RISK_MAX_DAILY_PROFIT_ENABLED": "true", "RISK_MAX_DAILY_PROFIT": "40000", "POSITIONAL_RANGE_END": "11:00",
        "ZERODTE_STEP_MINUTES": "5", "ENGINE_RECONCILE_CONFIRMATIONS": "3", "PAPER_SPAN_PCT": "10"})
    assert (c.entry_start_time, c.entry_end_time, c.force_exit_time) == (time(9, 30), time(14, 30), time(15, 10))
    assert not c.intraday_only and c.max_daily_profit_enabled and c.max_daily_profit == 40_000
    assert (c.positional_range_end, c.zerodte_step_minutes, c.reconcile_confirmations, c.paper_span_pct) == \
        (time(11, 0), 5, 3, 10.0)
    with pytest.raises(ValueError):
        EngineConfig(entry_end_time=time(15, 20), force_exit_time=time(15, 15))
    with pytest.raises(ValueError):
        EngineConfig(max_daily_profit_enabled=True, max_daily_profit=0)


# -- round 3: several instances in one account ------------------------------------------------------
from live.engine import Account  # noqa: E402


class Multi:
    """One Account + one PaperBroker shared by several instances (like live/app.build)."""

    def __init__(self, tmp_path, selector, market, cfgs, net=None):
        self.now = datetime.combine(MON, time(9, 0))
        self.book = SyntheticInstrumentBook(65)
        self.market, self.selector, self.tmp = market, selector, tmp_path
        self.paper = PaperBroker(funds=1e9, price_fn=self._price, margin_fn=lambda reqs: 150_000.0)
        zc = ZerodhaConfig(dry_run=False, fill_timeout_s=0, poll_interval_s=0, max_reprices=1)
        self.broker = ZerodhaExecutionBroker(self.paper, self.book, zc, live=False)
        g = cfgs[0]
        self.account = Account(g, market, self.broker, [], EngineState.load(tmp_path / "_account.json"),
                               AuditLog(g.audit_dir, "PAPER", instance="account"))
        self.inst = {}
        for c in cfgs:
            strat = PositionalBreakout(replace(RangeBreakoutParams(), expiry_offset=c.expiry_offset),
                                       intraday_exit=c.force_exit_time if c.intraday_only else None)
            self.inst[c.instance_id] = TradingEngine(
                c, market, self.broker, selector, [strat], RiskManager(c),
                EngineState.load(tmp_path / f"{c.instance_id}.json"),
                AuditLog(c.audit_dir, "PAPER", instance=c.instance_id), account=self.account)
        for sym_key, q in (net or {}).items():
            self.paper.net[self.book.option(*sym_key).tradingsymbol] = q

    def _price(self, key):
        i = self.book.by_symbol(key.split(":")[1])
        return self.market.option_price(self.selector.contract(i.expiry, i.strike, i.right), self.now)

    def run(self, start, end):
        self.now = start
        while self.now <= end:
            self.account.step(self.now)
            self.now += M

    def audit(self, iid, event=None):
        recs = [json.loads(l) for p in sorted((self.tmp / "audit").glob(f"audit_paper_{iid}_*.jsonl"))
                for l in p.read_text().splitlines()]
        return [r for r in recs if event is None or r["event"] == event]


def icfg(tmp_path, iid, **kw):
    base = dict(mode="PAPER", instance_id=iid, kill_file=tmp_path / "KILL", state_dir=tmp_path,
                audit_dir=tmp_path / "audit", capital=10_000_000, paper_slippage_points=0,
                intraday_only=True, position_mode="NAKED")
    base.update(kw)
    return EngineConfig(**base)


NEXT_TUE = date(2026, 10, 6)


def test_two_instances_have_independent_profit_caps(tmp_path, selector):
    a = icfg(tmp_path, "A", max_daily_profit_enabled=True, max_daily_profit=20_000)
    b = icfg(tmp_path, "B", expiry_offset=1)                          # next week's contract: no clash with A
    m = Multi(tmp_path, selector, profit_market(), [a, b])
    m.run(datetime.combine(MON, time(9, 16)), datetime.combine(MON, time(12, 5)))
    A, B = m.inst["A"], m.inst["B"]
    assert A.state.session_status == "MAX_PROFIT_REACHED" and A.state.halted == "MAX_PROFIT_REACHED"
    assert not A.state.positions and "MAX_PROFIT_REACHED" in A.closed[0]["exit_reason"]
    assert B.state.session_status is None and B.state.halted is None and "PB-20260928" in B.state.positions
    assert B.state.positions["PB-20260928"]["legs"][0]["contract"]["expiry"] == NEXT_TUE.isoformat()
    assert m.paper.positions() == {m.book.option("NIFTY", NEXT_TUE, 25200, "PUT").tradingsymbol: -325}
    assert (tmp_path / "A.json").exists() and (tmp_path / "B.json").exists()
    sa, sb = json.loads((tmp_path / "A.json").read_text()), json.loads((tmp_path / "B.json").read_text())
    assert sa["positions"] == {} and list(sb["positions"]) == ["PB-20260928"]    # state kept per instance


def test_profit_cap_flattens_a_carried_hedged_spread(tmp_path, selector):
    a = icfg(tmp_path, "A", intraday_only=False, position_mode="HEDGED", hedge_width=300,
             max_daily_profit_enabled=True, max_daily_profit=15_000)
    m = Multi(tmp_path, selector, profit_market(), [a])
    m.run(datetime.combine(MON, time(9, 16)), datetime.combine(MON, time(11, 32)))
    pos = m.inst["A"].state.positions["PB-20260928"]
    assert pos["carry_allowed"] and pos["position_mode"] == "HEDGED" and pos["hold_until"].startswith("2026-09-29T15:15")
    m.run(datetime.combine(MON, time(11, 33)), datetime.combine(MON, time(12, 5)))
    assert not m.inst["A"].state.positions and m.paper.positions() == {}           # both legs closed
    closed = m.inst["A"].closed[0]
    assert "MAX_PROFIT_REACHED" in closed["exit_reason"] and closed["position_mode"] == "HEDGED"
    assert len(closed["exit_order_ids"]) == 2


def test_per_instance_and_global_kill_switches(tmp_path, selector):
    m = Multi(tmp_path, selector, positional_market(), [icfg(tmp_path, "A"), icfg(tmp_path, "B", expiry_offset=1)])
    m.run(datetime.combine(MON, time(9, 16)), datetime.combine(MON, time(11, 35)))
    assert m.inst["A"].state.positions and m.inst["B"].state.positions
    (tmp_path / "KILL_A").write_text("x")
    m.run(datetime.combine(MON, time(11, 36)), datetime.combine(MON, time(11, 40)))
    assert not m.inst["A"].state.positions and m.inst["A"].killed
    assert m.inst["B"].state.positions and not m.account.stopped                 # B keeps running
    (tmp_path / "KILL").write_text("x")
    m.run(datetime.combine(MON, time(11, 41)), datetime.combine(MON, time(11, 42)))
    assert m.account.stopped and not m.inst["B"].state.positions and m.paper.positions() == {}


def test_orders_are_tagged_with_the_instance(tmp_path, selector):
    m = Multi(tmp_path, selector, positional_market(), [icfg(tmp_path, "A"), icfg(tmp_path, "B", expiry_offset=1)])
    m.run(datetime.combine(MON, time(9, 16)), datetime.combine(MON, time(11, 35)))
    tags = {o["req"].tradingsymbol: o["req"].tag for o in m.paper.orders.values()}
    assert tags == {m.book.option("NIFTY", TUE, 25200, "PUT").tradingsymbol: "A",
                    m.book.option("NIFTY", NEXT_TUE, 25200, "PUT").tradingsymbol: "B"}
    assert m.audit("A", "order")[0]["tag"] == "A" and m.audit("B", "order")[0]["tag"] == "B"


def test_shared_contract_blocked_by_default(tmp_path, selector):
    m = Multi(tmp_path, selector, positional_market(), [icfg(tmp_path, "A"), icfg(tmp_path, "B")])
    m.run(datetime.combine(MON, time(9, 16)), datetime.combine(MON, time(11, 35)))
    assert m.inst["A"].state.positions and not m.inst["B"].state.positions
    rc_b = m.audit("B", "risk_check")[0]
    assert not rc_b["ok"] and any(c["check"] == "contract_free" and "another instance" in c["detail"]
                                  for c in rc_b["checks"])


def test_shared_contract_mismatch_halts_every_claimant(tmp_path, selector):
    cfgs = [icfg(tmp_path, "A", shared_contracts=True), icfg(tmp_path, "B", shared_contracts=True)]
    m = Multi(tmp_path, selector, positional_market(), cfgs)
    m.run(datetime.combine(MON, time(9, 16)), datetime.combine(MON, time(11, 35)))
    sym = m.book.option("NIFTY", TUE, 25200, "PUT").tradingsymbol
    assert m.paper.net[sym] == -650 and m.inst["A"].state.positions and m.inst["B"].state.positions
    m.paper.net[sym] = -325                                              # someone closed half in Kite
    m.run(datetime.combine(MON, time(11, 36)), datetime.combine(MON, time(11, 38)))
    for iid in ("A", "B"):
        assert m.inst[iid].state.halted.startswith("reconciliation: shared contract")
        assert m.inst[iid].state.positions                               # nothing changed: cannot attribute


def test_manual_position_untouched_and_blocks_all_instances(tmp_path, selector):
    m = Multi(tmp_path, selector, positional_market(), [icfg(tmp_path, "A"), icfg(tmp_path, "B", expiry_offset=1)],
              net={("NIFTY", TUE, 24000, "CALL"): 65})
    m.account.startup(datetime.combine(MON, time(9, 10)))
    m.run(datetime.combine(MON, time(9, 16)), datetime.combine(MON, time(15, 20)))
    assert m.paper.net["NIFTY260929C24000"] == 65
    assert not m.audit("A", "position_opened") and not m.audit("B", "position_opened")
    assert m.account.state.halted.startswith("reconciliation: unknown broker position")


def test_positional_carries_to_expiry_but_intraday_positions_never_do(tmp_path, selector):
    carry = icfg(tmp_path, "P", intraday_only=False)
    m = Multi(tmp_path, selector, positional_market(), [carry])
    m.run(datetime.combine(MON, time(9, 16)), datetime.combine(MON, time(15, 30)))
    P = m.inst["P"]
    assert "PB-20260928R" in P.state.positions                            # re-entered at 14:00, held overnight
    m2 = Multi(tmp_path, selector, positional_market(), [carry],          # restart next morning
               net={("NIFTY", TUE, 25200, "PUT"): -325})
    m2.inst["P"].strategies[0].set_state(P.state.strategies["positional"])
    assert m2.account.startup(datetime.combine(TUE, time(9, 5)))["ok"]
    m2.run(datetime.combine(TUE, time(9, 16)), datetime.combine(TUE, time(15, 20)))
    day = [r for r in m2.audit("P", "day_start") if r["market_ts"].startswith("2026-09-29")][0]
    assert day["carried_positions"] == ["PB-20260928R"] and day["stray_positions"] == []
    closed = m2.inst["P"].closed[0]
    assert closed["exit_reason"] == "expiry-day exit" and closed["exit_ts"].startswith("2026-09-29T15:15")
    assert closed["days_held"] == 1
    # an intraday instance that somehow still holds yesterday's position exits it at once
    i = icfg(tmp_path / "i", "I")
    (tmp_path / "i").mkdir()
    m3 = Multi(tmp_path / "i", selector, positional_market(), [i], net={("NIFTY", TUE, 25200, "PUT"): -325})
    I = m3.inst["I"]
    I.state.positions["PB-OLD"] = I._position(
        "PB-OLD", "positional", False, datetime.combine(MON, time(11, 30)), datetime.combine(MON, time(11, 31)),
        25120.0, "t", {"basis": "spot", "level": 1}, [I._leg(("NIFTY", TUE, 25200.0, "PUT"), "SELL", "MAIN", 325,
                                                          OptionContract("NIFTY", "NFO", TUE, 25200, "PUT"), 130.0)],
        None, False, "2026-09-28T15:15:00")
    m3.run(datetime.combine(TUE, time(9, 16)), datetime.combine(TUE, time(9, 17)))
    assert I.closed[0]["exit_reason"].startswith("intraday: carried over from 2026-09-28")
    assert [r for r in m3.audit("I", "day_start")][0]["stray_positions"] == ["PB-OLD"]


def test_load_instances_prefixes_and_defaults():
    from live.config import load_instances
    cfgs, warns = load_instances(None, {"INSTANCES": "posH300,zd1", "posH300__STRATEGY": "positional",
                                        "zd1__STRATEGY": "zerodte", "RISK_MAX_DAILY_LOSS": "30000",
                                        "zd1__RISK_MAX_DAILY_LOSS": "10000", "posH300__ENGINE_POLL_SECONDS": "1"})
    p, z = cfgs
    assert (p.instance_id, p.position_mode, p.hedge_width, p.intraday_only, p.max_daily_loss) == \
        ("posH300", "HEDGED", 300, False, 30000)
    assert (z.instance_id, z.position_mode, z.intraday_only, z.max_daily_loss) == ("zd1", "NAKED", True, 10000)
    assert any("ENGINE_POLL_SECONDS is account-wide" in w for w in warns)
    naked, w2 = load_instances(None, {"INSTANCES": "p", "p__STRATEGY": "positional", "p__POSITION_MODE": "NAKED",
                                      "KITE_PRODUCT": "MIS"})
    assert len(w2) == 2 and naked[0].position_mode == "NAKED"              # warnings, never a refusal
    legacy, _ = load_instances(None, {})
    assert [c.instance_id for c in legacy] == ["positional", "zerodte"]
    with pytest.raises(ValueError):
        load_instances(None, {"INSTANCES": "bad_id", "bad_id__STRATEGY": "positional"})
    with pytest.raises(ValueError):
        load_instances(None, {"INSTANCES": "x"})                            # STRATEGY missing
