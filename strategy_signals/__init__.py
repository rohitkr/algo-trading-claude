"""Broker-agnostic order intents emitted by strategies.

This package is the only contract between strategy/backtest code and any broker
adapter (e.g. ``zerodha/``). It has no third-party dependencies and imports
nothing else from this repository, so a broker package can be moved to its own
repo and depend on just this module.

A strategy says *what* position it wants (legs, direction, quantity) with an
``OrderIntent``; the broker adapter decides *how* (symbols, order types, leg
ordering, margin checks).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum

__all__ = ["Action", "Side", "Right", "LegRole", "OptionLeg", "OrderIntent"]


class Action(str, Enum):
    ENTRY = "ENTRY"     # open the position described by the legs
    EXIT = "EXIT"       # close the position previously opened under the same position_id


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class Right(str, Enum):
    CALL = "CALL"
    PUT = "PUT"


class LegRole(str, Enum):
    MAIN = "MAIN"       # the premium-selling (or directional) leg
    HEDGE = "HEDGE"     # protective wing; brokers open it first and close it last


@dataclass(frozen=True)
class OptionLeg:
    underlying: str             # e.g. "NIFTY"
    expiry: date
    strike: float
    right: Right
    side: Side                  # side of the ENTRY order for this leg
    quantity: int               # units (lots x lot size), not lots
    role: LegRole = LegRole.MAIN
    ref_price: float | None = None   # strategy's reference/limit price, if any

    def __post_init__(self):
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        object.__setattr__(self, "right", Right(self.right))
        object.__setattr__(self, "side", Side(self.side))
        object.__setattr__(self, "role", LegRole(self.role))

    @property
    def key(self) -> tuple:
        return (self.underlying, self.expiry, float(self.strike), self.right.value)


@dataclass(frozen=True)
class OrderIntent:
    """Open or close one multi-leg position.

    For EXIT intents the legs repeat the ENTRY legs (same ``side`` as at entry);
    the broker reverses them.
    """
    intent_id: str
    position_id: str
    action: Action
    legs: tuple[OptionLeg, ...]
    ts: datetime | None = None
    strategy: str = ""
    reason: str = ""
    meta: dict = field(default_factory=dict, compare=False, hash=False)

    def __post_init__(self):
        object.__setattr__(self, "action", Action(self.action))
        object.__setattr__(self, "legs", tuple(self.legs))
        if not self.legs:
            raise ValueError("an intent needs at least one leg")

    @property
    def hedges(self) -> list[OptionLeg]:
        return [l for l in self.legs if l.role is LegRole.HEDGE]

    @property
    def mains(self) -> list[OptionLeg]:
        return [l for l in self.legs if l.role is not LegRole.HEDGE]

    @property
    def is_hedged(self) -> bool:
        return bool(self.hedges)
