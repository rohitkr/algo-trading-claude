"""Wiring: builds the engine for BACKTEST and PAPER from the existing components.

    market data  BACKTEST: live.replay.ReplayMarketData (DuckDB)
                 PAPER:    trading_data.breeze.live.BreezeMarketData (Breeze, existing session + budget)
    execution    zerodha.execution.ZerodhaExecutionBroker over zerodha.PaperBroker (simulated exchange,
                 real Zerodha order path: margin check, leg ordering, slicing, re-pricing, unwinds)
    strategies   live.strategies over backtest/rules.py + backtest parameter classes

LIVE is deliberately NOT wired here yet: see README "LIVE mode setup".
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path

from backtest.data import DataFeed
from backtest.strategies import ZeroDteStraddleSeller
from trading_data.breeze.session_store import now_ist
from trading_data.config import Settings, load_settings
from trading_data.log import setup_logging
from trading_data.storage import CandleStore
from zerodha import PaperBroker, ZerodhaConfig
from zerodha.execution import ZerodhaExecutionBroker
from zerodha.instruments import SyntheticInstrumentBook
from zerodha.margin import estimate_basket_margin

from .audit import AuditLog
from .config import EngineConfig
from .engine import TradingEngine
from .interfaces import MarketDataProvider
from .risk import RiskManager
from .selection import ContractSelector
from .state import EngineState
from .strategies import PositionalBreakout, ZeroDteStraddle, params_from_config, walk_forward_chooser


class LiveModeNotWired(RuntimeError):
    pass


class Clock:
    """The engine's notion of 'now' (wall clock IST in PAPER, simulated in BACKTEST)."""

    def __init__(self, fn=now_ist):
        self.fn, self.now = fn, fn()

    def tick(self) -> datetime:
        self.now = self.fn()
        return self.now


@dataclass
class Built:
    engine: TradingEngine
    store: CandleStore
    feed: DataFeed
    clock: Clock
    paper: PaperBroker


def setup_live_logging(settings: Settings, name: str) -> None:
    root = setup_logging(settings.paths.log_dir, name)
    for lg in ("live", "zerodha"):
        logger = logging.getLogger(lg)
        logger.handlers = list(root.handlers)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False


def build(cfg: EngineConfig, *, clock: Clock | None = None, settings: Settings | None = None,
          store: CandleStore | None = None, client=None, market: MarketDataProvider | None = None,
          state_path=None) -> Built:
    if cfg.mode == "LIVE":
        raise LiveModeNotWired("LIVE execution is not wired into the engine yet; run PAPER or BACKTEST. "
                               "See README 'LIVE mode setup' for what must be verified first.")
    settings = settings or load_settings()
    store = store or CandleStore(settings.paths.database)
    clock = clock or Clock()

    if cfg.mode == "PAPER" and client is None and market is None:
        from trading_data.breeze.client import BreezeClient
        client = BreezeClient(settings, store).connect()
    feed = DataFeed(settings, store, cfg.underlying, client if cfg.mode == "PAPER" else None)
    selector = ContractSelector(feed, cfg.lot_size, cfg.strike_step)
    if market is None:
        if cfg.mode == "BACKTEST":
            from .replay import ReplayMarketData
            market = ReplayMarketData(feed)
        else:
            from trading_data.breeze.live import BreezeMarketData
            market = BreezeMarketData(client, settings, cfg.underlying,
                                      min_refetch_s=cfg.bar_refetch_s, quote_ttl_s=cfg.quote_ttl_s)

    book = SyntheticInstrumentBook(selector.lot_size)

    def price(key: str) -> float:
        inst = book.by_symbol(key.split(":", 1)[1])
        px = market.option_price(selector.contract(inst.expiry, inst.strike, inst.right), clock.now, fresh=True)
        if px is None:
            raise KeyError(f"no market price for {key}")
        return px

    def spot_now() -> float:
        bars = market.spot_bars(clock.now.date(), clock.now)
        return float(bars["close"].iloc[-1]) if len(bars) else 0.0

    paper = PaperBroker(funds=cfg.capital, price_fn=price,
                        margin_fn=lambda reqs: estimate_basket_margin(reqs, book, spot_now(), cfg.paper_span_pct,
                                                                      cfg.paper_exposure_pct),
                        slippage=cfg.paper_slippage_points)
    pcfg = replace(ZerodhaConfig.from_env(), dry_run=False, fill_timeout_s=0, poll_interval_s=0)
    broker = ZerodhaExecutionBroker(paper, book, pcfg, live=False,
                                    name="paper (backtest replay)" if cfg.mode == "BACKTEST" else "paper")

    pos_params, zd_params = params_from_config(cfg, selector.lot_size)
    strategies = []
    if "positional" in cfg.strategies:
        strategies.append(PositionalBreakout(pos_params, cfg.positional_min_range_bars,
                                             cfg.positional_new_signal_cancels_reentry,
                                             intraday_exit=cfg.force_exit_time if cfg.intraday_only else None))
    if "zerodte" in cfg.strategies:
        seller = ZeroDteStraddleSeller(feed, zd_params)
        strategies.append(ZeroDteStraddle(zd_params, walk_forward_chooser(seller, cfg.zerodte_entry_time),
                                          quote_stops=cfg.zerodte_quote_stops,
                                          max_entry_delay=timedelta(seconds=cfg.zerodte_max_entry_delay_s)))

    state = EngineState.load(state_path or cfg.state_dir / f"state_{cfg.mode.lower()}.json")
    audit = AuditLog(cfg.audit_dir, cfg.mode, clock=now_ist)
    engine = TradingEngine(cfg, market, broker, selector, strategies, RiskManager(cfg), state, audit)
    _persist_paper_exchange(engine, paper, book, state_path or cfg.state_dir / f"state_{cfg.mode.lower()}.json")
    return Built(engine, store, feed, clock, paper)


def _persist_paper_exchange(engine: TradingEngine, paper: PaperBroker, book, state_path) -> None:
    """The simulated exchange keeps its own positions file (like Zerodha keeps its own), so startup
    reconciliation in PAPER is a real cross-check. Edit it to rehearse a manual exit."""
    path = Path(state_path).with_name(Path(state_path).stem + "_paper_exchange.json")
    if path.exists():
        for row in json.loads(path.read_text()):
            inst = book.option(row["underlying"], date.fromisoformat(row["expiry"]), row["strike"], row["right"])
            paper.net[inst.tradingsymbol] = int(row["qty"])

    def save() -> None:
        rows = []
        for sym, q in paper.net.items():
            if q:
                i = book.by_symbol(sym)
                rows.append({"underlying": i.name, "expiry": i.expiry.isoformat(), "strike": i.strike,
                             "right": i.right, "qty": q, "symbol": sym})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows, indent=1))
    engine.save_hooks.append(save)
