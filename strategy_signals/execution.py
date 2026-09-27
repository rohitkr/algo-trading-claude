"""Broker-neutral execution contract: what any execution adapter returns and must provide.

Adapters (zerodha/execution.py today) implement ExecutionBroker; the live
engine only depends on this module, so the execution venue can be replaced
without touching strategies, risk or the engine.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from . import OrderIntent

OptionKey = tuple  # (underlying, expiry, strike, right) == OptionLeg.key


@dataclass
class LegFill:
    key: OptionKey
    tradingsymbol: str
    side: str
    quantity: int                     # filled units
    average_price: float
    role: str = "MAIN"
    order_ids: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)


@dataclass
class ExecutionResult:
    intent_id: str
    ok: bool
    fills: list[LegFill] = field(default_factory=list)
    unwound: list[LegFill] = field(default_factory=list)
    message: str = ""
    uncertain: bool = False           # broker/network error: orders may or may not exist -> reconcile
    margin_required: float | None = None
    margin_available: float | None = None
    plan: list[str] = field(default_factory=list)

    def fill_for(self, key: OptionKey) -> LegFill | None:
        """All fills of one leg merged (quantity-weighted average price)."""
        legs = [f for f in self.fills if f.key == key]
        if not legs:
            return None
        qty = sum(f.quantity for f in legs)
        avg = sum(f.average_price * f.quantity for f in legs) / qty if qty else 0.0
        return LegFill(key, legs[0].tradingsymbol, legs[0].side, qty, round(avg, 4), legs[0].role,
                       [o for f in legs for o in f.order_ids], [s for f in legs for s in f.statuses])


class ExecutionBroker(Protocol):
    name: str
    live: bool                        # True only for an adapter that can place real orders

    def execute(self, intent: OrderIntent) -> ExecutionResult: ...
    def positions(self) -> dict[OptionKey, int]: ...          # net units (+long / -short) per option
    def open_orders(self) -> list[dict]: ...
    def available_margin(self) -> float: ...
    def required_margin(self, intent: OrderIntent) -> float: ...
