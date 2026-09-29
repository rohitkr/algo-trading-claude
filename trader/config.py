"""Trader settings from the root .env (shell variables override it). Nothing trading-related is hardcoded.

Safety: TRADER_MODE defaults to PAPER (simulated exchange, no Zerodha orders). A real order needs ALL of
    TRADER_MODE=LIVE, ENABLE_LIVE_TRADING=true, KITE_DRY_RUN=0 and `python3 -m trader serve --live`.

| Variable                          | Default                    | Meaning                                                  |
|-----------------------------------|----------------------------|----------------------------------------------------------|
| TRADER_MODE                       | PAPER                      | PAPER (simulated exchange at Breeze prices) / LIVE (Zerodha) |
| ENABLE_LIVE_TRADING               | false                      | must be true for LIVE (same switch as the live engine)   |
| TRADER_DB                         | data/trader/trades.sqlite  | trades, orders, audit events (SQLite, WAL)               |
| TRADER_AUDIT_DIR                  | logs/trader                | JSONL mirror of the audit events                         |
| TRADER_PORT                       | 8765                       | web UI port; the server only binds 127.0.0.1             |
| TRADER_UNDERLYINGS                | NIFTY,BANKNIFTY,FINNIFTY,SENSEX,CRUDEOIL,CRUDEOILM,GOLDM | instruments offered in the UI (MCX ones need MARKET_DATA_PROVIDER=KITE for prices) |
| TRADER_PRODUCT                    | MIS                        | default product for new trades: MIS or NRML              |
| **Monitoring**                    |                            |                                                          |
| TRADER_POLL_SECONDS               | 3                          | monitor loop: order book + positions sync, rules         |
| TRADER_RECONCILE_CONFIRMATIONS    | 2                          | a position mismatch must repeat on this many syncs before it is acted on |
| TRADER_ORDER_LOOKUP_GRACE_SECONDS | 15                         | an order whose placement is uncertain is only declared "never placed" after it has been missing from the order book for this long |
| TRADER_QUOTE_INTERVAL_SECONDS     | 15                         | Breeze price refresh per symbol while a target / trailing / partial / software stop needs it |
| TRADER_QUOTE_SLOW_SECONDS         | 60                         | refresh when only the resting SL order protects the trade (price is for display / gap check) |
| TRADER_BREEZE_DAILY_BUDGET        | 2000                       | Breeze calls/day this process may use (counted in TRADER_DB); refreshes are paced so it lasts to 15:30 |
| TRADER_BREEZE_RESERVE             | 100                        | calls kept back for exits / previews when pacing          |
| TRADER_EXIT_BUFFER_PCT            | 2.0                        | exit/partial LIMIT = LTP -/+ this % (marketable)         |
| TRADER_SL_LIMIT_BUFFER_PCT        | 5.0                        | resting SL order: limit = trigger +/- this %             |
| TRADER_EXIT_REPRICE_SECONDS       | 10                         | re-price an unfilled exit after this long                |
| TRADER_MAX_EXIT_REPRICES          | 5                          | then CRITICAL alert (the order stays working)            |
| TRADER_SL_MAX_MODIFICATIONS       | 20                         | Kite allows ~25 modifications per order; replace the SL order after this many |
| TRADER_STOP_GRACE_SECONDS         | 10                         | LTP beyond SL this long while the SL order is unfilled -> make it marketable |
| **Risk (checked server-side before every entry)** |            |                                                          |
| TRADER_MAX_OPEN_TRADES            | 3                          | simultaneous trades (pending entries count)              |
| TRADER_MAX_LOTS_PER_TRADE         | 10                         |                                                          |
| TRADER_MAX_QTY_PER_TRADE          | 1000                       | units                                                    |
| TRADER_MAX_ORDER_VALUE            | 500000                     | entry price x quantity (0 = off)                         |
| TRADER_MAX_LOSS_PER_TRADE         | 10000                      | abs(entry - SL) x quantity                               |
| TRADER_MAX_DAILY_LOSS             | 20000                      | realised + unrealised today <= -this: new trades blocked (0 = off) |
| TRADER_MAX_DAILY_PROFIT           | 0                          | >= this: new trades blocked (0 = off)                    |
| TRADER_SQUARE_OFF_ON_DAILY_LIMIT  | false                      | also exit every open trade when a daily limit is hit     |
| TRADER_MAX_TRADES_PER_DAY         | 10                         | confirmed entries today                                  |
| TRADER_TRADING_START              | 09:15                      | no entries before (blank = off)                          |
| TRADER_TRADING_END                | 15:00                      | no entries after (blank = off)                           |
| TRADER_SQUARE_OFF_TIME            | 15:15                      | every open trade is exited (AUTO_EXIT); blank = off, allowed for NRML only |
| TRADER_MCX_TRADING_START / _END   | 09:00 / 23:15              | entry window for MCX underlyings (CRUDEOIL, CRUDEOILM, GOLDM) |
| TRADER_MCX_SQUARE_OFF_TIME        | 23:20                      | square-off for MCX trades (MCX closes 23:30, 23:55 in US winter) |
| TRADER_LOT_UNITS_<NAME>           | CRUDEOIL 100, CRUDEOILM 10, GOLDM 10 | units per MCX lot (Kite quantities are in lots; P&L needs units) |
| TRADER_MAX_ENTRY_DEVIATION_PCT    | 20                         | entry limit vs Breeze LTP (fat-finger guard; 0 = off)    |
| TRADER_REQUIRE_LTP_FOR_ENTRY      | true                       | refuse entries when no Breeze price is available         |
| TRADER_FREEZE_QTY_<UNDERLYING>    | NIFTY 1800, BANKNIFTY 900, FINNIFTY 1800, SENSEX 1000 | orders above this are refused (no slicing) |
| TRADER_CONFIRM_TOKEN_SECONDS      | 120                        | a preview/confirm token expires after this               |
| **Paper**                         |                            |                                                          |
| TRADER_PAPER_STATE                | data/trader/paper_exchange.json | simulated exchange: orders + positions (edit it to rehearse a manual exit) |
| TRADER_PAPER_SLIPPAGE_POINTS      | 0.0                        | per unit on marketable fills                             |
| TRADER_PAPER_QUOTES               | breeze                     | breeze, or manual (type prices in the UI: rehearsal without a Breeze session) |
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

from zerodha.config import _env_value, read_env_file

MODES = ("PAPER", "LIVE")
DEFAULT_FREEZE = {"NIFTY": 1800, "BANKNIFTY": 900, "FINNIFTY": 1800, "MIDCPNIFTY": 2800, "SENSEX": 1000,
                  "BANKEX": 900,
                  # MCX, in units (barrels / 10 g): max order size per trade, 100 lots of each
                  "CRUDEOIL": 10000, "CRUDEOILM": 1000, "GOLDM": 1000}
BFO_UNDERLYINGS = {"SENSEX", "BANKEX", "SENSEX50"}
# MCX commodity options. Kite's instrument dump says lot_size 1 and Kite takes MCX order quantities in LOTS,
# but a lot is many units: premium is quoted per barrel (crude) / per 10 g (gold), so P&L per lot = price x
# units. The trader works in units everywhere (quantity = lots x units, P&L = qty x price, like NFO) and
# converts to lots only at the Kite boundary (trader/broker.py). TRADER_LOT_UNITS_<NAME> overrides / adds one.
MCX_LOT_UNITS = {"CRUDEOIL": 100, "CRUDEOILM": 10, "GOLDM": 10}


def exchange_for(underlying: str) -> str:
    u = underlying.upper()
    return "BFO" if u in BFO_UNDERLYINGS else "MCX" if u in MCX_LOT_UNITS else "NFO"


def _bool(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _opt_time(v: str | None) -> time | None:
    v = (v or "").strip()
    if not v:
        return None
    h, m = v.split(":")
    return time(int(h), int(m))


def load_env(env_file: str | Path | None = ".env", environ: dict | None = None) -> dict[str, str]:
    env = dict(read_env_file(env_file)) if env_file else {}
    env.update({k: _env_value(str(v)) for k, v in (os.environ if environ is None else environ).items()})
    return env


@dataclass(frozen=True)
class TraderConfig:
    mode: str = "PAPER"
    enable_live_trading: bool = False
    db_path: Path = Path("data/trader/trades.sqlite")
    audit_dir: Path = Path("logs/trader")
    port: int = 8765
    underlyings: tuple[str, ...] = ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "CRUDEOIL", "CRUDEOILM", "GOLDM")
    product: str = "MIS"
    # monitoring
    poll_seconds: float = 3.0
    reconcile_confirmations: int = 2
    order_lookup_grace_s: float = 15.0
    quote_ttl_s: float = 15.0
    quote_slow_s: float = 60.0
    breeze_daily_budget: int = 2000
    breeze_reserve: int = 100
    exit_buffer_pct: float = 2.0
    sl_limit_buffer_pct: float = 5.0
    exit_reprice_s: float = 10.0
    max_exit_reprices: int = 5
    sl_max_modifications: int = 20
    stop_grace_s: float = 10.0
    # risk
    max_open_trades: int = 3
    max_lots_per_trade: int = 10
    max_qty_per_trade: int = 1000
    max_order_value: float = 500_000.0
    max_loss_per_trade: float = 10_000.0
    max_daily_loss: float = 20_000.0
    max_daily_profit: float = 0.0
    square_off_on_daily_limit: bool = False
    max_trades_per_day: int = 10
    trading_start: time | None = time(9, 15)
    trading_end: time | None = time(15, 0)
    square_off_time: time | None = time(15, 15)
    # MCX runs 09:00-23:30 (23:55 when the US is off daylight saving): its own entry window / square-off
    mcx_trading_start: time | None = time(9, 0)
    mcx_trading_end: time | None = time(23, 15)
    mcx_square_off_time: time | None = time(23, 20)
    lot_units: dict = field(default_factory=lambda: dict(MCX_LOT_UNITS))
    max_entry_deviation_pct: float = 20.0
    require_ltp_for_entry: bool = True
    freeze_qty: dict = field(default_factory=lambda: dict(DEFAULT_FREEZE))
    confirm_token_s: float = 120.0
    # paper
    paper_state: Path = Path("data/trader/paper_exchange.json")
    paper_slippage: float = 0.0
    paper_quotes: str = "breeze"         # breeze | manual (set prices in the UI; rehearsal without Breeze)
    engine_trading_mode: str = ""        # TRADING_MODE (live/ engine's switch), only read to explain mistakes

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError(f"TRADER_MODE must be one of {MODES}, not {self.mode!r}")
        if self.product not in ("MIS", "NRML"):
            raise ValueError(f"TRADER_PRODUCT must be MIS or NRML, not {self.product!r}")
        if self.square_off_time is None and self.product == "MIS":
            raise ValueError("TRADER_SQUARE_OFF_TIME may only be blank when TRADER_PRODUCT=NRML")
        if self.reconcile_confirmations < 1:
            raise ValueError("TRADER_RECONCILE_CONFIRMATIONS must be >= 1")

    @property
    def live(self) -> bool:
        return self.mode == "LIVE"

    def freeze_for(self, underlying: str) -> int:
        return int(self.freeze_qty.get(underlying.upper(), 1800))

    def session_for(self, underlying: str) -> tuple[time | None, time | None, time | None]:
        """(entry start, entry end, square-off) for this underlying's exchange."""
        if exchange_for(underlying) == "MCX":
            return self.mcx_trading_start, self.mcx_trading_end, self.mcx_square_off_time
        return self.trading_start, self.trading_end, self.square_off_time

    @classmethod
    def from_env(cls, env_file: str | Path | None = ".env", environ: dict | None = None) -> "TraderConfig":
        env = load_env(env_file, environ)
        d = cls()
        g = lambda k, default: env.get(k, "").strip() or default  # noqa: E731  (blank = default)
        freeze = dict(DEFAULT_FREEZE)
        for k, v in env.items():
            if k.startswith("TRADER_FREEZE_QTY_") and v.strip():
                freeze[k[len("TRADER_FREEZE_QTY_"):].upper()] = int(v)
        times = {k: (_opt_time(env[k]) if k in env else default) for k, default in
                 (("TRADER_TRADING_START", d.trading_start), ("TRADER_TRADING_END", d.trading_end),
                  ("TRADER_SQUARE_OFF_TIME", d.square_off_time), ("TRADER_MCX_TRADING_START", d.mcx_trading_start),
                  ("TRADER_MCX_TRADING_END", d.mcx_trading_end),
                  ("TRADER_MCX_SQUARE_OFF_TIME", d.mcx_square_off_time))}
        units = dict(MCX_LOT_UNITS)
        for k, v in env.items():
            if k.startswith("TRADER_LOT_UNITS_") and v.strip():
                units[k[len("TRADER_LOT_UNITS_"):].upper()] = int(v)
        return cls(
            mode=g("TRADER_MODE", "PAPER").strip().strip("'\"").upper(),
            enable_live_trading=_bool(g("ENABLE_LIVE_TRADING", "false")),
            db_path=Path(g("TRADER_DB", str(d.db_path))),
            audit_dir=Path(g("TRADER_AUDIT_DIR", str(d.audit_dir))),
            port=int(g("TRADER_PORT", d.port)),
            underlyings=tuple(u.strip().upper() for u in g("TRADER_UNDERLYINGS", ",".join(d.underlyings)).split(",")
                              if u.strip()),
            product=g("TRADER_PRODUCT", d.product).upper(),
            poll_seconds=float(g("TRADER_POLL_SECONDS", d.poll_seconds)),
            reconcile_confirmations=int(g("TRADER_RECONCILE_CONFIRMATIONS", d.reconcile_confirmations)),
            order_lookup_grace_s=float(g("TRADER_ORDER_LOOKUP_GRACE_SECONDS", d.order_lookup_grace_s)),
            quote_ttl_s=float(g("TRADER_QUOTE_INTERVAL_SECONDS", d.quote_ttl_s)),
            quote_slow_s=float(g("TRADER_QUOTE_SLOW_SECONDS", d.quote_slow_s)),
            breeze_daily_budget=int(g("TRADER_BREEZE_DAILY_BUDGET", d.breeze_daily_budget)),
            breeze_reserve=int(g("TRADER_BREEZE_RESERVE", d.breeze_reserve)),
            exit_buffer_pct=float(g("TRADER_EXIT_BUFFER_PCT", d.exit_buffer_pct)),
            sl_limit_buffer_pct=float(g("TRADER_SL_LIMIT_BUFFER_PCT", d.sl_limit_buffer_pct)),
            exit_reprice_s=float(g("TRADER_EXIT_REPRICE_SECONDS", d.exit_reprice_s)),
            max_exit_reprices=int(g("TRADER_MAX_EXIT_REPRICES", d.max_exit_reprices)),
            sl_max_modifications=int(g("TRADER_SL_MAX_MODIFICATIONS", d.sl_max_modifications)),
            stop_grace_s=float(g("TRADER_STOP_GRACE_SECONDS", d.stop_grace_s)),
            max_open_trades=int(g("TRADER_MAX_OPEN_TRADES", d.max_open_trades)),
            max_lots_per_trade=int(g("TRADER_MAX_LOTS_PER_TRADE", d.max_lots_per_trade)),
            max_qty_per_trade=int(g("TRADER_MAX_QTY_PER_TRADE", d.max_qty_per_trade)),
            max_order_value=float(g("TRADER_MAX_ORDER_VALUE", d.max_order_value)),
            max_loss_per_trade=float(g("TRADER_MAX_LOSS_PER_TRADE", d.max_loss_per_trade)),
            max_daily_loss=float(g("TRADER_MAX_DAILY_LOSS", d.max_daily_loss)),
            max_daily_profit=float(g("TRADER_MAX_DAILY_PROFIT", d.max_daily_profit)),
            square_off_on_daily_limit=_bool(g("TRADER_SQUARE_OFF_ON_DAILY_LIMIT", "false")),
            max_trades_per_day=int(g("TRADER_MAX_TRADES_PER_DAY", d.max_trades_per_day)),
            trading_start=times["TRADER_TRADING_START"],
            trading_end=times["TRADER_TRADING_END"],
            square_off_time=times["TRADER_SQUARE_OFF_TIME"],
            max_entry_deviation_pct=float(g("TRADER_MAX_ENTRY_DEVIATION_PCT", d.max_entry_deviation_pct)),
            require_ltp_for_entry=_bool(g("TRADER_REQUIRE_LTP_FOR_ENTRY", "true")),
            freeze_qty=freeze,
            mcx_trading_start=times["TRADER_MCX_TRADING_START"],
            mcx_trading_end=times["TRADER_MCX_TRADING_END"],
            mcx_square_off_time=times["TRADER_MCX_SQUARE_OFF_TIME"],
            lot_units=units,
            confirm_token_s=float(g("TRADER_CONFIRM_TOKEN_SECONDS", d.confirm_token_s)),
            paper_state=Path(g("TRADER_PAPER_STATE", str(d.paper_state))),
            paper_slippage=float(g("TRADER_PAPER_SLIPPAGE_POINTS", d.paper_slippage)),
            paper_quotes=g("TRADER_PAPER_QUOTES", d.paper_quotes).lower(),
            engine_trading_mode=g("TRADING_MODE", "").upper(),
        )
