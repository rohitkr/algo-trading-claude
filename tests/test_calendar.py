from datetime import date, datetime, timedelta

import pytest

from trading_data.calendar import TradingCalendar, generate_expiries, previous_expiry


@pytest.fixture
def cal(settings):
    return TradingCalendar(settings.calendars["NSE"])


def legacy_get_weekly_expiries(start_str, end_str, holidays):
    """Verbatim port of the legacy function (with NSE_HOLIDAYS injected) for equivalence testing."""
    start = datetime.strptime(start_str, "%Y-%m-%d")
    end = datetime.strptime(end_str, "%Y-%m-%d")
    cutover = datetime(2025, 9, 1)
    expiries, current = [], start
    while current <= end:
        thu_days = (3 - current.weekday()) % 7
        thu_exp = current + timedelta(days=thu_days) if thu_days else current
        tue_days = (1 - current.weekday()) % 7
        tue_exp = current + timedelta(days=tue_days) if tue_days else current
        original = thu_exp if thu_exp < cutover else tue_exp
        if original > end:
            break
        exp = original
        es = exp.strftime("%Y-%m-%d")
        if es in holidays:
            adjusted = exp - timedelta(days=1)
            while adjusted.weekday() >= 5 or adjusted.strftime("%Y-%m-%d") in holidays:
                adjusted -= timedelta(days=1)
            exp = adjusted
        es = exp.strftime("%Y-%m-%d")
        if exp >= start and es not in expiries:
            expiries.append(es)
        current = original + timedelta(days=1)
    return sorted(set(expiries))


def test_weekday_and_weekend(cal):
    assert cal.is_trading_day(date(2025, 8, 7))          # Thursday
    assert not cal.is_trading_day(date(2025, 8, 9))      # Saturday
    assert not cal.is_trading_day("2025-08-10")          # Sunday, string input
    assert cal.is_trading_day(datetime(2025, 8, 8, 10))  # datetime input


def test_holidays(cal):
    assert not cal.is_trading_day(date(2025, 8, 15))
    assert not cal.is_trading_day(date(2026, 10, 2))
    assert cal.trading_days("2025-08-11", "2025-08-17") == [date(2025, 8, d) for d in (11, 12, 13, 14)]
    assert cal.previous_trading_day(date(2025, 8, 18)) == date(2025, 8, 14)


def test_session(cal):
    assert cal.expected_bars("1minute") == 375
    assert cal.expected_bars("5minute") == 75
    assert cal.in_session(datetime(2025, 8, 7, 15, 29)) and not cal.in_session(datetime(2025, 8, 7, 15, 30))


def test_legacy_defaults_expiries(settings, cal):
    p = settings.options_profile("NIFTY")
    assert generate_expiries(p.expiry_rules, p.expiry_start, p.expiry_end, cal) == [date(2025, 8, 7)]


@pytest.mark.parametrize("start,end", [("2023-01-01", "2026-12-31"), ("2025-08-20", "2025-09-20"),
                                       ("2025-08-29", "2025-09-10"), ("2024-03-01", "2024-04-30")])
def test_matches_legacy_generator(settings, cal, start, end):
    p = settings.options_profile("NIFTY")
    holidays = {d.isoformat() for d in cal.holidays}
    ours = [d.isoformat() for d in generate_expiries(p.expiry_rules, start, end, cal)]
    assert ours == legacy_get_weekly_expiries(start, end, holidays)


def test_thursday_to_tuesday_cutover_and_holiday_adjustment(settings, cal):
    p = settings.options_profile("NIFTY")
    exps = generate_expiries(p.expiry_rules, "2025-08-18", "2025-09-10", cal)
    assert exps == [date(2025, 8, 21), date(2025, 8, 28), date(2025, 9, 2), date(2025, 9, 9)]
    # 2024-03-28 Thu is fine, 2023-03-30 Thu is a holiday -> Wed 29th
    assert date(2023, 3, 29) in generate_expiries(p.expiry_rules, "2023-03-27", "2023-03-31", cal)
    for e in generate_expiries(p.expiry_rules, "2023-01-01", "2026-12-31", cal):
        assert cal.is_trading_day(e)


def test_monthly_rules(settings, cal):
    p = settings.options_profile("BANKNIFTY")
    exps = generate_expiries(p.expiry_rules, "2025-07-01", "2025-10-31", cal)
    assert exps == [date(2025, 7, 31), date(2025, 8, 28), date(2025, 9, 30), date(2025, 10, 28)]


def test_previous_expiry(settings, cal):
    p = settings.options_profile("NIFTY")
    assert previous_expiry(p.expiry_rules, date(2025, 8, 7), cal) == date(2025, 7, 31)
    assert previous_expiry(p.expiry_rules, date(2025, 9, 2), cal) == date(2025, 8, 28)
