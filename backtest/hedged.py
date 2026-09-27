"""Hedged (credit-spread) variants of the option-selling strategies.

A HedgedStrategy wraps an unchanged naked strategy: it runs it to get the sold
legs (identical entries, exits, stops and re-entries) and adds one BOUGHT wing
per sold leg, `wing_points` further out of the money, opened at the sold leg's
entry minute and closed at its exit minute. A sold PUT at K gets a bought PUT
at K - wing_points; a sold CALL at K gets a bought CALL at K + wing_points.

Entry and exit decisions depend only on the index (positional) or on the sold
leg's premium (0DTE), so the wing never changes them; it only changes P&L,
costs, margin and maximum loss. The 0DTE walk-forward entry time is therefore
also the naked strategy's choice.

Wing prices come from the same real option candles (DataFeed.option /
price_at) as the sold legs. A wing with no price at entry or exit is listed in
`missing` and its position is kept unhedged, so callers can report it.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date

from .engine import Trade

WING_SEP = "~W"


@dataclass(frozen=True)
class HedgeParams:
    wing_points: int = 200      # distance of the bought wing from the sold strike (NIFTY points)


def wing_strike(strike: float, right: str, width: int) -> float:
    return strike - width if right == "PUT" else strike + width


def position_id(trade: Trade) -> str:
    """Id shared by a sold leg and its wing."""
    return trade.trade_id.split(WING_SEP)[0]


@dataclass
class HedgedStrategy:
    base: object                                   # RangeBreakoutSeller | ZeroDteStraddleSeller (fresh instance)
    hedge: HedgeParams = field(default_factory=HedgeParams)
    missing: list[dict] = field(default_factory=list)

    @property
    def name(self) -> str:
        return f"{self.base.name} + {self.hedge.wing_points}pt wing"

    @property
    def feed(self):
        return self.base.feed

    def __getattr__(self, item):         # expose the base strategy's params/signals/grid/choices to reports
        if item == "base":
            raise AttributeError(item)
        return getattr(self.base, item)

    def run(self, start: date, end: date) -> list[Trade]:
        out: list[Trade] = []
        for t in self.base.run(start, end):
            short = replace(t, strategy=self.name)
            out.append(short)
            wing = self._wing(short)
            if wing is not None:
                out.append(wing)
        return out

    def _wing(self, short: Trade) -> Trade | None:
        from .data import price_at   # local: keeps this module importable without DuckDB/pandas feeds in tests

        feed = self.feed
        day, expiry = date.fromisoformat(short.day), date.fromisoformat(short.expiry)
        days = [day] if day == expiry else feed.cal.trading_days(day, expiry)
        strike = wing_strike(short.strike, short.right, self.hedge.wing_points)
        opt = feed.option(expiry, strike, short.right, days)
        entry = price_at(opt, short.entry_ts)
        exit_ = price_at(opt, short.exit_ts) if short.exit_ts is not None else None
        if entry is None or exit_ is None:
            self.missing.append({"position": short.trade_id, "expiry": short.expiry, "strike": strike,
                                 "right": short.right, "entry_ts": str(short.entry_ts),
                                 "missing": "entry" if entry is None else "exit"})
            return None
        wing = Trade(self.name, short.trade_id + WING_SEP, short.day, short.expiry, strike, short.right, "BUY",
                     short.qty, short.entry_ts, entry, spot_entry=short.spot_entry, is_reentry=short.is_reentry,
                     note=f"hedge wing {self.hedge.wing_points} pts OTM of {short.strike:g}")
        return wing.close(short.exit_ts, exit_, f"wing closed with sold leg ({short.exit_reason})",
                          self.base.costs, short.spot_exit)


def combine_positions(trades: list[Trade]) -> list[Trade]:
    """One record per position: the sold leg with its wing's P&L and costs folded in.

    Lets `engine.metrics` count a spread as one trade, so naked and hedged runs
    have the same trade count.
    """
    groups: dict[str, list[Trade]] = {}
    for t in trades:
        groups.setdefault(position_id(t), []).append(t)
    out = []
    for legs in groups.values():
        main = next(t for t in legs if t.side == "SELL")
        wings = [t for t in legs if t is not main]
        out.append(replace(main,
                           gross_pnl=round(sum(t.gross_pnl for t in legs), 2),
                           costs=round(sum(t.costs for t in legs), 2),
                           net_pnl=round(sum(t.net_pnl for t in legs), 2),
                           note=main.note + (f" | wing {wings[0].strike:g}" if wings else "")))
    return out
