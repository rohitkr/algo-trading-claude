import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trading_data.config import load_settings  # noqa: E402
from trading_data.storage import CandleStore  # noqa: E402

FAKE_ENV = {
    "ICICI_USER_ID": "test-user", "ICICI_PASSWORD": "test-pass",
    "BREEZE_API_KEY": "test-key", "BREEZE_API_SECRET": "test-secret",
}


@pytest.fixture
def settings():
    return load_settings(env_path=None, environ=dict(FAKE_ENV))


@pytest.fixture
def store(tmp_path):
    s = CandleStore(tmp_path / "test.duckdb")
    yield s
    s.close()
