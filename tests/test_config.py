from datetime import date, time
from pathlib import Path

import pytest

from trading_data.config import ConfigError, load_settings


def test_nifty_legacy_defaults(settings):
    p = settings.options_profile("NIFTY")
    assert (p.underlying, p.exchange, p.strike_step, p.strikes_each_side) == ("NIFTY", "NFO", 50, 20)
    assert (p.days_before_expiry, p.lot_size, p.max_threads, p.api_delay, p.daily_api_limit) == (45, 65, 3, 0.5, 4900)
    assert (p.expiry_start, p.expiry_end) == (date(2025, 8, 1), date(2025, 8, 10))
    assert p.dynamic_strike_buffer == 500 and p.retry_last_days == 10 and p.forward_fill is False
    md = settings.market_data
    assert (md.expected_bars_per_day, md.min_bars_threshold, md.chunk_days) == (375, 370, 7)
    cal = settings.calendars["NSE"]
    assert (cal.market_open, cal.market_close) == (time(9, 15), time(15, 29))


def test_instruments_are_config_driven(settings):
    assert settings.instrument("banknifty").stock_code == "CNXBAN"
    assert settings.instrument("SENSEX").exchange == "BSE"
    assert settings.options_profile("SENSEX").exchange == "BFO"
    with pytest.raises(ConfigError):
        settings.instrument("NOPE")


def test_env_overrides_expiry_and_credentials_redacted(settings):
    from tests.conftest import FAKE_ENV
    s = load_settings(env_path=None, environ={**FAKE_ENV, "EXPIRY_START": "2025-09-01", "EXPIRY_END": "2025-09-30"})
    assert s.options_profile("NIFTY").expiry_start == date(2025, 9, 1)
    assert "test-secret" not in repr(s.credentials) and "test-pass" not in repr(s)
    s.credentials.require_login()


def test_missing_credentials_are_reported():
    s = load_settings(env_path=None, environ={})
    with pytest.raises(ConfigError, match="BREEZE_API_KEY"):
        s.credentials.require_api()
    with pytest.raises(ConfigError, match="ICICI_USER_ID"):
        load_settings(env_path=None, environ={"BREEZE_API_KEY": "k", "BREEZE_API_SECRET": "s"}).credentials.require_login()


def test_env_file_is_loaded(tmp_path, monkeypatch):
    for k in ("BREEZE_API_KEY", "BREEZE_API_SECRET"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text("BREEZE_API_KEY=abc\nBREEZE_API_SECRET=def\n")
    s = load_settings(env_path=env)
    assert s.credentials.api_key == "abc"


def test_holidays_loaded_for_bse_from_nse(settings):
    assert date(2025, 8, 15) in settings.calendars["NSE"].holidays
    assert settings.calendars["BSE"].holidays == settings.calendars["NSE"].holidays
