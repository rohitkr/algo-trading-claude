"""Configuration-driven market downloader: env config, moving END_DATE, gap repair, failed batches."""
from datetime import date, datetime, time

import pytest

from tests.conftest import FAKE_ENV
from tests.test_downloaders import FakeBreeze, make_client
from trading_data.breeze.client import BreezeError
from trading_data.calendar import TradingCalendar
from trading_data.config import CalendarSettings, ConfigError, load_settings
from trading_data.downloaders.market import MarketDownloader, resolve_end_date


def env(**kw):
    return load_settings(env_path=None, environ={**FAKE_ENV, **kw})


def test_defaults_cover_three_years_of_nifty_to_today(settings):
    md = settings.market_data
    assert md.instruments == ("NIFTY",) and md.timeframe == "1minute"
    assert md.start_date == date(2023, 9, 27) and md.end_date == "today"


def test_env_selects_instrument_interval_and_range():
    s = env(MARKET_DATA_INSTRUMENT="banknifty", MARKET_DATA_EXCHANGE="NSE", MARKET_DATA_INTERVAL="5minute",
            MARKET_DATA_START_DATE="2024-01-01", MARKET_DATA_END_DATE="2024-06-30")
    md = s.market_data
    assert md.instruments == ("BANKNIFTY",) and md.timeframe == "5minute"
    assert (md.start_date, md.end_date) == (date(2024, 1, 1), "2024-06-30")
    assert env(MARKET_DATA_INSTRUMENT="NIFTY, SENSEX").market_data.instruments == ("NIFTY", "SENSEX")


def test_exchange_must_match_instrument():
    with pytest.raises(ConfigError, match="SENSEX, which trades on BSE"):
        env(MARKET_DATA_INSTRUMENT="SENSEX", MARKET_DATA_EXCHANGE="NSE")
    assert env(MARKET_DATA_INSTRUMENT="SENSEX", MARKET_DATA_EXCHANGE="BSE").instrument("SENSEX").calendar == "BSE"


def test_bad_values_are_rejected():
    with pytest.raises(ConfigError):
        env(MARKET_DATA_END_DATE="next week")
    with pytest.raises(ConfigError):
        env(MARKET_DATA_INTERVAL="2minute")
    with pytest.raises(ConfigError, match="Unknown instrument"):
        env(MARKET_DATA_INSTRUMENT="FOO")


def test_unregistered_instrument_from_env():
    s = env(MARKET_DATA_INSTRUMENT="FOO", MARKET_DATA_STOCK_CODE="FOOIDX", MARKET_DATA_EXCHANGE="BSE")
    i = s.instrument("FOO")
    assert (i.stock_code, i.exchange, i.calendar, i.verified) == ("FOOIDX", "BSE", "BSE", False)


def test_resolve_end_date(settings):
    cal = TradingCalendar(settings.calendars["NSE"])
    after = time(15, 45)
    fri_evening = datetime(2026, 9, 25, 18, 0)
    fri_morning = datetime(2026, 9, 25, 11, 0)
    sunday = datetime(2026, 9, 27, 10, 0)
    assert resolve_end_date("today", cal, after, fri_evening) == date(2026, 9, 25)
    assert resolve_end_date("today", cal, after, fri_morning) == date(2026, 9, 24)   # session not finished
    assert resolve_end_date("today", cal, after, sunday) == date(2026, 9, 25)
    assert resolve_end_date("2025-06-30", cal, after, sunday) == date(2025, 6, 30)
    assert resolve_end_date(date(2030, 1, 1), cal, after, sunday) == date(2026, 9, 25)  # never past the last session


def _patch_today(monkeypatch, d):
    import trading_data.downloaders.market as m
    monkeypatch.setattr(m, "last_complete_session", lambda cal, after, now=None: d)


def test_moving_end_date_catches_up_new_days(settings, store, monkeypatch):
    fake = FakeBreeze()
    dl = MarketDownloader(settings, store, make_client(settings, store, fake))
    _patch_today(monkeypatch, date(2025, 8, 22))
    dl.download("NIFTY", date(2025, 8, 18), "today")
    assert store.market_coverage("NIFTY", "NSE", "1minute") == (date(2025, 8, 18), date(2025, 8, 22))
    # "a few days later": only the new days are requested
    _patch_today(monkeypatch, date(2025, 8, 28))
    s = dl.download("NIFTY", date(2025, 8, 18), "today")          # 27 Aug 2025 is a holiday
    assert s.requested_days == [date(2025, 8, 25), date(2025, 8, 26), date(2025, 8, 28)]
    assert s.rows_inserted == 3 * 375 and s.already_complete == 5
    assert store.market_coverage("NIFTY", "NSE", "1minute")[1] == date(2025, 8, 28)


def test_gap_in_the_middle_is_repaired(settings, store, monkeypatch):
    _patch_today(monkeypatch, date(2025, 8, 29))
    fake = FakeBreeze()
    dl = MarketDownloader(settings, store, make_client(settings, store, fake))
    dl.download("NIFTY", date(2025, 8, 18), "today")
    store.con.execute("DELETE FROM market_candles WHERE CAST(ts AS DATE) = '2025-08-21'")
    store.con.execute("DELETE FROM market_candles WHERE ts >= '2025-08-26 12:00'")   # partial day
    todo, *_ = dl.missing_days(settings.instrument("NIFTY"), date(2025, 8, 18), date(2025, 8, 29), "1minute")
    assert todo == [date(2025, 8, 21), date(2025, 8, 26), date(2025, 8, 28), date(2025, 8, 29)]
    s = dl.download("NIFTY", date(2025, 8, 18), "today")
    assert s.requested_days == todo and not s.incomplete and not s.empty
    counts = store.market_bar_counts("NIFTY", "NSE", "1minute", date(2025, 8, 18), date(2025, 8, 29))
    assert set(counts.values()) == {375} and len(counts) == 9     # 27th is a holiday


def test_failed_batch_is_recorded_and_others_continue(settings, store, monkeypatch):
    _patch_today(monkeypatch, date(2025, 8, 29))

    class Flaky(FakeBreeze):
        def get_historical_data_v2(self, **kw):
            if kw["from_date"].startswith("2025-08-18"):
                return {"Status": 400, "Success": None, "Error": "Bad request"}
            return super().get_historical_data_v2(**kw)

    fake = Flaky()
    dl = MarketDownloader(settings, store, make_client(settings, store, fake))
    s = dl.download("NIFTY", date(2025, 8, 11), "today")
    assert len(s.failed_batches) == 1 and s.failed_batches[0][0] == date(2025, 8, 18)
    assert date(2025, 8, 25) in s.complete and date(2025, 8, 11) in s.complete
    assert store.failed_days("NIFTY", "NSE", "1minute")[0] == date(2025, 8, 18)
    # failed days are always retried on the next run
    todo, *_ = dl.missing_days(settings.instrument("NIFTY"), date(2025, 8, 11), date(2025, 8, 29), "1minute")
    assert todo[0] == date(2025, 8, 18)


def test_idempotent_rerun_makes_no_calls(settings, store, monkeypatch):
    _patch_today(monkeypatch, date(2025, 8, 22))
    fake = FakeBreeze()
    dl = MarketDownloader(settings, store, make_client(settings, store, fake))
    dl.download("NIFTY", date(2025, 8, 11), "today")
    n = len(fake.calls)
    s = dl.download("NIFTY", date(2025, 8, 11), "today")
    assert len(fake.calls) == n and s.rows_inserted == 0 and s.requested_days == []


def test_instruments_use_their_own_exchange_and_code(settings, store, monkeypatch):
    _patch_today(monkeypatch, date(2025, 8, 22))
    fake = FakeBreeze()
    dl = MarketDownloader(settings, store, make_client(settings, store, fake))
    dl.download("SENSEX", date(2025, 8, 18), "today")
    assert {c[0] for c in fake.calls} == {"BSESEN"}
    assert store.market_coverage("SENSEX", "BSE", "1minute")[0] == date(2025, 8, 18)
    assert store.market_coverage("SENSEX", "NSE", "1minute") == (None, None)


def test_special_session_counts_as_trading_day():
    cal = TradingCalendar(CalendarSettings("X", time(9, 15), time(15, 29), frozenset({date(2024, 11, 1)}),
                                           {date(2024, 11, 1): (time(18, 0), time(18, 59))}))
    assert cal.is_trading_day(date(2024, 11, 1)) and cal.expected_bars("1minute", date(2024, 11, 1)) == 60
    assert cal.in_session(datetime(2024, 11, 1, 18, 30)) and not cal.in_session(datetime(2024, 11, 1, 10, 0))
    assert cal.expected_bars("1minute", date(2024, 11, 4)) == 375
