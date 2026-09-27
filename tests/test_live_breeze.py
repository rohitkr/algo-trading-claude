"""Live Breeze API checks. Skipped unless BREEZE_LIVE=1 (needs .env + today's session token).

    BREEZE_LIVE=1 python3 -m pytest tests/test_live_breeze.py -v

Uses a throwaway DuckDB file and makes about 5 API calls.
"""
import os
from datetime import date, datetime

import pytest

from trading_data.breeze.client import BreezeClient, HistoricalRequest
from trading_data.config import load_settings
from trading_data.storage import CandleStore

pytestmark = pytest.mark.skipif(os.environ.get("BREEZE_LIVE") != "1", reason="live API test; set BREEZE_LIVE=1")


@pytest.fixture
def live(tmp_path):
    settings = load_settings()
    store = CandleStore(tmp_path / "live.duckdb")
    yield BreezeClient(settings, store).connect()
    store.close()


def test_index_minute_data_is_paged(live):
    rows = live.fetch_candles(HistoricalRequest("NIFTY", "NSE", "cash"),
                              datetime(2025, 8, 4, 9, 15), datetime(2025, 8, 8, 15, 29))
    days = {r["datetime"][:10] for r in rows}
    assert len(days) == 5 and len(rows) > 1000


def test_option_contract(live):
    rows = live.fetch_candles(HistoricalRequest("NIFTY", "NFO", "options", expiry=date(2025, 8, 7), right="call",
                                                strike=24500), datetime(2025, 8, 6, 9, 15), datetime(2025, 8, 7, 15, 29))
    assert rows and {"open_interest", "strike_price", "right"} <= set(rows[0])
