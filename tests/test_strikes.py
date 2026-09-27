from datetime import date

import pytest

from trading_data.strikes import atm_strike, dynamic_strikes


def legacy_get_dynamic_strikes(atm, expiry, spot_cache, step=50, each=20, days=45, buf=500):
    from datetime import timedelta
    lo, hi = atm - each * step, atm + each * step
    start = expiry - timedelta(days=days)
    prices = [v for d, v in spot_cache.items() if start <= d <= expiry]
    if prices:
        lo = min(lo, int(round(min(prices) / 100) * 100) - buf)
        hi = max(hi, int(round(max(prices) / 100) * 100) + buf)
    return list(range(lo, hi + step, step))


def test_atm():
    assert atm_strike(24012.3, 50) == 24000
    assert atm_strike(24026, 50) == 24050
    assert atm_strike(55149, 100) == 55100
    assert atm_strike(81234, 100) == 81200
    with pytest.raises(ValueError):
        atm_strike(100, 0)


def test_base_range_without_spot():
    s = dynamic_strikes(24000, date(2025, 8, 7), {}, step=50, strikes_each_side=20, days_before_expiry=45, buffer=500)
    assert s[0] == 23000 and s[-1] == 25000 and len(s) == 41


def test_expands_with_spot_movement_like_legacy():
    spot = {date(2025, 7, 1): 25600.0, date(2025, 7, 20): 24900.0, date(2025, 8, 6): 24400.0,
            date(2025, 5, 1): 30000.0}   # outside the 45-day window, ignored
    ours = dynamic_strikes(24500, date(2025, 8, 7), spot, step=50, strikes_each_side=20,
                           days_before_expiry=45, buffer=500)
    assert ours == legacy_get_dynamic_strikes(24500, date(2025, 8, 7), spot)
    assert ours[0] == 23500 and ours[-1] == 26100


def test_other_step_sizes():
    s = dynamic_strikes(55000, date(2025, 8, 28), {date(2025, 8, 1): 56330.0}, step=100,
                        strikes_each_side=10, days_before_expiry=45, buffer=1000)
    assert s[0] == 54000 and s[-1] == 57300 and all(x % 100 == 0 for x in s)
