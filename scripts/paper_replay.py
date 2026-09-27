"""Replay backtest signals through the Zerodha executor with a paper broker (offline, no orders sent).

    python3 scripts/paper_replay.py --hedge-width 200
    python3 scripts/paper_replay.py --strategy zerodte --hedge-width 300 --start 2026-07-27 --end 2026-09-25

Glue between the two sides: the backtest emits strategy_signals.OrderIntents and
zerodha.Executor executes them on zerodha.PaperBroker, filling each leg at the
backtest's own price. The paper P&L must equal the backtest's gross P&L, which
checks leg ordering, quantities and position bookkeeping end to end.
"""
import argparse
import sys
from datetime import timedelta

import _path  # noqa: F401

from backtest.data import DataFeed
from backtest.hedged import HedgedStrategy, HedgeParams
from backtest.signals import trades_to_intents
from backtest.strategies import RangeBreakoutSeller, ZeroDteStraddleSeller
from trading_data.app import bootstrap, parse_date
from zerodha import Executor, InstrumentBook, PaperBroker, ZerodhaConfig


def synthetic_book(intents, lot: int) -> InstrumentBook:
    """Instrument rows for the contracts the intents use (real symbols come from kite.instruments live)."""
    rows = {}
    for i in intents:
        for l in i.legs:
            t = "CE" if l.right.value == "CALL" else "PE"
            sym = f"{l.underlying}{l.expiry:%y%m%d}{l.strike:g}{t}"
            rows[sym] = {"instrument_token": len(rows) + 1, "tradingsymbol": sym, "name": l.underlying,
                         "expiry": l.expiry.isoformat(), "strike": l.strike, "tick_size": 0.05, "lot_size": lot,
                         "instrument_type": t, "exchange": "NFO"}
    return InstrumentBook(rows.values())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strategy", choices=["positional", "zerodte"], default="positional")
    ap.add_argument("--hedge-width", type=int, default=200, help="0 = naked")
    ap.add_argument("--start", type=parse_date)
    ap.add_argument("--end", type=parse_date)
    args = ap.parse_args()

    settings, store = bootstrap("paper_replay")
    feed = DataFeed(settings, store, "NIFTY")
    end = args.end or store.market_coverage(feed.instrument.name, feed.instrument.exchange, "1minute")[1]
    start = args.start or feed.cal.next_trading_day(end - timedelta(days=61), include_self=True)
    feed.load_spot(start - timedelta(days=90), end)
    strat = (RangeBreakoutSeller if args.strategy == "positional" else ZeroDteStraddleSeller)(feed)
    if args.hedge_width:
        strat = HedgedStrategy(strat, HedgeParams(args.hedge_width))
    trades = strat.run(start, end)
    intents = trades_to_intents(trades)

    book = synthetic_book(intents, feed.profile.lot_size)
    broker = PaperBroker(funds=10_000_000)
    cfg = ZerodhaConfig(dry_run=False, order_type="MARKET", fill_timeout_s=0, poll_interval_s=0)
    ex = Executor(broker, book, cfg)
    failures = 0
    for intent in intents:
        for l in intent.legs:
            broker.prices[book.option(l.underlying, l.expiry, l.strike, l.right.value).key] = l.ref_price
        rep = ex.handle(intent)
        failures += not rep.ok
        print(f"{intent.ts:%Y-%m-%d %H:%M} {intent.action.value:5} {intent.position_id:18} "
              + " -> ".join(f"{p.side} {p.tradingsymbol}" for p in rep.plan) + ("" if rep.ok else f"  FAILED {rep.message}"))

    gross = sum(t.gross_pnl for t in trades)
    print(f"\n{strat.name}: {len(intents)} intents, {len([e for e in broker.log if e[0] == 'place'])} orders, "
          f"{failures} failures, open positions {len(ex.positions)}")
    print(f"Paper P&L (before costs) ₹{broker.cash:,.2f} vs backtest gross ₹{gross:,.2f}")
    store.close()
    return 0 if failures == 0 and abs(broker.cash - gross) < 1 and not ex.positions else 1


if __name__ == "__main__":
    sys.exit(main())
