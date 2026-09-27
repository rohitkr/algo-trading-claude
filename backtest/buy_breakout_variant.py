"""Exploratory: the positional range breakout, but BUYING the option instead of selling it.

Same as RangeBreakoutSeller (backtest/strategies.py) except the side and the option right:
    close above the 09:15-11:15 range -> BUY a CALL;  close below -> BUY a PUT.
Everything else is identical: range, entry bar and price, next weekly expiry, 0.5% underlying stop
against the entry spot (checked up to 15:15 each day), one re-entry at cost when the underlying
comes back to the original entry level, exit at 15:15 on expiry day, 5 lots.

Strike variants (`strike`): ITM (CALL at ATM-100, PUT at ATM+100, the same ITM distance as the
seller), ATM, OTM (CALL at ATM+100, PUT at ATM-100).
Optional `premium_sl_pct`: also exit when the option closes that % below the entry premium.

Standalone and throwaway: does not change strategies.py / rules.py / live / zerodha.

    python3 backtest/buy_breakout_variant.py --start 2026-07-27 --end 2026-09-25
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest import rules  # noqa: E402
from backtest.data import DataFeed, price_at  # noqa: E402
from backtest.engine import CostModel, Trade, metrics  # noqa: E402
from backtest.strategies import RangeBreakoutParams  # noqa: E402
from trading_data.strikes import atm_strike  # noqa: E402


@dataclass
class RangeBreakoutBuyer:
    feed: DataFeed
    params: RangeBreakoutParams = field(default_factory=RangeBreakoutParams)
    strike_mode: str = "ITM"               # ITM | ATM | OTM
    premium_sl_pct: float | None = None    # e.g. 30 = also exit when the option is 30% below entry
    costs: CostModel = field(default_factory=CostModel)
    missing: int = 0

    @property
    def name(self) -> str:
        return f"Breakout BUY {self.strike_mode}" + (f" +{self.premium_sl_pct:g}% prem SL" if self.premium_sl_pct else "")

    def strike_for(self, spot: float, right: str) -> float:
        atm = atm_strike(spot, self.feed.profile.strike_step)
        off = {"ITM": -self.params.itm_points, "ATM": 0, "OTM": self.params.itm_points}[self.strike_mode]
        return atm + off if right == "CALL" else atm - off

    def run(self, start: date, end: date) -> list[Trade]:
        p, feed = self.params, self.feed
        qty = p.lots * feed.profile.lot_size
        trades: list[Trade] = []
        free_from = datetime.min
        for d in feed.cal.trading_days(start, end):
            spot = feed.spot_day(d)
            if spot is None or len(spot) < 100:
                continue
            hi, lo = rules.range_levels(rules.range_bars(spot, d, p.range_start, p.range_end))
            sig = None
            for ts, bar in spot.between_time(p.range_end, p.last_entry).iterrows():
                direction = rules.breakout(bar["close"], hi, lo)
                if direction:
                    sig = (ts, direction, float(bar["close"]))
                    break
            if sig is None or (p.one_position_at_a_time and sig[0] <= free_from):
                continue
            ts, direction, spot_px = sig
            expiry = feed.next_expiry(d, strictly_after=True)
            right = "CALL" if direction == rules.UP else "PUT"
            strike = self.strike_for(spot_px, right)
            opt = feed.option(expiry, strike, right, feed.cal.trading_days(d, expiry))
            new = self._simulate(d, expiry, strike, right, direction, ts, spot_px, opt, qty)
            if not new:
                self.missing += 1
                continue
            trades += new
            free_from = max(t.exit_ts for t in new)
        return trades

    def _simulate(self, d, expiry, strike, right, direction, entry_ts, entry_spot, opt, qty) -> list[Trade]:
        p, feed = self.params, self.feed
        # the underlying stop runs against the breakout direction: same trigger as the seller
        stop_right = rules.breakout_right(direction)          # UP -> "PUT" semantics = stop on a fall
        final_ts = datetime.combine(expiry, p.exit_time)
        path = feed.spot_path(d, expiry)
        path = path[(path.index > entry_ts) & (path.index <= final_ts)]
        last_data_ts = path.index.max() if len(path) else entry_ts
        trades, tid = [], f"PBB-{d:%m%d}"

        def open_trade(ts, spot_px, reentry):
            px = price_at(opt, ts)
            if px is None:
                return None
            return Trade(self.name, tid + ("R" if reentry else ""), str(d), str(expiry), strike, right, "BUY", qty,
                         ts, px, spot_entry=spot_px, is_reentry=reentry, note=f"breakout {direction}")

        trade = open_trade(entry_ts, entry_spot, False)
        if trade is None:
            return []
        reentered = waiting = False
        for ts, bar in path.iterrows():
            close = float(bar["close"])
            if ts.time() > p.act_until:
                continue
            if trade is not None:
                opx = price_at(opt, ts)
                if rules.spot_stop_hit(close, trade.spot_entry, stop_right, p.sl_pct):
                    trades.append(trade.close(ts, opx, f"stop: NIFTY {p.sl_pct}% against", self.costs, close))
                    trade, waiting = None, p.reentry and not reentered
                    continue
                if self.premium_sl_pct and opx is not None and opx <= trade.entry_price * (1 - self.premium_sl_pct / 100):
                    trades.append(trade.close(ts, opx, f"stop: premium -{self.premium_sl_pct:g}%", self.costs, close))
                    trade, waiting = None, p.reentry and not reentered
                    continue
                if ts >= final_ts:
                    trades.append(trade.close(ts, opx, "expiry-day exit", self.costs, close))
                    trade = None
                    break
            elif waiting and rules.reentry_window_open(ts, final_ts):
                if rules.spot_reentry_ok(close, entry_spot, stop_right):
                    trade = open_trade(ts, close, True)
                    reentered, waiting = True, False
        if trade is not None:
            trades.append(trade.close(last_data_ts, price_at(opt, last_data_ts), "open at data end (marked to market)",
                                      self.costs, float(path["close"].iloc[-1]) if len(path) else None))
        return trades


def summary(name: str, trades: list[Trade], missing: int = 0) -> str:
    m = metrics(trades)
    pf = f"{m.profit_factor:.2f}" if m.profit_factor is not None else "n/a"
    stops = sum(t.exit_reason.startswith("stop") for t in trades)
    return (f"{name:34} trades {m.trades:3}  net ₹{m.net_pnl:>11,.0f}  win {m.win_rate:5.1f}%  PF {pf:>5}  "
            f"maxDD ₹{m.max_drawdown:>10,.0f}  stops {stops:2}  re-entries {m.reentries}  no-data {missing}")


def main() -> int:
    from backtest.strategies import RangeBreakoutSeller
    from trading_data.app import bootstrap, connect_client

    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=date.fromisoformat, required=True)
    ap.add_argument("--end", type=date.fromisoformat, required=True)
    ap.add_argument("--offline", action="store_true", help="use only option data already in DuckDB")
    args = ap.parse_args()
    settings, store = bootstrap("buy_breakout")
    client = None if args.offline else connect_client(settings, store)
    feed = DataFeed(settings, store, "NIFTY", client)
    feed.load_spot(args.start - timedelta(days=10), args.end)
    print(f"\n{args.start} .. {args.end}")
    sell = RangeBreakoutSeller(feed)
    print(summary("SELL ITM (existing strategy)", sell.run(args.start, args.end),
                  sum(1 for s in sell.signals if s["reason"] == "no option price at entry")))
    for mode in ("ITM", "ATM", "OTM"):
        for psl in (None, 30.0):
            b = RangeBreakoutBuyer(feed, strike_mode=mode, premium_sl_pct=psl)
            print(summary(b.name, b.run(args.start, args.end), b.missing))
    if client:
        print(f"Breeze API calls: {client.budget.used_this_run}")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
