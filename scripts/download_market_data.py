"""Market-data downloader: historical backfill AND incremental catch-up in one command.

    python3 scripts/download_market_data.py

What to download comes from configuration, not from this file:
    .env                 MARKET_DATA_INSTRUMENT, MARKET_DATA_EXCHANGE, MARKET_DATA_INTERVAL,
                         MARKET_DATA_START_DATE, MARKET_DATA_END_DATE (a date or "today")
    config/settings.toml [market_data] defaults and the [instruments.*] registry

Every run compares each trading day in START..END with what DuckDB actually
holds and downloads only the missing or incomplete days (new days at the end
AND gaps in the middle), in API-safe batches. Running it again later with
END_DATE=today brings the dataset up to the latest finished session.

Optional one-off overrides (no need to edit .env):
    --instruments BANKNIFTY SENSEX   --start 2024-01-01   --end 2024-06-30 | today
    --interval 5minute   --dry-run (plan only, no API calls)   --force (retry days that failed 3 times)
"""
import argparse
import sys

import _path  # noqa: F401

from trading_data.app import EXIT_API_LIMIT, EXIT_ERROR, EXIT_OK, bootstrap, connect_client, parse_date
from trading_data.downloaders.market import MarketDownloader
from trading_data.log import get_logger
from trading_data.reports import build_report, render_text, write_report


def date_ranges(days, cal):
    """Trading days -> 'a → b, c' where each range is a run of consecutive trading days."""
    if not days:
        return "none"
    out, start, prev = [], days[0], days[0]
    for d in days[1:]:
        if cal.next_trading_day(prev) != d:
            out.append((start, prev))
            start = d
        prev = d
    out.append((start, prev))
    text = ", ".join(f"{a} → {b}" if a != b else f"{a}" for a, b in out[:8])
    return text + (f", ... (+{len(out) - 8} more ranges)" if len(out) > 8 else "")


def end_arg(value: str):
    return "today" if value.lower() == "today" else parse_date(value)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instruments", nargs="+", help="override MARKET_DATA_INSTRUMENT")
    ap.add_argument("--start", type=parse_date, help="override MARKET_DATA_START_DATE")
    ap.add_argument("--end", type=end_arg, help="override MARKET_DATA_END_DATE (YYYY-MM-DD or today)")
    ap.add_argument("--interval", "--timeframe", dest="interval", choices=["1minute", "5minute", "30minute", "1day"])
    ap.add_argument("--force", action="store_true", help="also retry days that already came back empty 3 times")
    ap.add_argument("--dry-run", action="store_true", help="show the plan only; no API calls")
    args = ap.parse_args(argv)

    settings, store = bootstrap("market_data")
    log = get_logger("market")
    md = settings.market_data
    names = [n.upper() for n in (args.instruments or md.instruments)]
    for n in names:
        settings.instrument(n)  # fail fast on unknown instruments
    interval = args.interval or md.timeframe
    start = args.start or md.start_date
    end_setting = args.end or md.end_date
    dl = MarketDownloader(settings, store, None)

    # ---- plan (DuckDB only, no API) -------------------------------------------
    plans = {}
    print("\nMarket Data Downloader\n")
    for name in names:
        inst = settings.instrument(name)
        cal = dl.calendar_for(inst)
        end = dl.resolve_end(inst, end_setting)
        first, last = store.market_coverage(inst.name, inst.exchange, interval, start, end)
        todo, skipped, n_days, complete = dl.missing_days(inst, start, end, interval, args.force)
        plans[name] = (end, todo)
        print(f"Instrument : {inst.name} (Breeze stock code {inst.stock_code})")
        print(f"Exchange   : {inst.exchange}  (calendar {inst.calendar})")
        print(f"Interval   : {interval}")
        print(f"Configured : {start} → {end_setting}" + (f"  (resolves to {end})" if str(end_setting) != str(end) else ""))
        print(f"Existing   : {f'{first} → {last}' if first else 'no data yet'}  "
              f"({complete}/{n_days} trading days complete)")
        print(f"Missing    : {len(todo)} trading days: {date_ranges(todo, cal)}")
        if skipped:
            print(f"Skipped    : {len(skipped)} days returned no/incomplete data {md.max_attempts_per_day} times "
                  f"(use --force to retry): {date_ranges(skipped, cal)}")
        print()

    if args.dry_run:
        store.close()
        return EXIT_OK

    # ---- download ----------------------------------------------------------------
    exit_code = EXIT_OK
    if any(todo for _, todo in plans.values()):
        dl.client = connect_client(settings, store)
        try:
            for name in names:
                if not plans[name][1]:
                    log.info("%s: already up to date", name)
                    continue
                s = dl.download(name, start, plans[name][0], timeframe=interval, force=args.force)
                print(f"\n{name}: batches {s.batches_done}/{s.batches_total} | rows downloaded {s.rows_downloaded:,} "
                      f"| rows inserted {s.rows_inserted:,} | API calls {s.api_calls} "
                      f"| failed batches {len(s.failed_batches)}\n")
                if s.failed_batches:
                    exit_code = EXIT_ERROR
                if s.stopped_reason:
                    exit_code = EXIT_API_LIMIT if "limit" in s.stopped_reason.lower() else EXIT_ERROR
                    break
        except KeyboardInterrupt:
            log.warning("Interrupted. Every finished batch is already committed; re-run the same command to continue.")
            exit_code = EXIT_ERROR
    else:
        log.info("Nothing to download: DuckDB already covers the configured range.")

    rep = build_report(settings, store, names, start=start, end=end_setting, timeframe=interval)
    print("\n" + render_text(rep))
    txt, _ = write_report(rep, settings.paths.report_dir)
    log.info("Report saved to %s", txt)
    store.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
