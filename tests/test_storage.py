from datetime import date, datetime

import pandas as pd
import pytest

from trading_data.storage import CandleStore, OptionContract


def market_frame(day=date(2025, 8, 7), n=3, close=100.0, instrument="NIFTY"):
    ts = pd.date_range(datetime.combine(day, datetime.min.time()).replace(hour=9, minute=15), periods=n, freq="1min")
    return pd.DataFrame({"instrument": instrument, "exchange": "NSE", "timeframe": "1minute", "ts": ts,
                         "open": 100.0, "high": 101.0, "low": 99.0, "close": close,
                         "volume": pd.array([0] * n, dtype="Int64"), "open_interest": pd.array([None] * n, dtype="Int64")})


def test_initialization_is_idempotent(tmp_path):
    p = tmp_path / "x.duckdb"
    CandleStore(p).close()
    s = CandleStore(p)
    tables = set(s.query_df("SELECT table_name FROM information_schema.tables")["table_name"])
    assert {"market_candles", "option_candles", "option_contracts", "api_usage", "market_day_status"} <= tables
    s.close()


def test_insert_and_duplicate_prevention(store):
    store.upsert_market_candles(market_frame())
    store.upsert_market_candles(market_frame(close=100.5))   # same keys again, corrected close
    df = store.get_market_candles("NIFTY", date(2025, 8, 7), date(2025, 8, 7))
    assert len(df) == 3 and (df["close"] == 100.5).all()
    assert store.market_bar_counts("NIFTY", "NSE", "1minute", date(2025, 8, 7), date(2025, 8, 7)) == {date(2025, 8, 7): 3}


def test_partial_reload_never_deletes(store):
    store.upsert_market_candles(market_frame(n=5))
    store.upsert_market_candles(market_frame(n=2))          # a later, shorter response
    assert len(store.get_market_candles("NIFTY", "2025-08-07", "2025-08-07")) == 5


def test_instruments_are_isolated(store):
    store.upsert_market_candles(market_frame(instrument="NIFTY"))
    store.upsert_market_candles(market_frame(instrument="BANKNIFTY", n=2))
    assert len(store.get_market_candles("BANKNIFTY", "2025-08-07", "2025-08-07")) == 2


def test_option_contract_identity():
    a = OptionContract("nifty", "nfo", date(2025, 8, 7), 24000, "CE")
    b = OptionContract("NIFTY", "NFO", date(2025, 8, 7), 24000.0, "CALL")
    assert a == b and hash(a) == hash(b) and a.label == "NIFTY 2025-08-07 24000 CALL"
    assert a != OptionContract("NIFTY", "NFO", date(2025, 8, 7), 24000, "PUT")
    with pytest.raises(ValueError):
        OptionContract("NIFTY", "NFO", date(2025, 8, 7), 24000, "XX")


def test_option_upsert_and_query(store):
    c = OptionContract("NIFTY", "NFO", date(2025, 8, 7), 24000, "CALL")
    ts = pd.date_range("2025-08-06 09:15", periods=4, freq="1min")
    df = pd.DataFrame({"underlying": "NIFTY", "exchange": "NFO", "expiry": date(2025, 8, 7), "strike": 24000.0,
                       "option_right": "CALL", "timeframe": "1minute", "ts": ts, "open": 10.0, "high": 11.0,
                       "low": 9.0, "close": 10.5, "volume": pd.array([5] * 4, dtype="Int64"),
                       "open_interest": pd.array([100] * 4, dtype="Int64")})
    store.upsert_option_candles(df)
    store.upsert_option_candles(df)
    assert store.option_row_count(c) == 4
    out = store.get_option_candles("NIFTY", "2025-08-07", 24000, "CE")
    assert len(out) == 4 and list(out["open_interest"]) == [100] * 4
    store.record_contract_status(c, "1minute", "complete", 4)
    store.record_contract_status(c, "1minute", "complete", 4)
    assert store.contract_statuses("NIFTY", "NFO", date(2025, 8, 7)) == {(24000.0, "CALL"): ("complete", 4)}
    assert store.query_df("SELECT attempts FROM option_contracts")["attempts"].iloc[0] == 2


def test_api_usage_counter(store):
    d = date(2026, 9, 27)
    assert store.api_calls_on(d) == 0
    store.add_api_calls(d)
    assert store.add_api_calls(d, 2) == 3
