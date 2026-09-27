"""Turn backtest trades into broker-agnostic OrderIntents (strategy_signals).

Each position (a sold leg plus its optional wing) becomes an ENTRY intent at its
entry minute and an EXIT intent at its exit minute, in time order. A broker
adapter (e.g. zerodha.Executor) can replay these in paper mode, and a live
signal source would emit the same objects.
"""
from __future__ import annotations

from datetime import date

from strategy_signals import Action, LegRole, OptionLeg, OrderIntent, Right, Side

from .engine import Trade
from .hedged import position_id


def trade_leg(t: Trade, underlying: str, role: LegRole) -> OptionLeg:
    return OptionLeg(underlying=underlying, expiry=date.fromisoformat(t.expiry), strike=float(t.strike),
                     right=Right(t.right), side=Side(t.side), quantity=t.qty, role=role, ref_price=t.entry_price)


def trades_to_intents(trades: list[Trade], underlying: str = "NIFTY") -> list[OrderIntent]:
    groups: dict[str, list[Trade]] = {}
    for t in trades:
        groups.setdefault(position_id(t), []).append(t)
    intents: list[OrderIntent] = []
    for pid, legs in groups.items():
        main = next(t for t in legs if t.side == "SELL")
        order = [main] + [t for t in legs if t is not main]
        opt_legs = tuple(trade_leg(t, underlying, LegRole.MAIN if t is main else LegRole.HEDGE) for t in order)
        intents.append(OrderIntent(f"{pid}:entry", pid, Action.ENTRY, opt_legs, main.entry_ts, main.strategy,
                                   main.note))
        if main.exit_ts is not None:
            exit_legs = tuple(OptionLeg(l.underlying, l.expiry, l.strike, l.right, l.side, l.quantity, l.role,
                                        t.exit_price) for l, t in zip(opt_legs, order))
            intents.append(OrderIntent(f"{pid}:exit", pid, Action.EXIT, exit_legs, main.exit_ts, main.strategy,
                                       main.exit_reason))
    return sorted(intents, key=lambda i: (i.ts, 0 if i.action is Action.EXIT else 1))
