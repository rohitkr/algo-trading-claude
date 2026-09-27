"""Verify the stored market data for the configured instrument(s). Reads DuckDB only; no API calls.

    python3 scripts/verify_market_data.py
    python3 scripts/verify_market_data.py --instruments BANKNIFTY SENSEX --start 2024-01-01 --end 2024-06-30
    python3 scripts/verify_market_data.py --show-days 20        # list every missing/incomplete day (default 15)

Uses the same MARKET_DATA_* configuration as the downloader. Exit code 0 = clean,
1 = missing/incomplete/duplicate data found.
"""
import argparse
import sys

import _path  # noqa: F401

from trading_data.app import bootstrap, parse_date
from trading_data.reports import market_quality


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instruments", nargs="+")
    ap.add_argument("--start", type=parse_date)
    ap.add_argument("--end", type=lambda v: "today" if v.lower() == "today" else parse_date(v))
    ap.add_argument("--interval", choices=["1minute", "5minute", "30minute", "1day"])
    ap.add_argument("--show-days", type=int, default=15, help="how many missing/incomplete days to list")
    args = ap.parse_args()

    settings, store = bootstrap("verify")
    md = settings.market_data
    ok = True
    for name in [n.upper() for n in (args.instruments or md.instruments)]:
        q = market_quality(settings, store, name, args.start, args.end, args.interval)
        clean = not (q.missing_days or q.incomplete_days or q.duplicate_candles or q.out_of_session_rows)
        ok &= clean

        def day(d):
            if not d:
                return "    (no data)"
            return (f"    date        {d['date']}\n    candles     {d['candles']}\n"
                    f"    first bar   {d['first_ts']}\n    last bar    {d['last_ts']}")

        def listing(items):
            items = list(items)
            shown = ", ".join(items[:args.show_days])
            return shown + (f" ... (+{len(items) - args.show_days} more)" if len(items) > args.show_days else "")

        print(f"""
Instrument          {q.instrument}
Exchange            {q.exchange}
Interval            {q.timeframe}
Checked range       {q.start} → {q.end}

Earliest timestamp  {q.earliest_ts or '-'}
Latest timestamp    {q.latest_ts or '-'}
Total candles       {q.total_rows:,}
Trading days        {q.trading_days_requested} in range, {q.trading_days_downloaded} complete

Duplicate candles   {q.duplicate_candles}
Missing days        {len(q.missing_days)}{'  ' + listing(q.missing_days) if q.missing_days else ''}
Incomplete days     {len(q.incomplete_days)}{'  ' + listing(q.incomplete_days) if q.incomplete_days else ''}
Out-of-session      {q.out_of_session_rows} days with bars outside the session
Weekend/holiday     {q.non_trading_day_rows} rows

First trading day:
{day(q.first_day)}
Latest trading day:
{day(q.latest_day)}

Result              {'OK' if clean else 'NEEDS ATTENTION (run scripts/download_market_data.py to repair)'}""")
    store.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
