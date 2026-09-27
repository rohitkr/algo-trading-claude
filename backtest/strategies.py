"""The two option-selling strategies, as parameterised rule sets.

RangeBreakoutSeller (positional)
    Range = underlying high/low from range_start to range_end (09:15-11:15).
    First 1-minute CLOSE above the high -> sell an ITM PUT; below the low -> sell an ITM CALL.
    Strike = ATM -/+ itm_points on the next weekly expiry after the entry day.
    Stop loss: underlying closes sl_pct (0.5%) against the entry spot.
    Exit otherwise at exit_time on expiry day.
    One re-entry per signal, "at cost": after a stop, if the underlying returns
    to the original entry level, sell the same contract again (new 0.5% stop).

ZeroDteStraddleSeller (intraday, expiry day only)
    Sell a CALL and a PUT at the entry time, each `itm_points` in the money
    (ATM when itm_points = 0); each leg has a fixed stop at
    sl_pct above its entry premium (triggered on the 1-minute high, filled at the
    stop price). One re-entry per leg when its premium comes back to the entry
    price. Everything is closed at exit_time.
    Entry time is chosen walk-forward ("optimised"): for each expiry day, the
    candidate time with the best total P&L over the previous `lookback` expiry
    days is used. Only past data is used for each choice.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import pandas as pd

from trading_data.log import get_logger

from . import rules
from .data import DataFeed, price_at
from .engine import CostModel, Trade

log = get_logger("backtest")


# ---------------------------------------------------------------------------
# Positional: 2-hour range breakout, sell ITM option
# ---------------------------------------------------------------------------
@dataclass
class RangeBreakoutParams:
    range_start: time = time(9, 15)
    range_end: time = time(11, 15)       # range uses bars before this time
    last_entry: time = time(15, 0)
    exit_time: time = time(15, 15)       # on expiry day
    # Stops and re-entries are only evaluated up to this time each day. Breeze's NIFTY
    # feed often freezes 15:17-15:19 and jumps at 15:20, which would fire false stops.
    act_until: time = time(15, 15)
    itm_points: int = 100
    sl_pct: float = 0.5
    reentry: bool = True
    lots: int = 5                        # 5 x 65 = 325 qty
    one_position_at_a_time: bool = True
    expiry_offset: int = 0               # 0 = nearest weekly expiry after the entry day, 1 = the one after, ...


@dataclass
class RangeBreakoutSeller:
    feed: DataFeed
    params: RangeBreakoutParams = field(default_factory=RangeBreakoutParams)
    costs: CostModel = field(default_factory=CostModel)
    name: str = "Positional range breakout"
    signals: list[dict] = field(default_factory=list)

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
            after = spot.between_time(p.range_end, p.last_entry)
            sig = None
            for ts, bar in after.iterrows():
                # Upside is checked first: if both sides ever qualified on the same bar, the PE sale wins.
                direction = rules.breakout(bar["close"], hi, lo)
                if direction:
                    sig = (ts, direction, float(bar["close"]))
                    break
            record = {"day": str(d), "range_high": hi, "range_low": lo, "signal": sig[1] if sig else "none",
                      "signal_ts": str(sig[0]) if sig else None, "taken": False, "reason": ""}
            self.signals.append(record)
            if sig is None:
                record["reason"] = "no breakout"
                continue
            ts, direction, spot_px = sig
            if p.one_position_at_a_time and ts <= free_from:
                record["reason"] = "previous position still open"
                continue
            expiry = feed.next_expiry(d, strictly_after=True)
            for _ in range(p.expiry_offset):
                expiry = feed.next_expiry(expiry, strictly_after=True)
            right = rules.breakout_right(direction)
            strike = rules.itm_strike(spot_px, right, p.itm_points, feed.profile.strike_step)
            days = feed.cal.trading_days(d, expiry)
            opt = feed.option(expiry, strike, right, days)
            new = self._simulate(d, expiry, strike, right, direction, ts, spot_px, opt, qty)
            if not new:
                record["reason"] = "no option price at entry"
                continue
            record["taken"] = True
            trades += new
            free_from = max(t.exit_ts for t in new)
        return trades

    def _simulate(self, d, expiry, strike, right, direction, entry_ts, entry_spot, opt, qty) -> list[Trade]:
        p, feed = self.params, self.feed
        final_ts = datetime.combine(expiry, p.exit_time)
        path = feed.spot_path(d, expiry)
        path = path[(path.index > entry_ts) & (path.index <= final_ts)]
        last_data_ts = path.index.max() if len(path) else entry_ts
        trades: list[Trade] = []
        tid = f"PB-{d:%m%d}"

        def open_trade(ts, spot_px, reentry):
            px = price_at(opt, ts)
            if px is None:
                return None
            return Trade(self.name, tid + ("R" if reentry else ""), str(d), str(expiry), strike, right, "SELL", qty,
                         ts, px, spot_entry=spot_px, is_reentry=reentry,
                         note=f"{'close above' if direction == 'UP' else 'close below'} 2h range")

        trade = open_trade(entry_ts, entry_spot, False)
        if trade is None:
            return []
        reentered = False
        waiting_reentry = False
        for ts, bar in path.iterrows():
            close = float(bar["close"])
            if ts.time() > p.act_until:
                continue
            if trade is not None:
                if rules.spot_stop_hit(close, trade.spot_entry, right, p.sl_pct):
                    trades.append(trade.close(ts, price_at(opt, ts), f"stop: NIFTY {p.sl_pct}% against", self.costs, close))
                    trade = None
                    waiting_reentry = p.reentry and not reentered
                    continue
                if ts >= final_ts:
                    trades.append(trade.close(ts, price_at(opt, ts), "expiry-day exit", self.costs, close))
                    trade = None
                    break
            elif waiting_reentry and rules.reentry_window_open(ts, final_ts):
                if rules.spot_reentry_ok(close, entry_spot, right):
                    trade = open_trade(ts, close, True)
                    reentered, waiting_reentry = True, False
        if trade is not None:  # data ends before expiry: mark to market
            trades.append(trade.close(last_data_ts, price_at(opt, last_data_ts),
                                      "open at data end (marked to market)", self.costs,
                                      float(path["close"].iloc[-1]) if len(path) else None))
        return trades


# ---------------------------------------------------------------------------
# Intraday 0DTE: short ATM straddle on expiry day, walk-forward entry time
# ---------------------------------------------------------------------------
@dataclass
class ZeroDteParams:
    first_entry: time = time(9, 20)
    last_entry: time = time(14, 30)
    step_minutes: int = 10
    exit_time: time = time(15, 15)
    sl_pct: float = 30.0
    reentry: bool = True
    lookback: int = 8          # expiry days used to choose the entry time
    lots: int = 5              # 5 x 65 = 325 qty
    itm_points: int = 100      # ITM legs: CALL at ATM-100, PUT at ATM+100 (0 = ATM straddle)

    def candidate_times(self) -> list[time]:
        return rules.candidate_times(self.first_entry, self.last_entry, self.step_minutes)


@dataclass
class ZeroDteStraddleSeller:
    feed: DataFeed
    params: ZeroDteParams = field(default_factory=ZeroDteParams)
    costs: CostModel = field(default_factory=CostModel)
    name: str = "0DTE ITM straddle"
    grid: dict = field(default_factory=dict)          # (expiry, time) -> net P&L
    choices: list[dict] = field(default_factory=list)

    def run(self, start: date, end: date) -> list[Trade]:
        p, feed = self.params, self.feed
        test_days = feed.expiries(start, end)
        history = feed.expiries(start - timedelta(days=7 * (p.lookback + 2)), start - timedelta(days=1))[-p.lookback:]
        for e in history + test_days:
            for t in p.candidate_times():
                self.score(e, t)

        trades: list[Trade] = []
        all_days = history + test_days
        for e in test_days:
            prior = [x for x in all_days if x < e][-p.lookback:]
            best, scores = self.choose_entry_time(prior)
            day_trades = self._day_trades(e, best)
            self.choices.append({"expiry": str(e), "entry_time": best.strftime("%H:%M"),
                                 "training_days": len(prior), "training_pnl": round(scores[best], 2),
                                 "day_pnl": round(sum(t.net_pnl for t in day_trades), 2)})
            trades += day_trades
        return trades

    def score(self, e: date, t: time) -> float:
        """Net P&L of entering at `t` on expiry day `e` (cached in self.grid)."""
        if (e, t) not in self.grid:
            self.grid[(e, t)] = sum(tr.net_pnl for tr in self._day_trades(e, t))
        return self.grid[(e, t)]

    def prior_expiries(self, e: date) -> list[date]:
        """The `lookback` expiry days before `e` used to choose its entry time."""
        p = self.params
        return self.feed.expiries(e - timedelta(days=7 * (p.lookback + 2)), e - timedelta(days=1))[-p.lookback:]

    def choose_entry_time(self, prior: list[date]) -> tuple[time, dict[time, float]]:
        """Walk-forward entry time from past expiry days only (shared with live trading)."""
        times = self.params.candidate_times()
        scores = {t: sum(self.score(x, t) for x in prior) for t in times}
        return rules.best_entry_time(times, scores), scores

    def _day_trades(self, e: date, t: time) -> list[Trade]:
        p, feed = self.params, self.feed
        spot = feed.spot_day(e)
        entry_ts = datetime.combine(e, t)
        if spot is None or entry_ts not in spot.index:
            return []
        spot_px = float(spot.at[entry_ts, "close"])
        strikes = rules.straddle_strikes(spot_px, p.itm_points, feed.profile.strike_step)
        qty = p.lots * feed.profile.lot_size
        out = []
        for right in ("CALL", "PUT"):
            strike = strikes[right]
            opt = feed.option(e, strike, right, [e])
            out += self._leg(e, t, strike, right, opt, spot_px, qty)
        return out

    def _leg(self, e, t, strike, right, opt, spot_px, qty) -> list[Trade]:
        p = self.params
        entry_ts = datetime.combine(e, t)
        exit_ts = datetime.combine(e, p.exit_time)
        entry = price_at(opt, entry_ts)
        if entry is None or opt.empty:
            return []
        bars = opt[(opt.index > entry_ts) & (opt.index <= exit_ts)]
        tid = f"ZD-{e:%m%d}-{t:%H%M}-{right[0]}"
        trade = Trade(self.name, tid, str(e), str(e), strike, right, "SELL", qty, entry_ts, entry, spot_entry=spot_px,
                      note=f"ITM {p.itm_points} pts, entry {t:%H:%M}")
        first_entry = entry
        stop = rules.premium_stop_level(entry, p.sl_pct)
        done, reentered, waiting = [], False, False
        for ts, bar in bars.iterrows():
            if trade is not None:
                if rules.premium_stop_hit(bar["high"], stop):
                    fill = rules.premium_stop_fill(stop, bar["open"])   # gap through the stop fills at the open
                    done.append(trade.close(ts, round(fill, 2), f"stop: premium +{p.sl_pct:.0f}%", self.costs))
                    trade = None
                    waiting = p.reentry and not reentered
                    continue
            elif waiting and rules.premium_reentry_ok(float(bar["close"]), first_entry) and ts < exit_ts:
                trade = Trade(self.name, tid + "R", str(e), str(e), strike, right, "SELL", qty, ts,
                              float(bar["close"]), spot_entry=None, is_reentry=True, note="re-entry at cost")
                stop = rules.premium_stop_level(trade.entry_price, p.sl_pct)
                reentered, waiting = True, False
        if trade is not None:
            last = bars.index.max() if len(bars) else entry_ts
            done.append(trade.close(last, price_at(opt, last), "time exit 15:15", self.costs))
        return done
