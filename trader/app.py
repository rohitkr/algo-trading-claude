"""Wiring: config -> repository, instruments, prices, broker (paper or Kite), service."""
from __future__ import annotations

import fcntl
import logging
from dataclasses import dataclass
from pathlib import Path

from trading_data.breeze.session_store import now_ist

from .broker import build_broker
from .config import TraderConfig
from .instruments import InstrumentService, kite_loader
from .market import BreezeQuotes, KiteQuotes, ManualQuotes
from .paper import PaperExchange
from .repository import Repository
from .service import TradeService
from .strategy import StrategyService

log = logging.getLogger("trader")


class AlreadyRunning(RuntimeError):
    pass


@dataclass
class App:
    cfg: TraderConfig
    service: TradeService
    repo: Repository
    paper: PaperExchange | None
    quotes: object
    lock_fh: object
    strategies: StrategyService
    stream: object = None          # marketdata.KiteStream (MARKET_DATA_PROVIDER=KITE), else None
    hub: object = None             # trader.stream.TickHub: pushes ticks / dashboard changes to the pages


def single_instance_lock(db_path: Path):
    """Two trader processes on one database would both manage the same trades: refuse the second."""
    p = Path(db_path).with_suffix(".lock")
    p.parent.mkdir(parents=True, exist_ok=True)
    fh = open(p, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise AlreadyRunning(f"another trader process holds {p}") from None
    fh.write("locked\n")
    fh.flush()
    return fh


def build(cfg: TraderConfig, *, cli_live: bool, clock=now_ist, mcfg=None) -> App:
    from marketdata.config import MarketDataConfig
    mcfg = mcfg or MarketDataConfig.from_env()
    lock = single_instance_lock(cfg.db_path)
    repo = Repository(cfg.db_path, clock)
    from zerodha.config import ZerodhaConfig
    zcfg = ZerodhaConfig.from_env()

    live_kite = None
    if cfg.mode == "LIVE":
        broker =build_broker(cfg, cli_live=cli_live, zcfg=zcfg)       # refuses unless every switch is on
        live_kite = broker.kite

    def kite_for_instruments():
        if live_kite is not None:
            return live_kite
        from zerodha.auth import new_kite
        return new_kite(zcfg) if zcfg.api_key else _PublicInstruments()

    instruments = InstrumentService(kite_loader(kite_for_instruments), cfg.underlyings, clock, cfg.lot_units)

    def breeze_quotes():
        from trading_data.config import load_settings
        return BreezeQuotes(load_settings(), repo, cfg.breeze_daily_budget, cfg.quote_ttl_s, clock=clock,
                            reserve=cfg.breeze_reserve)

    stream = None
    if cfg.mode == "PAPER" and cfg.paper_quotes == "manual":
        quotes = ManualQuotes()
    elif mcfg.kite:
        from marketdata.kite_stream import build_kite_stream
        stream = build_kite_stream(mcfg, zcfg)             # one KiteTicker for this whole process
        quotes = KiteQuotes(stream, breeze_quotes() if mcfg.breeze_fallback else None,
                            spot_resolver=lambda u: _spot_token(instruments, u))
    else:
        quotes = breeze_quotes()

    paper = None
    if cfg.mode == "PAPER":
        def price(exchange: str, symbol: str):
            # paper fills of working orders: priced at the slow rate (entries just rest until touched)
            return quotes.ltp(instruments.by_symbol(exchange, symbol), max_age=cfg.quote_slow_s)
        paper = PaperExchange(price, cfg.paper_state, clock=clock, slippage=cfg.paper_slippage)
        broker = build_broker(cfg, cli_live=cli_live, paper_kite=paper)
    broker.units = instruments.units_per_lot      # MCX: Kite quantities are lots, the trader counts units

    from live.audit import AuditLog
    audit = AuditLog(cfg.audit_dir, cfg.mode, clock=clock, instance="trader")
    svc = TradeService(cfg, repo, broker, instruments, quotes, clock, audit_log=audit)
    if zcfg.api_key:
        def rest_kite():
            if live_kite is not None:
                return live_kite
            from zerodha.auth import connected_kite        # PAPER: today's saved login (as the price stream)
            return connected_kite(zcfg)

        def prev_close(underlying: str):
            # Kite ohlc() by instrument token: "close" is the last trading day's close (for the spot's % change)
            tok = str(_spot_token(instruments, underlying))
            return (rest_kite().ohlc([tok]).get(tok) or {}).get("ohlc", {}).get("close")
        svc.prev_close_fn = prev_close

        def margin(orders: list[dict]):
            # Kite basket margins for a strategy before it is placed (hedge benefit included; display only)
            return rest_kite().basket_order_margins(orders, consider_positions=False)
        svc.margin_fn = margin
    strategies = StrategyService(svc, repo)
    svc.extra_tick = strategies.tick        # combined-P&L rules run right after the per-trade engine, every tick
    from .stream import TickHub
    return App(cfg, svc, repo, paper, quotes, lock, strategies, stream, TickHub(svc, quotes))


def _spot_token(instruments: InstrumentService, underlying: str) -> int:
    """Index token for NSE/BSE underlyings; for MCX (no index) the future the options are written on."""
    from marketdata import index_token

    from .config import exchange_for
    if exchange_for(underlying) == "MCX":
        return instruments.spot_future(underlying)[0]
    return index_token(underlying)


class _PublicInstruments:
    """Kite's instrument dump is a public CSV; used in PAPER when no KITE_API_KEY is configured."""

    def instruments(self, exchange: str):
        import csv
        import io
        import urllib.request
        with urllib.request.urlopen(f"https://api.kite.trade/instruments/{exchange}", timeout=30) as r:
            return list(csv.DictReader(io.StringIO(r.read().decode())))
