"""The strategies' trading rules as pure functions, shared by the backtest and live trading.

backtest/strategies.py simulates with these functions and live/strategies.py
trades with them, so the two cannot drift apart. Nothing here touches data
sources, brokers or time: every input is passed in.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pandas as pd

from trading_data.strikes import atm_strike

UP, DOWN = "UP", "DOWN"
REENTRY_CUTOFF = timedelta(minutes=15)      # positional re-entry must happen this long before the final exit


# -- positional range breakout -----------------------------------------------------------
def range_bars(spot_day: pd.DataFrame, d: date, range_start: time, range_end: time) -> pd.DataFrame:
    """Bars that form the opening range: range_start up to (not including) range_end."""
    return spot_day.between_time(range_start, (datetime.combine(d, range_end) - timedelta(minutes=1)).time())


def range_levels(bars: pd.DataFrame) -> tuple[float, float]:
    """(high, low) of the opening-range bars."""
    return float(bars["high"].max()), float(bars["low"].min())


def breakout(close: float, hi: float, lo: float) -> str | None:
    """UP on a close above the range high, DOWN below the low. UP is checked first."""
    if close > hi:
        return UP
    if close < lo:
        return DOWN
    return None


def breakout_right(direction: str) -> str:
    """Upside breakout sells a PUT, downside sells a CALL."""
    return "PUT" if direction == UP else "CALL"


def itm_strike(spot: float, right: str, itm_points: int, step: int) -> int:
    """Strike `itm_points` in the money from the ATM of `spot`: PUT above ATM, CALL below."""
    atm = atm_strike(spot, step)
    return atm + itm_points if right == "PUT" else atm - itm_points


def spot_stop_level(ref: float, right: str, sl_pct: float) -> float:
    move = sl_pct / 100 * ref
    return ref - move if right == "PUT" else ref + move


def spot_stop_hit(spot_close: float, ref: float, right: str, sl_pct: float) -> bool:
    """The underlying closed sl_pct against a short `right` entered at spot `ref`."""
    move = sl_pct / 100 * ref
    return spot_close <= ref - move if right == "PUT" else spot_close >= ref + move


def spot_reentry_ok(spot_close: float, entry_spot: float, right: str) -> bool:
    """Re-entry "at cost": the underlying is back at (or through) the original entry level."""
    return spot_close >= entry_spot if right == "PUT" else spot_close <= entry_spot


def reentry_window_open(ts: datetime, final_ts: datetime) -> bool:
    return ts < final_ts - REENTRY_CUTOFF


# -- 0DTE ITM straddle -------------------------------------------------------------------
def straddle_strikes(spot: float, itm_points: int, step: int) -> dict[str, int]:
    """CALL at ATM - itm_points, PUT at ATM + itm_points (ATM straddle when itm_points = 0)."""
    atm = atm_strike(spot, step)
    return {"CALL": atm - itm_points, "PUT": atm + itm_points}


def premium_stop_level(entry_price: float, sl_pct: float) -> float:
    return entry_price * (1 + sl_pct / 100)


def premium_stop_hit(bar_high: float, stop: float) -> bool:
    return bar_high >= stop


def premium_stop_fill(stop: float, bar_open: float) -> float:
    """A gap through the stop fills at the bar's open."""
    return max(stop, float(bar_open))


def premium_reentry_ok(option_close: float, first_entry: float) -> bool:
    return option_close <= first_entry


def candidate_times(first_entry: time, last_entry: time, step_minutes: int) -> list[time]:
    out, t = [], datetime.combine(date.min, first_entry)
    while t.time() <= last_entry:
        out.append(t.time())
        t += timedelta(minutes=step_minutes)
    return out


def best_entry_time(times: list[time], scores: dict[time, float]) -> time:
    """Walk-forward choice: highest past P&L, earliest time on a tie."""
    return max(times, key=lambda t: (scores[t], -times.index(t)))
