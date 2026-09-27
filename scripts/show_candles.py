"""Export OHLCV candles from DuckDB to a CSV file (read-only, no API calls).

Examples:

    python3 scripts/show_candles.py \
        --start 2025-08-01 \
        --end 2025-08-01 \
        --csv output/nifty_aug_01.csv

    python3 scripts/show_candles.py \
        --instrument BANKNIFTY \
        --start "2025-08-01 09:15" \
        --end "2025-08-01 10:00" \
        --csv output/banknifty_sample.csv

    python3 scripts/show_candles.py \
        --start 2025-08-01 \
        --end 2025-08-31 \
        --resample 1D \
        --csv output/nifty_aug_daily.csv

    python3 scripts/show_candles.py \
        --start 2024-01-01 \
        --end 2024-12-31 \
        --resample 1h \
        --tail 20 \
        --csv output/nifty_2024_last20.csv

Dates are inclusive (a date alone means the whole day).
Times are IST.
Defaults: instrument/interval from MARKET_DATA_INSTRUMENT / MARKET_DATA_INTERVAL.
"""

import argparse
import sys
from pathlib import Path
from datetime import datetime

import _path  # noqa: F401
import pandas as pd

from trading_data.config import load_settings
from trading_data.storage import CandleStore


def parse_when(value: str, end: bool) -> datetime:
    value = value.strip()

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass

    try:
        d = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected YYYY-MM-DD or 'YYYY-MM-DD HH:MM', got {value!r}"
        ) from None

    return d.replace(hour=23, minute=59, second=59) if end else d


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    ap.add_argument(
        "--instrument",
        help="e.g. NIFTY, BANKNIFTY, SENSEX (default: MARKET_DATA_INSTRUMENT)",
    )

    ap.add_argument(
        "--start",
        required=True,
        help="YYYY-MM-DD or 'YYYY-MM-DD HH:MM'",
    )

    ap.add_argument(
        "--end",
        help="YYYY-MM-DD or 'YYYY-MM-DD HH:MM' (default: same day as --start)",
    )

    ap.add_argument(
        "--interval",
        help="stored timeframe to read (default: MARKET_DATA_INTERVAL, usually 1minute)",
    )

    ap.add_argument(
        "--resample",
        help="aggregate to a bigger bar, e.g. 5min, 15min, 1h, 1D (pandas offset)",
    )

    ap.add_argument(
        "--head",
        type=int,
        help="export only the first N rows",
    )

    ap.add_argument(
        "--tail",
        type=int,
        help="export only the last N rows",
    )

    ap.add_argument(
        "--csv",
        required=True,
        help="output CSV file path",
    )

    args = ap.parse_args()

    if args.head is not None and args.tail is not None:
        ap.error("--head and --tail cannot be used together")

    if args.head is not None and args.head <= 0:
        ap.error("--head must be greater than 0")

    if args.tail is not None and args.tail <= 0:
        ap.error("--tail must be greater than 0")

    settings = load_settings()

    instrument = (
        args.instrument or settings.market_data.instruments[0]
    ).upper()

    interval = args.interval or settings.market_data.timeframe

    start = parse_when(args.start, end=False)
    end = parse_when(args.end or args.start, end=True)

    if end < start:
        ap.error("--end is before --start")

    if not settings.paths.database.exists():
        print(
            f"No database at {settings.paths.database}. "
            "Run scripts/download_market_data.py first."
        )
        return 1

    store = CandleStore(
        settings.paths.database,
        read_only=True,
    )

    try:
        df = store.get_market_candles(
            instrument,
            start,
            end,
            timeframe=interval,
        )
    finally:
        store.close()

    if df.empty:
        print(
            f"No {interval} candles for {instrument} "
            f"between {start} and {end}."
        )
        return 1

    df = df.set_index("ts")[[
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]]

    # Optional resampling
    if args.resample:
        df = (
            df.resample(
                args.resample,
                label="left",
                closed="left",
            )
            .agg(
                {
                    "open": "first",
                    "high": "max",
                    "low": "min",
                    "close": "last",
                    "volume": "sum",
                }
            )
            .dropna(subset=["open"])
        )

    # Apply head/tail filtering before exporting.
    if args.head:
        output_df = df.head(args.head)
    elif args.tail:
        output_df = df.tail(args.tail)
    else:
        output_df = df

    # Create parent directory if required.
    output_path = Path(args.csv)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Export CSV.
    output_df.to_csv(args.csv, index=True)

    print(
        f"Exported {len(output_df):,} rows to {output_path}"
    )

    print(
        f"Instrument : {instrument}\n"
        f"Interval   : {interval}\n"
        f"Range      : {output_df.index.min()} -> {output_df.index.max()}\n"
        f"Rows       : {len(output_df):,}"
        f"{f' (resampled: {args.resample})' if args.resample else ''}"
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())