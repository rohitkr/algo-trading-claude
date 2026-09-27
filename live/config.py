"""Live-engine settings from the root .env (process environment overrides it). Nothing trading-related is hardcoded.

Safety: TRADING_MODE defaults to PAPER. A real order would need ALL of
    TRADING_MODE=LIVE, ENABLE_LIVE_TRADING=true, KITE_DRY_RUN=0 and the --live CLI flag
(and LIVE execution is not wired into the engine yet).

| Variable                      | Default        | Meaning                                                   |
|-------------------------------|----------------|-----------------------------------------------------------|
| TRADING_MODE                  | PAPER          | BACKTEST (DuckDB replay) / PAPER (Breeze data, simulated fills) / LIVE |
| ENABLE_LIVE_TRADING           | false          | must be true for LIVE                                     |
| STRATEGIES                    | positional,zerodte | which strategies run                                  |
| POSITION_MODE                 | NAKED          | NAKED (as backtested) or HEDGED (buy a wing per short)    |
| HEDGE_WIDTH                   | 200            | wing distance in points (HEDGED)                          |
| UNDERLYING                    | NIFTY          | [options.*] profile in config/settings.toml (expiry calendar, exchange) |
| LOT_SIZE                      | (settings.toml)| override the exchange lot size (NIFTY: 65)                |
| STRIKE_STEP                   | (settings.toml)| override the strike interval (NIFTY: 50); ATM = round(spot / step) x step |
| **Session**                   |                |                                                           |
| INTRADAY_ONLY                 | true           | no position survives FORCE_EXIT_TIME (positional exits same day; differs from the backtest) |
| ENTRY_START_TIME              | 09:15          | no new entries before this                                |
| ENTRY_END_TIME                | 15:00          | no new entries after this (signals after it are ignored, never opened) |
| FORCE_EXIT_TIME               | 15:15          | INTRADAY_ONLY: everything still open is squared off at this time |
| **Positional (backtest RangeBreakoutParams)** |  |                                                          |
| POSITIONAL_LOTS               | 5              | lots per trade (backtest: 5 x 65)                         |
| POSITIONAL_ITM_POINTS         | 100            | PUT at ATM+100 / CALL at ATM-100                          |
| POSITIONAL_SL_PCT             | 0.5            | underlying move against the entry spot                    |
| POSITIONAL_REENTRY            | true           | one re-entry at cost                                      |
| POSITIONAL_RANGE_START / _END | 09:15 / 11:15  | opening range (bars before RANGE_END)                     |
| POSITIONAL_LAST_ENTRY         | 15:00          | last breakout bar considered                              |
| POSITIONAL_EXIT_TIME          | 15:15          | expiry-day exit (INTRADAY_ONLY=false only)                |
| POSITIONAL_ACT_UNTIL          | 15:15          | stops / re-entries evaluated up to this bar each day      |
| POSITIONAL_MIN_RANGE_BARS     | 100            | data-quality guard on the range                           |
| POSITIONAL_NEW_SIGNAL_CANCELS_REENTRY | true   | a new breakout replaces a pending re-entry (INTRADAY_ONLY=false only) |
| **0DTE (backtest ZeroDteParams)** |            |                                                           |
| ZERODTE_LOTS                  | 5              |                                                           |
| ZERODTE_ITM_POINTS            | 100            | CALL at ATM-100, PUT at ATM+100 (0 = ATM straddle)        |
| ZERODTE_SL_PCT                | 30             | premium stop per leg                                      |
| ZERODTE_REENTRY               | true           | one re-entry per leg at cost                              |
| ZERODTE_LOOKBACK              | 8              | expiry days for the walk-forward entry time               |
| ZERODTE_FIRST_ENTRY / _LAST_ENTRY / _STEP_MINUTES | 09:20 / 14:30 / 10 | walk-forward candidate entry times |
| ZERODTE_EXIT_TIME             | 15:15          | time exit                                                 |
| ZERODTE_ENTRY_TIME            | (auto)         | HH:MM override of the walk-forward choice                 |
| ZERODTE_QUOTE_STOPS           | true           | also check stops on live quotes between bars              |
| ZERODTE_MAX_ENTRY_DELAY_SECONDS | 180          | give up the entry if the entry-minute bar is this late    |
| **Risk**                      |                |                                                           |
| RISK_CAPITAL                  | 1000000        | capital for sizing (PAPER funds; LIVE uses min(this, broker margin)) |
| RISK_MAX_RISK_PER_TRADE       | 60000          | max estimated loss at stop (naked) / max loss (hedged)    |
| RISK_MAX_TRADES_PER_DAY       | 6              | entries incl. re-entries                                  |
| RISK_MAX_DAILY_LOSS_ENABLED   | true           |                                                           |
| RISK_MAX_DAILY_LOSS           | 50000          | realised + unrealised today; blocks new entries for the day |
| RISK_MAX_DAILY_PROFIT_ENABLED | false          |                                                           |
| RISK_MAX_DAILY_PROFIT         | 0              | same P&L measure; squares off everything, MAX_PROFIT_REACHED, no more trades today |
| RISK_MAX_OPEN_POSITIONS       | 3              |                                                           |
| RISK_MAX_LOTS_PER_TRADE       | 5              |                                                           |
| RISK_MAX_QTY_PER_TRADE        | 325            | units                                                     |
| RISK_MAX_REENTRIES            | 1              | per original signal                                       |
| RISK_MARGIN_UTILISATION_PCT   | 80             | use at most this % of available margin                    |
| RISK_POSITIONAL_DELTA         | 1.0            | option move per point of underlying for the risk estimate |
| RISK_MAX_DATA_LAG_SECONDS     | 180            | no entries when the newest bar / the signal bar is older  |
| RISK_MIN_API_BUDGET           | 300            | no entries when fewer Breeze calls remain today           |
| RISK_SQUARE_OFF_ON_DATA_OUTAGE| false          | flatten if data is down longer than below                 |
| RISK_DATA_OUTAGE_SECONDS      | 600            |                                                           |
| **Engine**                    |                |                                                           |
| ENGINE_POLL_SECONDS           | 10             | main loop interval                                        |
| ENGINE_STOP_TIME              | 15:35          | the run loop ends after this                              |
| ENGINE_RECONCILE_SECONDS      | 60             | broker position reconciliation interval                   |
| ENGINE_RECONCILE_CONFIRMATIONS| 2              | a mismatch must repeat this many checks before it is acted on (broker position lag) |
| ENGINE_HALT_ON_UNKNOWN_POSITIONS | true        | an UNDERLYING option position the engine did not open halts new entries |
| ENGINE_EXIT_RETRY_LIMIT       | 5              | failed exits retried every poll, then CRITICAL + halt     |
| ENGINE_BAR_REFETCH_SECONDS    | 15             | Breeze: min seconds between bar requests per instrument   |
| ENGINE_QUOTE_TTL_SECONDS      | 20             | Breeze: quote cache per contract                          |
| ENGINE_KILL_FILE              | data/live/KILL | create it to flatten everything and stop                  |
| ENGINE_STATE_DIR              | data/live      | state_<mode>.json                                         |
| ENGINE_AUDIT_DIR              | logs/live      | audit_<mode>_<date>.jsonl + trades_<mode>.csv             |
| **Paper simulation**          |                |                                                           |
| PAPER_SLIPPAGE_POINTS         | 0.5            | per unit per side (backtest CostModel)                    |
| PAPER_SPAN_PCT / PAPER_EXPOSURE_PCT | 9 / 2    | margin estimate (% of notional per short)                 |

Order handling (limit buffer, freeze qty, fill timeout, re-prices, margin buffer, product) is KITE_* in
zerodha/config.py; the expiry calendar and holidays are config/settings.toml + config/holidays.toml.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path

MODES = ("BACKTEST", "PAPER", "LIVE")


def _bool(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _time(v: str) -> time:
    h, m = str(v).strip().split(":")
    return time(int(h), int(m))


def load_env(env_file: str | Path | None = ".env", environ: dict | None = None) -> dict[str, str]:
    """.env values overlaid by the process environment (inline # comments stripped)."""
    env: dict[str, str] = {}
    if env_file and Path(env_file).exists():
        from dotenv import dotenv_values
        env.update({k: v for k, v in dotenv_values(env_file).items() if v is not None})
    env.update(os.environ if environ is None else environ)
    return {k: str(v).split(" #")[0].strip() for k, v in env.items()}


@dataclass(frozen=True)
class EngineConfig:
    mode: str = "PAPER"
    enable_live_trading: bool = False
    strategies: tuple[str, ...] = ("positional", "zerodte")
    position_mode: str = "NAKED"
    hedge_width: int = 200
    underlying: str = "NIFTY"
    lot_size: int | None = None          # None = settings.toml [options.<underlying>].lot_size
    strike_step: int | None = None
    # session
    intraday_only: bool = True
    entry_start_time: time = time(9, 15)
    entry_end_time: time = time(15, 0)
    force_exit_time: time = time(15, 15)
    # positional (defaults = backtest RangeBreakoutParams)
    positional_lots: int = 5
    positional_itm_points: int = 100
    positional_sl_pct: float = 0.5
    positional_reentry: bool = True
    positional_range_start: time = time(9, 15)
    positional_range_end: time = time(11, 15)
    positional_last_entry: time = time(15, 0)
    positional_exit_time: time = time(15, 15)
    positional_act_until: time = time(15, 15)
    positional_min_range_bars: int = 100
    positional_new_signal_cancels_reentry: bool = True
    # 0DTE (defaults = backtest ZeroDteParams)
    zerodte_lots: int = 5
    zerodte_itm_points: int = 100
    zerodte_sl_pct: float = 30.0
    zerodte_reentry: bool = True
    zerodte_lookback: int = 8
    zerodte_first_entry: time = time(9, 20)
    zerodte_last_entry: time = time(14, 30)
    zerodte_step_minutes: int = 10
    zerodte_exit_time: time = time(15, 15)
    zerodte_entry_time: time | None = None
    zerodte_quote_stops: bool = True
    zerodte_max_entry_delay_s: float = 180.0
    # risk
    capital: float = 1_000_000.0
    max_risk_per_trade: float = 60_000.0
    max_trades_per_day: int = 6
    max_daily_loss_enabled: bool = True
    max_daily_loss: float = 50_000.0
    max_daily_profit_enabled: bool = False
    max_daily_profit: float = 0.0
    max_open_positions: int = 3
    max_lots_per_trade: int = 5
    max_qty_per_trade: int = 325
    max_reentries: int = 1
    margin_utilisation_pct: float = 80.0
    positional_delta: float = 1.0
    max_data_lag_s: float = 180.0
    min_api_budget: int = 300
    square_off_on_data_outage: bool = False
    data_outage_s: float = 600.0
    # engine
    poll_seconds: float = 10.0
    stop_time: time = time(15, 35)
    reconcile_seconds: float = 60.0
    reconcile_confirmations: int = 2
    halt_on_unknown_positions: bool = True
    exit_retry_limit: int = 5
    bar_refetch_s: float = 15.0
    quote_ttl_s: float = 20.0
    kill_file: Path = Path("data/live/KILL")
    state_dir: Path = Path("data/live")
    audit_dir: Path = Path("logs/live")
    # paper simulation
    paper_slippage_points: float = 0.5
    paper_span_pct: float = 9.0
    paper_exposure_pct: float = 2.0

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError(f"TRADING_MODE must be one of {MODES}, not {self.mode!r}")
        if self.position_mode not in ("NAKED", "HEDGED"):
            raise ValueError(f"POSITION_MODE must be NAKED or HEDGED, not {self.position_mode!r}")
        if self.position_mode == "HEDGED" and self.hedge_width <= 0:
            raise ValueError("HEDGE_WIDTH must be positive in HEDGED mode")
        unknown = set(self.strategies) - {"positional", "zerodte"}
        if unknown:
            raise ValueError(f"unknown STRATEGIES {sorted(unknown)}; use positional,zerodte")
        if not (self.entry_start_time <= self.entry_end_time <= self.force_exit_time):
            raise ValueError("need ENTRY_START_TIME <= ENTRY_END_TIME <= FORCE_EXIT_TIME")
        if self.max_daily_profit_enabled and self.max_daily_profit <= 0:
            raise ValueError("RISK_MAX_DAILY_PROFIT must be positive when RISK_MAX_DAILY_PROFIT_ENABLED=true")
        if self.max_daily_loss_enabled and self.max_daily_loss <= 0:
            raise ValueError("RISK_MAX_DAILY_LOSS must be positive when RISK_MAX_DAILY_LOSS_ENABLED=true")

    @property
    def hedged(self) -> bool:
        return self.position_mode == "HEDGED"

    @property
    def live_armed(self) -> bool:
        return self.mode == "LIVE" and self.enable_live_trading

    @classmethod
    def from_env(cls, env_file: str | Path | None = ".env", environ: dict | None = None, **overrides) -> "EngineConfig":
        e = load_env(env_file, environ)
        g = e.get
        d = cls.__dataclass_fields__

        def num(key, name, typ=float):
            v = g(key, "")
            return typ(v) if v != "" else d[name].default

        def tm(key, name, fallback_key=None):
            v = g(key, "") or (g(fallback_key, "") if fallback_key else "")
            return _time(v) if v else d[name].default

        def flag(key, name):
            v = g(key, "")
            return _bool(v) if v != "" else d[name].default

        entry = g("ZERODTE_ENTRY_TIME", "")
        kw = dict(
            mode=(g("TRADING_MODE", "") or "PAPER").upper(),
            enable_live_trading=flag("ENABLE_LIVE_TRADING", "enable_live_trading"),
            strategies=tuple(s.strip().lower() for s in (g("STRATEGIES", "") or "positional,zerodte").split(",")
                             if s.strip()),
            position_mode=(g("POSITION_MODE", "") or "NAKED").upper(),
            hedge_width=num("HEDGE_WIDTH", "hedge_width", int),
            underlying=(g("UNDERLYING", "") or "NIFTY").upper(),
            lot_size=num("LOT_SIZE", "lot_size", int),
            strike_step=num("STRIKE_STEP", "strike_step", int),
            intraday_only=flag("INTRADAY_ONLY", "intraday_only"),
            entry_start_time=tm("ENTRY_START_TIME", "entry_start_time"),
            entry_end_time=tm("ENTRY_END_TIME", "entry_end_time", "RISK_NO_NEW_ENTRIES_AFTER"),
            force_exit_time=tm("FORCE_EXIT_TIME", "force_exit_time"),
            positional_lots=num("POSITIONAL_LOTS", "positional_lots", int),
            positional_itm_points=num("POSITIONAL_ITM_POINTS", "positional_itm_points", int),
            positional_sl_pct=num("POSITIONAL_SL_PCT", "positional_sl_pct"),
            positional_reentry=flag("POSITIONAL_REENTRY", "positional_reentry"),
            positional_range_start=tm("POSITIONAL_RANGE_START", "positional_range_start"),
            positional_range_end=tm("POSITIONAL_RANGE_END", "positional_range_end"),
            positional_last_entry=tm("POSITIONAL_LAST_ENTRY", "positional_last_entry"),
            positional_exit_time=tm("POSITIONAL_EXIT_TIME", "positional_exit_time"),
            positional_act_until=tm("POSITIONAL_ACT_UNTIL", "positional_act_until"),
            positional_min_range_bars=num("POSITIONAL_MIN_RANGE_BARS", "positional_min_range_bars", int),
            positional_new_signal_cancels_reentry=flag("POSITIONAL_NEW_SIGNAL_CANCELS_REENTRY",
                                                       "positional_new_signal_cancels_reentry"),
            zerodte_lots=num("ZERODTE_LOTS", "zerodte_lots", int),
            zerodte_itm_points=num("ZERODTE_ITM_POINTS", "zerodte_itm_points", int),
            zerodte_sl_pct=num("ZERODTE_SL_PCT", "zerodte_sl_pct"),
            zerodte_reentry=flag("ZERODTE_REENTRY", "zerodte_reentry"),
            zerodte_lookback=num("ZERODTE_LOOKBACK", "zerodte_lookback", int),
            zerodte_first_entry=tm("ZERODTE_FIRST_ENTRY", "zerodte_first_entry"),
            zerodte_last_entry=tm("ZERODTE_LAST_ENTRY", "zerodte_last_entry"),
            zerodte_step_minutes=num("ZERODTE_STEP_MINUTES", "zerodte_step_minutes", int),
            zerodte_exit_time=tm("ZERODTE_EXIT_TIME", "zerodte_exit_time"),
            zerodte_entry_time=_time(entry) if entry else None,
            zerodte_quote_stops=flag("ZERODTE_QUOTE_STOPS", "zerodte_quote_stops"),
            zerodte_max_entry_delay_s=num("ZERODTE_MAX_ENTRY_DELAY_SECONDS", "zerodte_max_entry_delay_s"),
            capital=num("RISK_CAPITAL", "capital"),
            max_risk_per_trade=num("RISK_MAX_RISK_PER_TRADE", "max_risk_per_trade"),
            max_trades_per_day=num("RISK_MAX_TRADES_PER_DAY", "max_trades_per_day", int),
            max_daily_loss_enabled=flag("RISK_MAX_DAILY_LOSS_ENABLED", "max_daily_loss_enabled"),
            max_daily_loss=num("RISK_MAX_DAILY_LOSS", "max_daily_loss"),
            max_daily_profit_enabled=flag("RISK_MAX_DAILY_PROFIT_ENABLED", "max_daily_profit_enabled"),
            max_daily_profit=num("RISK_MAX_DAILY_PROFIT", "max_daily_profit"),
            max_open_positions=num("RISK_MAX_OPEN_POSITIONS", "max_open_positions", int),
            max_lots_per_trade=num("RISK_MAX_LOTS_PER_TRADE", "max_lots_per_trade", int),
            max_qty_per_trade=num("RISK_MAX_QTY_PER_TRADE", "max_qty_per_trade", int),
            max_reentries=num("RISK_MAX_REENTRIES", "max_reentries", int),
            margin_utilisation_pct=num("RISK_MARGIN_UTILISATION_PCT", "margin_utilisation_pct"),
            positional_delta=num("RISK_POSITIONAL_DELTA", "positional_delta"),
            max_data_lag_s=num("RISK_MAX_DATA_LAG_SECONDS", "max_data_lag_s"),
            min_api_budget=num("RISK_MIN_API_BUDGET", "min_api_budget", int),
            square_off_on_data_outage=flag("RISK_SQUARE_OFF_ON_DATA_OUTAGE", "square_off_on_data_outage"),
            data_outage_s=num("RISK_DATA_OUTAGE_SECONDS", "data_outage_s"),
            poll_seconds=num("ENGINE_POLL_SECONDS", "poll_seconds"),
            stop_time=tm("ENGINE_STOP_TIME", "stop_time"),
            reconcile_seconds=num("ENGINE_RECONCILE_SECONDS", "reconcile_seconds"),
            reconcile_confirmations=num("ENGINE_RECONCILE_CONFIRMATIONS", "reconcile_confirmations", int),
            halt_on_unknown_positions=flag("ENGINE_HALT_ON_UNKNOWN_POSITIONS", "halt_on_unknown_positions"),
            exit_retry_limit=num("ENGINE_EXIT_RETRY_LIMIT", "exit_retry_limit", int),
            bar_refetch_s=num("ENGINE_BAR_REFETCH_SECONDS", "bar_refetch_s"),
            quote_ttl_s=num("ENGINE_QUOTE_TTL_SECONDS", "quote_ttl_s"),
            kill_file=Path(g("ENGINE_KILL_FILE", "") or "data/live/KILL"),
            state_dir=Path(g("ENGINE_STATE_DIR", "") or "data/live"),
            audit_dir=Path(g("ENGINE_AUDIT_DIR", "") or "logs/live"),
            paper_slippage_points=num("PAPER_SLIPPAGE_POINTS", "paper_slippage_points"),
            paper_span_pct=num("PAPER_SPAN_PCT", "paper_span_pct"),
            paper_exposure_pct=num("PAPER_EXPOSURE_PCT", "paper_exposure_pct"),
        )
        kw.update(overrides)
        return cls(**kw)
