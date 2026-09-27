"""Historical options downloader (any configured underlying; NIFTY by default).

    python3 scripts/download_options.py                                   # NIFTY, EXPIRY_START..EXPIRY_END from .env/settings
    python3 scripts/download_options.py --underlying BANKNIFTY --expiry-start 2025-08-01 --expiry-end 2025-08-31
    python3 scripts/download_options.py --dry-run                         # expiries, ATM and strike plan only

Resumable: re-running skips contracts already stored, and continues after an
interrupt or after the daily API limit stopped the previous run.
"""
import argparse
import dataclasses
import sys

import _path  # noqa: F401

from trading_data.app import EXIT_API_LIMIT, EXIT_OK, bootstrap, connect_client, parse_date
from trading_data.downloaders.options import OptionsDownloader
from trading_data.log import get_logger
from trading_data.reports import build_report, render_text, write_report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--underlying", default="NIFTY", help="options profile from config/settings.toml")
    ap.add_argument("--expiry-start", type=parse_date)
    ap.add_argument("--expiry-end", type=parse_date)
    ap.add_argument("--threads", type=int, help="override max_threads")
    ap.add_argument("--strikes-each-side", type=int, help="override strikes_each_side")
    ap.add_argument("--retry-no-data", action="store_true", help="retry contracts that previously returned no data")
    ap.add_argument("--forward-fill", action="store_true",
                    help="legacy fill-to-375 bars, written to option_candles_synthetic (never the canonical table)")
    ap.add_argument("--dry-run", action="store_true", help="plan only: needs spot data already in DuckDB")
    args = ap.parse_args()

    settings, store = bootstrap("options")
    log = get_logger("options")
    profile = settings.options_profile(args.underlying)
    overrides = {}
    if args.threads:
        overrides["max_threads"] = max(1, args.threads)
    if args.strikes_each_side is not None:
        overrides["strikes_each_side"] = args.strikes_each_side
    if overrides:
        profile = dataclasses.replace(profile, **overrides)

    client = None if args.dry_run else connect_client(settings, store, profile.daily_api_limit, profile.api_delay)
    dl = OptionsDownloader(settings, store, client, profile)
    try:
        summary = dl.run(args.expiry_start, args.expiry_end, retry_no_data=args.retry_no_data,
                         forward_fill=True if args.forward_fill else None, dry_run=args.dry_run)
    except KeyboardInterrupt:
        log.warning("Interrupted. Completed contracts are saved; re-run to continue.")
        store.close()
        return EXIT_OK
    log.info("Run summary: %d expiries, %d contracts planned, %d skipped (already done), %d downloaded, "
             "%d no data, %d failed, %d used retry window, %d rows written, %d API calls",
             len(summary.expiries), summary.planned_contracts, summary.skipped_existing, summary.downloaded,
             summary.no_data, summary.failed, summary.retried, summary.rows_written, summary.api_calls)
    if not args.dry_run:
        rep = build_report(settings, store, instruments=[], option_profiles=[profile.name])
        print("\n" + render_text(rep))
        txt, _ = write_report(rep, settings.paths.report_dir)
        log.info("Report written to %s", txt)
    store.close()
    return EXIT_API_LIMIT if summary.stopped_reason and "limit" in summary.stopped_reason.lower() else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
