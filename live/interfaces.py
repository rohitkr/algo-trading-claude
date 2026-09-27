"""The four seams of the live engine and the records passed between them.

    MarketDataProvider -> Strategy -> Signal -> RiskManager -> ExecutionBroker

The engine (live/engine.py) only talks to these protocols, so the data source
(Breeze today, DuckDB replay in BACKTEST mode) and the execution venue (Zerodha
live, the paper simulator) can each be swapped without touching the others.
Orders cross the broker seam as broker-neutral strategy_signals.OrderIntents.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol

import pandas as pd

from strategy_signals import Action
from strategy_signals.execution import ExecutionBroker, ExecutionResult, LegFill, OptionKey  # noqa: F401  (re-exported)
from trading_data.storage import OptionContract

def contract_key(c: OptionContract) -> OptionKey:
    return (c.underlying, c.expiry, float(c.strike), c.right)


# -- market data ----------------------------------------------------------------------------
class MarketDataProvider(Protocol):
    """Completed 1-minute bars and latest prices. A bar stamped T covers [T, T+1min) and is only
    returned once now >= T + 1 minute, so strategies never see an unfinished candle."""

    def spot_bars(self, day: date, now: datetime) -> pd.DataFrame: ...          # index ts; open/high/low/close
    def option_bars(self, contract: OptionContract, day: date, now: datetime) -> pd.DataFrame: ...
    def option_price(self, contract: OptionContract, now: datetime, fresh: bool = False) -> float | None: ...
    def api_budget_remaining(self) -> int | None: ...                          # None = unlimited (replay)


# -- strategy -------------------------------------------------------------------------------
@dataclass
class Signal:
    """A strategy's decision. ENTRY opens a short on `contract`; EXIT closes position_id."""
    strategy: str
    action: Action
    position_id: str
    contract: OptionContract
    ts: datetime                      # the bar that triggered it
    spot: float | None                # underlying close at that bar
    reason: str
    lots: int = 0
    side: str = "SELL"
    ref_price: float | None = None    # option price the backtest would use (bar close / stop price)
    stop: dict = field(default_factory=dict)   # {"basis": "spot"|"premium", "level": float, "pct": float}
    is_reentry: bool = False
    meta: dict = field(default_factory=dict)


class Strategy(Protocol):
    name: str

    def on_poll(self, ctx: "StrategyContext") -> list[Signal]: ...
    def on_entry_result(self, signal: Signal, ok: bool) -> None: ...   # entry executed or refused
    def get_state(self) -> dict: ...
    def set_state(self, state: dict) -> None: ...


@dataclass
class StrategyContext:
    now: datetime
    market: MarketDataProvider
    selector: "object"               # live.selection.ContractSelector

    @property
    def today(self) -> date:
        return self.now.date()

    def spot_bars(self) -> pd.DataFrame:
        return self.market.spot_bars(self.today, self.now)


# -- risk -----------------------------------------------------------------------------------
@dataclass
class RiskCheck:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class RiskDecision:
    ok: bool
    lots: int
    checks: list[RiskCheck] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    @property
    def failed(self) -> list[RiskCheck]:
        return [c for c in self.checks if not c.passed]


def contract_to_dict(c: OptionContract) -> dict:
    return {"underlying": c.underlying, "exchange": c.exchange, "expiry": c.expiry.isoformat(),
            "strike": c.strike, "right": c.right}


def contract_from_dict(d: dict) -> OptionContract:
    return OptionContract(d["underlying"], d["exchange"], date.fromisoformat(d["expiry"]), d["strike"], d["right"])
