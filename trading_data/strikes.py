"""ATM and strike-range maths (pure functions, instrument-agnostic)."""
from __future__ import annotations

from datetime import date, timedelta


def atm_strike(price: float, step: int) -> int:
    """Nearest strike to `price` on a `step` grid: round(price / step) * step (legacy rule)."""
    if step <= 0:
        raise ValueError("strike step must be positive")
    return int(round(price / step) * step)


def dynamic_strikes(atm: int, expiry: date, spot_by_day: dict[date, float], *, step: int,
                    strikes_each_side: int, days_before_expiry: int, buffer: int,
                    round_to: int = 100) -> list[int]:
    """Legacy get_dynamic_strikes(), parameterised.

    Start with ATM +/- strikes_each_side * step, then widen so the range also
    covers the underlying's min/max over the `days_before_expiry` days before
    expiry (rounded to `round_to`) plus `buffer` points on each side.
    """
    lo = atm - strikes_each_side * step
    hi = atm + strikes_each_side * step
    start = expiry - timedelta(days=days_before_expiry)
    prices = [v for d, v in spot_by_day.items() if start <= d <= expiry and v]
    if prices:
        needed_lo = int(round(min(prices) / round_to) * round_to) - buffer
        needed_hi = int(round(max(prices) / round_to) * round_to) + buffer
        lo = min(lo, needed_lo)
        hi = max(hi, needed_hi)
    # keep the grid anchored on ATM even if the buffer is not a multiple of step
    lo = atm - ((atm - lo + step - 1) // step) * step
    hi = atm + ((hi - atm + step - 1) // step) * step
    return list(range(lo, hi + step, step))
