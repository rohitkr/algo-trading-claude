from datetime import date, datetime

import pandas as pd

from trading_data.calendar import TradingCalendar
from trading_data.validation import classify_day, clean_candles, forward_fill_day, records_to_frame


def rec(ts, o=100, h=101, l=99, c=100.5, v=10):
    return {"datetime": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


def test_cleaning_rules(settings):
    cal = TradingCalendar(settings.calendars["NSE"])
    records = [
        rec("2025-08-07 09:15:00"), rec("2025-08-07 09:16:00"),
        rec("2025-08-07 09:16:00", c=100.7),        # duplicate timestamp (last wins)
        rec("2025-08-07 09:14:00"),                 # pre-open
        rec("2025-08-07 15:35:00"),                 # post-close (Breeze returns these for indices)
        rec("2025-08-09 10:00:00"),                 # Saturday
        rec("2025-08-15 10:00:00"),                 # holiday
        rec("not-a-date"), rec(None),               # malformed / missing
        rec("2025-08-07 09:17:00", h=90),           # high < low
        rec("2025-08-07 09:18:00", c="abc"),        # non numeric
    ]
    df, rep = clean_candles(records_to_frame(records), cal, "1minute")
    assert list(df["ts"].dt.strftime("%H:%M")) == ["09:15", "09:16"]
    assert df["close"].iloc[1] == 100.7
    assert (rep.duplicate_timestamps, rep.outside_session_rows, rep.weekend_rows, rep.holiday_rows) == (1, 2, 1, 1)
    assert (rep.malformed_datetime, rep.missing_datetime, rep.invalid_numeric) == (1, 1, 2)
    assert rep.bars_per_day == {date(2025, 8, 7): 2}


def test_day_classification():
    assert classify_day(375, 375, 370) == "complete"
    assert classify_day(370, 375, 370) == "complete"
    assert classify_day(369, 375, 370) == "incomplete"
    assert classify_day(0, 375, 370) == "empty"


def test_forward_fill_only_returns_generated_rows(settings):
    cal = TradingCalendar(settings.calendars["NSE"])
    real = pd.DataFrame({"ts": pd.to_datetime(["2025-08-07 09:15", "2025-08-07 09:18"]),
                         "open": [10.0, 12.0], "high": [11.0, 12.5], "low": [9.5, 11.5], "close": [10.5, 12.2],
                         "volume": [5, 7], "open_interest": [100, 110]})
    gen = forward_fill_day(real, cal, date(2025, 8, 7))
    assert len(gen) + len(real) == 375
    assert not set(gen["ts"]) & set(real["ts"])
    first = gen.iloc[0]
    assert first["ts"] == datetime(2025, 8, 7, 9, 16) and first["close"] == 10.5 and first["volume"] == 0
