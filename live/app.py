"""Wiring: builds the engine for BACKTEST and PAPER from the existing components.

    market data  BACKTEST: live.replay.ReplayMarketData (DuckDB)
                 PAPER:    MARKET_DATA_PROVIDER=BREEZE (default): trading_data.breeze.live.BreezeMarketData
                           MARKET_DATA_PROVIDER=KITE: marketdata.kite_provider.KiteMarketDataProvider
                           (KiteTicker prices + Kite historical bars; see marketdata/config.py)
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
from .engine import Account, TradingEngine
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
    account: Account
    instances: dict[str, TradingEngine]
    store: CandleStore
    feed: DataFeed
    clock: Clock
    paper: PaperBroker

    @property
    def engine(self) -> TradingEngine:            # the only / first instance
        return next(iter(self.instances.values()))


def setup_live_logging(settings: Settings, name: str) -> None:
    root = setup_logging(settings.paths.log_dir, name)
    for lg in ("live", "zerodha"):
        logger = logging.getLogger(lg)
        logger.handlers = list(root.handlers)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False


def state_dir(cfg: EngineConfig) -> Path:
    return cfg.state_dir / cfg.mode.lower()


def build(cfgs: list[EngineConfig] | EngineConfig, *, clock: Clock | None = None, settings: Settings | None = None,
          store: CandleStore | None = None, client=None, market: MarketDataProvider | None = None,
          state_root: Path | None = None, mcfg=None) -> Built:
    """One Account with one TradingEngine per instance config (all sharing data feed and broker)."""
    cfgs = [cfgs] if isinstance(cfgs, EngineConfig) else list(cfgs)
    g = cfgs[0]                                     # account-wide settings are identical in every instance config
    if g.mode == "LIVE":
        raise LiveModeNotWired("LIVE execution is not wired into the engine yet; run PAPER or BACKTEST. "
                               "See README 'LIVE mode setup' for what must be verified first.")
    if len({c.underlying for c in cfgs}) > 1:
        raise ValueError("all instances must trade the same UNDERLYING")
    settings = settings or load_settings()
    store = store or CandleStore(settings.paths.database)
    clock = clock or Clock()
    root = Path(state_root) if state_root else state_dir(g)

    kite_data = None
    if g.mode == "PAPER" and client is None and market is None:
        from marketdata.config import MarketDataConfig
        mcfg = mcfg or MarketDataConfig.from_env()
        if mcfg.kite:
            kite_data = mcfg
        if not mcfg.kite or mcfg.breeze_fallback:
            # Breeze stays the DataFeed's on-demand source for EXPIRED option history (zero-DTE walk-forward),
            # which Kite does not serve; with Kite and no Breeze fallback the feed reads DuckDB only.
            from trading_data.breeze.client import BreezeClient
            client = BreezeClient(settings, store).connect()
    feed = DataFeed(settings, store, g.underlying, client if g.mode == "PAPER" else None)
    if market is None:
        if g.mode == "BACKTEST":
            from .replay import ReplayMarketData
            market = ReplayMarketData(feed)
        elif kite_data is not None:
            from marketdata.kite_provider import KiteMarketDataProvider
            from marketdata.kite_stream import build_kite_stream
            from zerodha.auth import access_token
            zcfg = ZerodhaConfig.from_env()
            access_token(zcfg)                      # LoginRequired now, not a silently price-less engine
            stream = build_kite_stream(kite_data, zcfg).start()
            market = KiteMarketDataProvider(stream.kite_factory, stream, settings, g.underlying,
                                            min_refetch_s=g.bar_refetch_s,
                                            historical_min_interval_s=kite_data.historical_min_interval_s)
        else:
            from trading_data.breeze.live import BreezeMarketData
            market = BreezeMarketData(client, settings, g.underlying,
                                      min_refetch_s=g.bar_refetch_s, quote_ttl_s=g.quote_ttl_s)

    base_selector = ContractSelector(feed, g.lot_size, g.strike_step)
    book = SyntheticInstrumentBook(base_selector.lot_size)

    def price(key: str) -> float:
        inst = book.by_symbol(key.split(":", 1)[1])
        px = market.option_price(base_selector.contract(inst.expiry, inst.strike, inst.right), clock.now, fresh=True)
        if px is None:
            raise KeyError(f"no market price for {key}")
        return px

    def spot_now() -> float:
        bars = market.spot_bars(clock.now.date(), clock.now)
        return float(bars["close"].iloc[-1]) if len(bars) else 0.0

    paper = PaperBroker(funds=sum(c.capital for c in cfgs), price_fn=price,
                        margin_fn=lambda reqs: estimate_basket_margin(reqs, book, spot_now(), g.paper_span_pct,
                                                                      g.paper_exposure_pct),
                        slippage=g.paper_slippage_points)
    pcfg = replace(ZerodhaConfig.from_env(), dry_run=False, fill_timeout_s=0, poll_interval_s=0)
    broker = ZerodhaExecutionBroker(paper, book, pcfg, live=False,
                                    name="paper (backtest replay)" if g.mode == "BACKTEST" else "paper")

    account = Account(g, market, broker, [], EngineState.load(root / "_account.json"),
                      AuditLog(g.audit_dir, g.mode, clock=now_ist, instance="account"))
    instances: dict[str, TradingEngine] = {}
    for cfg in cfgs:
        selector = ContractSelector(feed, cfg.lot_size, cfg.strike_step)
        pos_params, zd_params = params_from_config(cfg, selector.lot_size)
        if cfg.strategy == "positional":
            strat = PositionalBreakout(pos_params, cfg.positional_min_range_bars,
                                       cfg.positional_new_signal_cancels_reentry,
                                       intraday_exit=cfg.force_exit_time if cfg.intraday_only else None)
        else:
            seller = ZeroDteStraddleSeller(feed, zd_params)
            strat = ZeroDteStraddle(zd_params, walk_forward_chooser(seller, cfg.zerodte_entry_time),
                                    quote_stops=cfg.zerodte_quote_stops,
                                    max_entry_delay=timedelta(seconds=cfg.zerodte_max_entry_delay_s))
        instances[cfg.instance_id] = TradingEngine(
            cfg, market, broker, selector, [strat], RiskManager(cfg), EngineState.load(root / f"{cfg.instance_id}.json"),
            AuditLog(cfg.audit_dir, cfg.mode, clock=now_ist, instance=cfg.instance_id), account=account)
    _persist_paper_exchange(account, paper, book, root / "_paper_exchange.json")
    return Built(account, instances, store, feed, clock, paper)


def _persist_paper_exchange(account: Account, paper: PaperBroker, book, path: Path) -> None:
    """The simulated exchange keeps its own positions file (like Zerodha keeps its own), so startup
    reconciliation in PAPER is a real cross-check. Edit it to rehearse a manual exit."""
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
    account.save_hooks.append(save)
