"""Backtest the NIFTY option-selling strategies on data in DuckDB and write an HTML report.

    python3 scripts/run_backtest.py                                  # last 2 months of stored NIFTY data
    python3 scripts/run_backtest.py --start 2026-07-27 --end 2026-09-25
    python3 scripts/run_backtest.py --offline                        # use only option data already in DuckDB
    python3 scripts/run_backtest.py --hedge-width 200                # hedged variant: buy a wing 200 pts further OTM

Option contracts the trades need are fetched from Breeze on first use (needs
today's session token) and stored in DuckDB, so re-runs are offline.
"""
import argparse
import json
import sys
from dataclasses import asdict
from datetime import date, timedelta

import _path  # noqa: F401

from backtest.data import DataFeed
from backtest.engine import metrics
from backtest.hedged import HedgedStrategy, HedgeParams, combine_positions
from backtest.report import write_html
from backtest.strategies import RangeBreakoutSeller, ZeroDteStraddleSeller
from trading_data.app import bootstrap, connect_client, parse_date
from trading_data.log import get_logger


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--underlying", default="NIFTY")
    ap.add_argument("--start", type=parse_date)
    ap.add_argument("--end", type=parse_date)
    ap.add_argument("--capital", type=float, default=1_000_000)
    ap.add_argument("--offline", action="store_true", help="do not call Breeze for missing option data")
    ap.add_argument("--hedge-width", type=int, default=0,
                    help="buy a protective wing this many points further OTM per sold leg (0 = naked, the default)")
    ap.add_argument("--out", help="HTML path (default reports/backtest_<start>_<end>.html)")
    args = ap.parse_args()

    settings, store = bootstrap("backtest")
    log = get_logger("backtest")
    feed_probe = DataFeed(settings, store, args.underlying)
    last = store.market_coverage(feed_probe.instrument.name, feed_probe.instrument.exchange, "1minute")[1]
    end = args.end or last
    start = args.start or feed_probe.cal.next_trading_day(end - timedelta(days=61), include_self=True)
    client = None if args.offline else connect_client(settings, store)
    feed = DataFeed(settings, store, args.underlying, client)
    feed.load_spot(start - timedelta(days=90), end)
    log.info("Backtest %s %s -> %s, capital %.0f", args.underlying, start, end, args.capital)

    positional = RangeBreakoutSeller(feed)
    intraday = ZeroDteStraddleSeller(feed)
    if args.hedge_width:
        hedge = HedgeParams(wing_points=args.hedge_width)
        positional, intraday = HedgedStrategy(positional, hedge), HedgedStrategy(intraday, hedge)
    results = []
    for strat in (positional, intraday):
        trades = strat.run(start, end)
        if args.hedge_width:   # one row per spread (sold leg + wing), so counts and drawdown match the naked run
            trades = combine_positions(trades)
        m = metrics(trades)
        log.info("%s: %d trades, net %.0f, win rate %.1f%%, max DD %.0f", strat.name, m.trades, m.net_pnl,
                 m.win_rate, m.max_drawdown)
        results.append((strat, trades, m))

    out = args.out or str(settings.paths.report_dir / f"backtest_{args.underlying.lower()}_{start}_{end}"
                                                         f"{f'_hedged{args.hedge_width}' if args.hedge_width else ''}.html")
    payload = write_html(out, args.underlying, start, end, args.capital, results)
    json_path = out.rsplit(".", 1)[0] + ".json"
    with open(json_path, "w") as fh:
        json.dump(payload, fh, indent=2, default=str)
    all_m = metrics([t for _, trades, _ in results for t in trades])
    print(f"\nCombined: {all_m.trades} trades, net P&L ₹{all_m.net_pnl:,.0f} "
          f"({100 * all_m.net_pnl / args.capital:.2f}% of ₹{args.capital:,.0f}), "
          f"win rate {all_m.win_rate}%, max drawdown ₹{all_m.max_drawdown:,.0f}")
    print(f"Report: {out}\nData:   {json_path}")
    if client:
        print(f"Breeze API calls this run: {client.budget.used_this_run}")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
