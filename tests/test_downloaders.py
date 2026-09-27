"""Downloader behaviour against a fake Breeze SDK (no network)."""
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from trading_data.breeze.client import ApiLimitReached, BreezeClient
from trading_data.downloaders.market import MarketDownloader, group_runs
from trading_data.downloaders.options import OptionsDownloader
from trading_data.calendar import TradingCalendar


class FakeBreeze:
    """Mimics get_historical_data_v2: returns the LAST `cap` rows of the window."""

    def __init__(self, cap=1000, bars=375, missing_days=(), empty_contracts=()):
        self.cap, self.bars, self.missing_days, self.empty_contracts = cap, bars, set(missing_days), set(empty_contracts)
        self.calls = []

    def get_historical_data_v2(self, interval, from_date, to_date, stock_code, exchange_code, product_type,
                               expiry_date="", right="", strike_price=""):
        self.calls.append((stock_code, from_date, to_date, right, strike_price))
        start = datetime.strptime(from_date[:19], "%Y-%m-%dT%H:%M:%S")
        end = datetime.strptime(to_date[:19], "%Y-%m-%dT%H:%M:%S")
        if (strike_price, right) in self.empty_contracts:
            return {"Status": 200, "Success": [], "Error": None}
        rows, d = [], start.date()
        while d <= end.date():
            if d.weekday() < 5 and d not in self.missing_days:
                if interval == "1day":
                    ts = [datetime.combine(d, datetime.min.time())]
                else:
                    first = datetime.combine(d, datetime.min.time()).replace(hour=9, minute=15)
                    ts = [first + timedelta(minutes=i) for i in range(self.bars)] + [first.replace(hour=15, minute=35)]
                base = 24000 + (d.toordinal() % 7) * 10
                for t in ts:
                    if start <= t <= end:
                        rows.append({"datetime": t.strftime("%Y-%m-%d %H:%M:%S"), "open": base, "high": base + 5,
                                     "low": base - 5, "close": base + 1, "volume": 1, "open_interest": 7})
            d += timedelta(days=1)
        return {"Status": 200, "Success": rows[-self.cap:], "Error": None}


def make_client(settings, store, fake, limit=None):
    return BreezeClient(settings, store, daily_limit=limit, delay_seconds=0, breeze=fake)


def test_pagination_covers_whole_window(settings, store):
    fake = FakeBreeze(cap=1000)
    client = make_client(settings, store, fake)
    from trading_data.breeze.client import HistoricalRequest
    rows = client.fetch_candles(HistoricalRequest("NIFTY", "NSE", "cash"),
                                datetime(2025, 8, 4, 9, 15), datetime(2025, 8, 8, 15, 29))
    stamps = {r["datetime"] for r in rows}
    assert len(stamps) == 5 * 376 - 1     # 375 bars + 1 post-close bar per day; the last one is past the window end
    assert len(fake.calls) == 2


def test_market_download_resume_and_missing_days(settings, store, monkeypatch):
    import trading_data.downloaders.market as m
    monkeypatch.setattr(m, "last_complete_session", lambda cal, after, now=None: date(2025, 8, 22))
    fake = FakeBreeze(missing_days={date(2025, 8, 20)})
    dl = MarketDownloader(settings, store, make_client(settings, store, fake))
    s = dl.download("NIFTY", date(2025, 8, 11), date(2025, 8, 22))
    assert len(s.complete) == 8 and s.empty == [date(2025, 8, 20)]  # 15th is a holiday
    assert store.query_df("SELECT count(*) n FROM market_candles")["n"].iloc[0] == 8 * 375

    # simulate the missed-days scenario: data for 11..14 only, then catch up
    store.con.execute("DELETE FROM market_candles WHERE ts >= '2025-08-18'")
    todo, *_ = dl.missing_days(settings.instrument("NIFTY"), date(2025, 8, 11), date(2025, 8, 22), "1minute")
    assert todo == [date(2025, 8, d) for d in (18, 19, 20, 21, 22)]
    fake.missing_days.clear()
    calls = len(fake.calls)
    s2 = dl.download("NIFTY", date(2025, 8, 11), date(2025, 8, 22))
    assert s2.requested_days == todo and len(s2.complete) == 5
    assert len(fake.calls) > calls
    # nothing left: a re-run makes no API calls and no duplicates
    calls = len(fake.calls)
    dl.download("NIFTY", date(2025, 8, 11), date(2025, 8, 22))
    assert len(fake.calls) == calls
    assert store.query_df("SELECT count(*) n FROM market_candles")["n"].iloc[0] == 9 * 375


def test_max_attempts_stops_rerequesting_empty_days(settings, store, monkeypatch):
    import trading_data.downloaders.market as m
    monkeypatch.setattr(m, "last_complete_session", lambda cal, after, now=None: date(2025, 8, 22))
    fake = FakeBreeze(missing_days={date(2025, 8, 21)})
    dl = MarketDownloader(settings, store, make_client(settings, store, fake))
    for _ in range(settings.market_data.max_attempts_per_day):
        dl.download("NIFTY", date(2025, 8, 21), date(2025, 8, 21))
    todo, skipped, *_ = dl.missing_days(settings.instrument("NIFTY"), date(2025, 8, 21), date(2025, 8, 21), "1minute")
    assert todo == [] and skipped == [date(2025, 8, 21)]


def test_api_limit_stops_safely(settings, store, monkeypatch):
    import trading_data.downloaders.market as m
    monkeypatch.setattr(m, "last_complete_session", lambda cal, after, now=None: date(2025, 8, 29))
    fake = FakeBreeze()
    dl = MarketDownloader(settings, store, make_client(settings, store, fake, limit=3))
    s = dl.download("NIFTY", date(2025, 8, 1), date(2025, 8, 29))
    assert s.stopped_reason and "limit" in s.stopped_reason
    assert len(fake.calls) == 3
    kept = store.query_df("SELECT count(DISTINCT CAST(ts AS DATE)) n FROM market_candles")["n"].iloc[0]
    assert kept > 0


def test_group_runs(settings):
    cal = TradingCalendar(settings.calendars["NSE"])
    days = cal.trading_days("2025-08-01", "2025-08-29")
    runs = group_runs([d for d in days if d != date(2025, 8, 20)], cal, 7)
    assert all((r[-1] - r[0]).days < 7 for r in runs)
    assert date(2025, 8, 19) in runs[2] or any(r[-1] == date(2025, 8, 19) for r in runs)


def test_options_end_to_end(settings, store, monkeypatch):
    import dataclasses
    import trading_data.downloaders.market as m
    monkeypatch.setattr(m, "last_complete_session", lambda cal, after, now=None: date(2025, 8, 8))
    profile = dataclasses.replace(settings.options_profile("NIFTY"), strikes_each_side=2, dynamic_strike_buffer=0,
                                  days_before_expiry=3, max_threads=2)
    fake = FakeBreeze(empty_contracts={("24100", "put")})
    dl = OptionsDownloader(settings, store, make_client(settings, store, fake), profile)
    s = dl.run()
    assert s.expiries == [date(2025, 8, 7)]
    plan = store.get_expiry_plan("NIFTY", "NFO", date(2025, 8, 7))
    assert plan["atm_date"].date() == date(2025, 7, 31)          # previous expiry = entry day
    assert int(plan["atm"]) % 50 == 0
    assert s.no_data == 1 and s.retried == 1 and s.downloaded == s.planned_contracts - 1

    from trading_data.reports import options_quality
    q = options_quality(settings, store, "NIFTY")
    assert q.missing_ce_pe_pairs == [{"expiry": "2025-08-07", "strike": 24100, "missing": "PUT"}]

    calls = len(fake.calls)
    s2 = dl.run()                      # resume: nothing left to fetch
    assert s2.downloaded == 0 and s2.skipped_existing == s.planned_contracts
    assert len(fake.calls) == calls


def test_options_api_limit_then_resume(settings, store, monkeypatch):
    import dataclasses
    import trading_data.downloaders.market as m
    monkeypatch.setattr(m, "last_complete_session", lambda cal, after, now=None: date(2025, 8, 8))
    profile = dataclasses.replace(settings.options_profile("NIFTY"), strikes_each_side=3, dynamic_strike_buffer=0,
                                  days_before_expiry=3, max_threads=3)
    fake = FakeBreeze()
    s = OptionsDownloader(settings, store, make_client(settings, store, fake, limit=6), profile).run()
    assert s.stopped_reason and "limit" in s.stopped_reason
    done_first = s.downloaded
    assert 0 < done_first < s.planned_contracts
    s2 = OptionsDownloader(settings, store, make_client(settings, store, fake, limit=10_000), profile).run()
    assert s2.skipped_existing == done_first and s2.downloaded == s.planned_contracts - done_first
