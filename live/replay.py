"""BACKTEST mode: the live engine replayed minute by minute over candles stored in DuckDB.

ReplayMarketData serves exactly what BreezeMarketData would have served at each
simulated `now` (completed bars only), from backtest.data.DataFeed. Prices for
fills are the last completed bar's close, i.e. the backtest's price_at(ts) when
the engine acts one minute after the signal bar, so replay trades can be
compared one-to-one with backtest/strategies.py (see compare_with_backtest).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd

from backtest.data import DataFeed
from trading_data.storage import OptionContract

MINUTE = timedelta(minutes=1)


class ReplayMarketData:
    def __init__(self, feed: DataFeed):
        self.feed = feed
        self._hist: dict[tuple, pd.DataFrame] = {}

    def spot_bars(self, day: date, now: datetime) -> pd.DataFrame:
        df = self.feed.spot_day(day)
        if df is None:
            return pd.DataFrame(columns=["open", "high", "low", "close"])
        return df[df.index + MINUTE <= now]

    def option_bars(self, contract: OptionContract, day: date, now: datetime) -> pd.DataFrame:
        df = self.feed.option(contract.expiry, contract.strike, contract.right, [day])
        if df.empty:
            return df
        df = df[df.index.date == day]
        return df[df.index + MINUTE <= now]

    def option_price(self, contract: OptionContract, now: datetime, fresh: bool = False) -> float | None:
        """Last traded price up to now (looks back over earlier days like backtest price_at)."""
        key = (contract, now.date())
        if key not in self._hist:
            days = self.feed.cal.trading_days(now.date() - timedelta(days=10), min(now.date(), contract.expiry))
            self._hist[key] = self.feed.option(contract.expiry, contract.strike, contract.right, days) if days \
                else pd.DataFrame()
        df = self._hist[key]
        if df.empty:
            return None
        done = df[df.index + MINUTE <= now]
        return float(done["close"].iloc[-1]) if len(done) else None

    def api_budget_remaining(self) -> int | None:
        return None


def backtest_trades(feed: DataFeed, cfg, start: date, end: date) -> list:
    """The backtest's own trades for one instance's strategy and parameters (hedged -> one record per spread)."""
    from backtest.hedged import HedgedStrategy, HedgeParams, combine_positions
    from backtest.strategies import RangeBreakoutSeller, ZeroDteStraddleSeller
    from .strategies import params_from_config

    pos_p, zd_p = params_from_config(cfg, feed.profile.lot_size)
    strat = RangeBreakoutSeller(feed, pos_p) if cfg.strategy == "positional" else ZeroDteStraddleSeller(feed, zd_p)
    if cfg.hedged:
        trades = combine_positions(HedgedStrategy(strat, HedgeParams(cfg.hedge_width)).run(start, end))
    else:
        trades = strat.run(start, end)
    return [(cfg.strategy, t) for t in trades]


def compare(live: list[dict], bt: list) -> dict:
    """Match live-engine positions with backtest trades on (strategy, entry bar, strike, right)."""
    def k_live(r):
        return (r["strategy"], str(pd.Timestamp(r["entry_ts"])), float(r["strike"]), r["right"])

    def k_bt(name, t):
        return (name, str(pd.Timestamp(t.entry_ts)), float(t.strike), t.right)

    L = {k_live(r): r for r in live}
    B = {k_bt(n, t): t for n, t in bt}
    rows, same_exit, same_reason = [], 0, 0
    for k in sorted(set(L) | set(B)):
        r, t = L.get(k), B.get(k)
        if r and t:
            ex_same = str(pd.Timestamp(r["exit_ts"])) == str(pd.Timestamp(t.exit_ts))
            reason_same = r["exit_reason"].split(" (")[0] == t.exit_reason.split(" (")[0]
            same_exit += ex_same
            same_reason += reason_same
            rows.append(f"MATCH  {k[0]:10} {k[1]} {k[2]:g} {k[3]:4} entry {r['entry_price']:.2f}/{t.entry_price:.2f} "
                        f"exit {r['exit_ts'][11:16]}/{t.exit_ts:%H:%M} {r['exit_price']:.2f}/{t.exit_price:.2f} "
                        f"pnl {r['gross_pnl']:,.0f}/{t.gross_pnl:,.0f}"
                        + ("" if ex_same and reason_same else f"  <- {r['exit_reason']} | {t.exit_reason}"))
        elif r:
            rows.append(f"LIVE-ONLY {k} {r['exit_reason']} pnl {r['gross_pnl']:,.0f}")
        else:
            rows.append(f"BACKTEST-ONLY {k} {t.exit_reason} pnl {t.gross_pnl:,.0f}")
    both = set(L) & set(B)
    summary = {"live_positions": len(L), "backtest_trades": len(B), "matched": len(both),
               "same_exit_minute": same_exit, "same_exit_reason": same_reason,
               "live_gross_pnl": round(sum(r["gross_pnl"] for r in L.values()), 2),
               "backtest_gross_pnl": round(sum(t.gross_pnl for t in B.values()), 2),
               "matched_live_pnl": round(sum(L[k]["gross_pnl"] for k in both), 2),
               "matched_backtest_pnl": round(sum(B[k].gross_pnl for k in both), 2)}
    return {"summary": summary, "rows": rows}
