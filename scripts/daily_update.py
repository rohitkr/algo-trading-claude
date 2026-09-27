"""End-of-day update. Same as download_market_data.py (kept so existing habits keep working).

    python3 scripts/daily_update.py

With MARKET_DATA_END_DATE=today it finds every missing trading day in DuckDB
(new days and gaps) and downloads them; skipping a few days is fine.
"""
import sys

import _path  # noqa: F401
from download_market_data import main

if __name__ == "__main__":
    sys.exit(main())
