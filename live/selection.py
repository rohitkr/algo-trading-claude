"""Contract / option selection for live trading, using the backtest's own rules and calendar.

Expiries come from backtest.data.DataFeed (config/settings.toml expiry rules +
holiday calendar); strikes from backtest.rules; hedge wings from
backtest.hedged.wing_strike. Lot size and strike step can be overridden in the
engine config (LOT_SIZE / STRIKE_STEP) but default to settings.toml.
"""
from __future__ import annotations

from datetime import date

from backtest import rules
from backtest.data import DataFeed
from backtest.hedged import wing_strike
from trading_data.storage import OptionContract


class ContractSelector:
    def __init__(self, feed: DataFeed, lot_size: int | None = None, strike_step: int | None = None):
        self.feed = feed
        self.profile = feed.profile
        self.cal = feed.cal
        self.lot_size = int(lot_size or self.profile.lot_size)
        self.strike_step = int(strike_step or self.profile.strike_step)

    def contract(self, expiry: date, strike: float, right: str) -> OptionContract:
        return OptionContract(self.profile.underlying, self.profile.exchange, expiry, strike, right)

    def is_trading_day(self, d: date) -> bool:
        return self.cal.is_trading_day(d)

    def is_expiry(self, d: date) -> bool:
        return d in self.feed.expiries(d, d)

    # positional: next weekly expiry strictly after the entry day, ITM by itm_points
    def positional(self, spot: float, direction: str, day: date, itm_points: int,
                   expiry_offset: int = 0) -> OptionContract:
        right = rules.breakout_right(direction)
        expiry = self.feed.next_expiry(day, strictly_after=True)
        for _ in range(expiry_offset):              # same rule as backtest RangeBreakoutParams.expiry_offset
            expiry = self.feed.next_expiry(expiry, strictly_after=True)
        return self.contract(expiry, rules.itm_strike(spot, right, itm_points, self.strike_step), right)

    # 0DTE: today's expiry, CALL at ATM - itm, PUT at ATM + itm
    def zerodte(self, spot: float, day: date, itm_points: int) -> dict[str, OptionContract]:
        strikes = rules.straddle_strikes(spot, itm_points, self.strike_step)
        return {right: self.contract(day, k, right) for right, k in strikes.items()}

    def wing(self, short: OptionContract, width: int) -> OptionContract:
        return self.contract(short.expiry, wing_strike(short.strike, short.right, width), short.right)
