"""Data-quality report straight from DuckDB (no API calls).

    python3 scripts/data_report.py
    python3 scripts/data_report.py --instruments NIFTY --options NIFTY BANKNIFTY
"""
import argparse
import sys

import _path  # noqa: F401

from trading_data.app import bootstrap, parse_date
from trading_data.reports import build_report, render_text, write_report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instruments", nargs="*", help="default: market_data.instruments")
    ap.add_argument("--options", nargs="*", default=["NIFTY"], help="options profiles to include")
    ap.add_argument("--start", type=parse_date)
    ap.add_argument("--end", type=parse_date)
    args = ap.parse_args()
    settings, store = bootstrap("report")
    names = [n.upper() for n in args.instruments] if args.instruments is not None else None
    rep = build_report(settings, store, names, [o.upper() for o in args.options], args.start, args.end)
    print(render_text(rep))
    txt, js = write_report(rep, settings.paths.report_dir)
    print(f"\nSaved: {txt}\n       {js}")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
