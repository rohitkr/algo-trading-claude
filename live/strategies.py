"""The two backtested strategies as minute-by-minute state machines for live trading.

Every decision calls the same functions as backtest/strategies.py
(backtest/rules.py) with the same parameters (RangeBreakoutParams /
ZeroDteParams), so live and backtest cannot drift apart. What necessarily
differs is timing: a live bar is only acted on once it has completed, so orders
go out in the minute after the backtest's fill minute. `python3 -m live replay`
runs these classes over DuckDB history and compares them with the backtest.

State is plain JSON (saved by the engine every poll) so a positional trade
carried overnight survives restarts.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from typing import Callable

import pandas as pd

from backtest import rules
from backtest.strategies import RangeBreakoutParams, ZeroDteParams
from strategy_signals import Action

from .interfaces import Signal, StrategyContext, contract_from_dict, contract_to_dict

MINUTE = timedelta(minutes=1)


def _ts(v: str | None) -> datetime | None:
    return datetime.fromisoformat(v) if v else None


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


class _Base:
    name = ""

    def __init__(self):
        self.notes: list[dict] = []          # decisions without an order, drained into the audit log

    def note(self, ts, event: str, **fields) -> None:
        self.notes.append({"strategy": self.name, "bar_ts": _iso(ts) if isinstance(ts, datetime) else ts,
                           "decision": event, **fields})


# ---------------------------------------------------------------------------------------------
# Strategy A: positional 2-hour range breakout (backtest RangeBreakoutSeller)
# ---------------------------------------------------------------------------------------------
class PositionalBreakout(_Base):
    name = "positional"

    def __init__(self, params: RangeBreakoutParams, min_range_bars: int = 100,
                 new_signal_cancels_reentry: bool = True, intraday_exit: time | None = None):
        super().__init__()
        self.p = params
        # INTRADAY_ONLY: the position (and its re-entry window) ends at this time on the entry day
        # instead of p.exit_time on the next weekly expiry. This is a live-only change from the backtest.
        self.intraday_exit = intraday_exit
        self.min_range_bars = min_range_bars
        self.new_signal_cancels_reentry = new_signal_cancels_reentry
        self.s: dict = {"cursor": None, "day": None, "hi": None, "lo": None, "day_done": False,
                        "pos": None, "waiting": None, "last_exit_ts": None}

    def get_state(self) -> dict:
        return self.s

    def set_state(self, state: dict) -> None:
        self.s.update(state or {})

    # -- engine hooks ------------------------------------------------------------------------
    def on_poll(self, ctx: StrategyContext) -> list[Signal]:
        bars = ctx.spot_bars()
        if bars is None or bars.empty:
            return []
        cursor = _ts(self.s["cursor"])
        new = bars[bars.index > cursor] if cursor else bars
        out: list[Signal] = []
        for ts, bar in new.iterrows():
            out += self._on_bar(ts.to_pydatetime(), bar, bars, ctx)
            self.s["cursor"] = _iso(ts.to_pydatetime())
        return out

    def on_entry_result(self, signal: Signal, ok: bool) -> None:
        pos = self.s["pos"]
        if not ok and pos and pos["pid"] == signal.position_id:
            self.s["pos"] = None            # refused entry: no position, and no re-entry for it
            self.note(signal.ts, "entry_not_executed", position_id=signal.position_id)

    def on_external_close(self, position_id: str, reason: str) -> None:
        """The engine closed or dropped this position itself (force exit, manual exit, square-off,
        lost track of it): stop managing it and never re-enter it."""
        pos, waiting = self.s["pos"], self.s["waiting"]
        if pos and pos["pid"] == position_id:
            self.s["pos"] = None
        if waiting and waiting["pid"].rstrip("R") == position_id.rstrip("R"):
            self.s["waiting"] = None
        self.note(None, "stopped_managing", position_id=position_id, reason=reason)

    # -- rules -------------------------------------------------------------------------------
    def _on_bar(self, ts: datetime, bar, day_bars: pd.DataFrame, ctx: StrategyContext) -> list[Signal]:
        p, out = self.p, []
        close = float(bar["close"])
        exited_now = False

        # 1) manage the open position / pending re-entry (backtest RangeBreakoutSeller._simulate)
        pos, waiting = self.s["pos"], self.s["waiting"]
        live = pos or waiting
        if live:
            final_ts = _ts(live["final_ts"])
            if pos and ts > _ts(pos["entry_ts"]):
                if ts.time() <= p.act_until and ts <= final_ts and \
                        rules.spot_stop_hit(close, pos["entry_spot"], pos["right"], p.sl_pct):
                    out.append(self._exit(pos, ts, close, f"stop: NIFTY {p.sl_pct}% against"))
                    self.s["waiting"] = pos if (p.reentry and not pos["reentered"]) else None
                    exited_now = True
                elif ts >= final_ts:     # the 15:15 bar on expiry day (or the first bar after it if it is missing)
                    out.append(self._exit(pos, ts, close, "intraday exit" if self.intraday_exit else
                                          "expiry-day exit"))
                    self.s["waiting"] = None
                    exited_now = True
            elif waiting and not pos and not exited_now and ts.time() <= p.act_until:
                if not rules.reentry_window_open(ts, final_ts):
                    if ts >= final_ts:
                        self.s["waiting"] = None
                        self.note(ts, "reentry_expired", position_id=waiting["pid"])
                elif rules.spot_reentry_ok(close, waiting["orig_entry_spot"], waiting["right"]):
                    out.append(self._enter(ctx, ts, close, contract_from_dict(waiting["contract"]),
                                           waiting["direction"], waiting["pid"] + "R", reentry=True,
                                           orig=waiting))
                    self.s["waiting"] = None

        # 2) today's range and the first breakout (backtest RangeBreakoutSeller.run)
        d = ts.date()
        if self.s["day"] != d.isoformat():
            self.s.update(day=d.isoformat(), hi=None, lo=None, day_done=False)
        if self.s["day_done"] or not (p.range_end <= ts.time() <= p.last_entry):
            return out
        if self.s["hi"] is None:
            day = day_bars[day_bars.index.date == d]
            rng = rules.range_bars(day, d, p.range_start, p.range_end)
            if len(rng) < self.min_range_bars:
                self.s["day_done"] = True
                self.note(ts, "no_trade_day", reason=f"only {len(rng)} range bars (< {self.min_range_bars})")
                return out
            self.s["hi"], self.s["lo"] = rules.range_levels(rng)
            self.note(ts, "range", range_high=self.s["hi"], range_low=self.s["lo"], bars=len(rng))
        direction = rules.breakout(close, self.s["hi"], self.s["lo"])
        if not direction:
            return out
        self.s["day_done"] = True           # only the first breakout of the day counts
        last_exit = _ts(self.s["last_exit_ts"])
        busy = self.s["pos"] is not None or exited_now or (last_exit is not None and ts <= last_exit)
        if self.s["waiting"] and not self.new_signal_cancels_reentry:
            busy = True
        if busy:
            self.note(ts, "signal_skipped", signal=direction, spot=close, reason="previous position still open")
            return out
        if self.s["waiting"]:
            self.note(ts, "reentry_cancelled", position_id=self.s["waiting"]["pid"], reason="new breakout signal")
            self.s["waiting"] = None
        contract = ctx.selector.positional(close, direction, d, p.itm_points)
        out.append(self._enter(ctx, ts, close, contract, direction, f"PB-{d:%Y%m%d}", reentry=False))
        return out

    def _enter(self, ctx, ts, spot, contract, direction, pid, reentry: bool, orig: dict | None = None) -> Signal:
        p = self.p
        final_ts = datetime.combine(ts.date(), self.intraday_exit) if self.intraday_exit else \
            datetime.combine(contract.expiry, p.exit_time)
        self.s["pos"] = {"pid": pid, "contract": contract_to_dict(contract), "right": contract.right,
                         "direction": direction, "entry_spot": spot, "entry_ts": _iso(ts),
                         "orig_entry_spot": orig["orig_entry_spot"] if orig else spot,
                         "final_ts": _iso(final_ts), "reentered": reentry, "is_reentry": reentry}
        stop = rules.spot_stop_level(spot, contract.right, p.sl_pct)
        return Signal(self.name, Action.ENTRY, pid, contract, ts, spot,
                      ("re-entry at cost: NIFTY back at " if reentry else
                       f"{'close above' if direction == rules.UP else 'close below'} 2h range "
                       f"[{self.s['lo']:.2f}, {self.s['hi']:.2f}]: ") + f"{spot:.2f}",
                      lots=p.lots, ref_price=ctx.market.option_price(contract, ctx.now),
                      stop={"basis": "spot", "level": round(stop, 2), "pct": p.sl_pct, "ref": spot},
                      is_reentry=reentry,
                      meta={"direction": direction, "final_exit": _iso(final_ts),
                            "range_high": self.s["hi"], "range_low": self.s["lo"]})

    def _exit(self, pos: dict, ts: datetime, spot: float, reason: str) -> Signal:
        self.s["pos"] = None
        self.s["last_exit_ts"] = _iso(ts)
        return Signal(self.name, Action.EXIT, pos["pid"], contract_from_dict(pos["contract"]), ts, spot, reason,
                      is_reentry=pos["is_reentry"],
                      stop={"basis": "spot", "level": round(rules.spot_stop_level(pos["entry_spot"], pos["right"],
                                                                                  self.p.sl_pct), 2)})


# ---------------------------------------------------------------------------------------------
# Strategy B: 0DTE ITM straddle on expiry day (backtest ZeroDteStraddleSeller)
# ---------------------------------------------------------------------------------------------
EntryTimeChooser = Callable[[date], tuple[time | None, dict]]


class ZeroDteStraddle(_Base):
    name = "zerodte"

    def __init__(self, params: ZeroDteParams, choose_entry_time: EntryTimeChooser,
                 quote_stops: bool = True, max_entry_delay: timedelta = timedelta(minutes=3)):
        super().__init__()
        self.p = params
        self.choose_entry_time = choose_entry_time
        self.quote_stops = quote_stops
        self.max_entry_delay = max_entry_delay
        self.s: dict = {"day": None, "entry_time": None, "entered": False, "legs": {}}

    def get_state(self) -> dict:
        return self.s

    def set_state(self, state: dict) -> None:
        self.s.update(state or {})

    def on_entry_result(self, signal: Signal, ok: bool) -> None:
        leg = self.s["legs"].get(signal.contract.right)
        if not ok and leg and leg["pid"] == signal.position_id:
            leg.update(open=False, waiting=False)
            self.note(signal.ts, "entry_not_executed", position_id=signal.position_id)

    def on_external_close(self, position_id: str, reason: str) -> None:
        """Stop managing this leg (force exit / manual exit / square-off): no stop, no re-entry."""
        for leg in self.s["legs"].values():
            if leg["pid"] == position_id:
                leg.update(open=False, waiting=False, reentered=True)
        self.note(None, "stopped_managing", position_id=position_id, reason=reason)

    # -- engine hook -------------------------------------------------------------------------
    def on_poll(self, ctx: StrategyContext) -> list[Signal]:
        d, now, p = ctx.today, ctx.now, self.p
        if not ctx.selector.is_expiry(d):
            return []
        if self.s["day"] != d.isoformat():
            t, info = self.choose_entry_time(d)
            self.s.update(day=d.isoformat(), entry_time=t.strftime("%H:%M") if t else None, entered=False, legs={})
            self.note(now, "entry_time_chosen" if t else "no_trade_day", entry_time=self.s["entry_time"], **info)
        if not self.s["entry_time"]:
            return []
        entry_ts = datetime.combine(d, time.fromisoformat(self.s["entry_time"]))
        exit_ts = datetime.combine(d, p.exit_time)
        out: list[Signal] = []

        if not self.s["entered"] and now >= entry_ts + MINUTE:
            out += self._entry(ctx, entry_ts)

        for right, leg in self.s["legs"].items():
            c = contract_from_dict(leg["contract"])
            bars = ctx.market.option_bars(c, d, now)
            cursor = max(_ts(leg["cursor"]), _ts(leg["entry_ts"]))
            new = bars[(bars.index > cursor) & (bars.index <= exit_ts)] if not bars.empty else bars
            for ts, bar in new.iterrows():
                ts = ts.to_pydatetime()
                out += self._on_option_bar(leg, c, ts, bar, exit_ts)
                leg["cursor"] = _iso(ts)
            # between bars: a live quote at/above the stop means this minute's high will be too
            if self.quote_stops and leg["open"] and now < exit_ts + MINUTE:
                px = ctx.market.option_price(c, now, fresh=True)
                if px is not None and rules.premium_stop_hit(px, leg["stop"]):
                    out.append(self._stop(leg, c, now.replace(second=0, microsecond=0), px, "quote"))

        if now >= exit_ts + MINUTE:        # the 15:15 bar has completed: time exit
            for right, leg in self.s["legs"].items():
                if leg["open"]:
                    leg.update(open=False, waiting=False)
                    out.append(Signal(self.name, Action.EXIT, leg["pid"], contract_from_dict(leg["contract"]),
                                      exit_ts, None, "time exit 15:15", is_reentry=leg["is_reentry"]))
                elif leg["waiting"]:
                    leg["waiting"] = False
        return out

    def _entry(self, ctx: StrategyContext, entry_ts: datetime) -> list[Signal]:
        p, now = self.p, ctx.now
        spot = ctx.spot_bars()
        self.s["entered"] = True
        if entry_ts not in spot.index:
            if now < entry_ts + MINUTE + self.max_entry_delay:
                self.s["entered"] = False           # bar not delivered yet; try again next poll
            else:
                self.note(entry_ts, "entry_missed", reason="no underlying bar at the entry minute")
            return []
        spot_px = float(spot.at[entry_ts, "close"])
        out = []
        for right, c in ctx.selector.zerodte(spot_px, ctx.today, p.itm_points).items():
            bars = ctx.market.option_bars(c, ctx.today, now)
            upto = bars[bars.index <= entry_ts] if not bars.empty else bars
            ref = float(upto["close"].iloc[-1]) if len(upto) else ctx.market.option_price(c, now, fresh=True)
            if ref is None:
                self.note(entry_ts, "leg_skipped", contract=c.label, reason="no option price at entry")
                continue
            pid = f"ZD-{entry_ts:%Y%m%d-%H%M}-{right[0]}"
            leg = {"pid": pid, "contract": contract_to_dict(c), "first_entry": ref, "entry_ref": ref,
                   "stop": rules.premium_stop_level(ref, p.sl_pct), "open": True, "waiting": False,
                   "reentered": False, "is_reentry": False, "entry_ts": _iso(entry_ts), "cursor": _iso(entry_ts),
                   "stopped_at": None}
            self.s["legs"][right] = leg
            out.append(self._signal_entry(leg, c, entry_ts, spot_px, f"ITM {p.itm_points} pts, entry {entry_ts:%H:%M}"))
        return out

    def _on_option_bar(self, leg: dict, c, ts: datetime, bar, exit_ts: datetime) -> list[Signal]:
        p = self.p
        if leg["open"]:
            if rules.premium_stop_hit(float(bar["high"]), leg["stop"]):
                return [self._stop(leg, c, ts, rules.premium_stop_fill(leg["stop"], bar["open"]), "bar")]
        elif leg["waiting"] and ts < exit_ts and (not leg["stopped_at"] or ts > _ts(leg["stopped_at"])) \
                and rules.premium_reentry_ok(float(bar["close"]), leg["first_entry"]):
            ref = float(bar["close"])
            leg.update(pid=leg["pid"] + "R", entry_ref=ref, stop=rules.premium_stop_level(ref, p.sl_pct),
                       open=True, waiting=False, reentered=True, is_reentry=True, entry_ts=_iso(ts))
            return [self._signal_entry(leg, c, ts, None, "re-entry at cost")]
        return []

    def _stop(self, leg: dict, c, ts: datetime, ref: float, how: str) -> Signal:
        leg.update(open=False, waiting=self.p.reentry and not leg["reentered"], stopped_at=_iso(ts))
        return Signal(self.name, Action.EXIT, leg["pid"], c, ts, None, f"stop: premium +{self.p.sl_pct:.0f}% ({how})",
                      ref_price=round(ref, 2), is_reentry=leg["is_reentry"],
                      stop={"basis": "premium", "level": round(leg["stop"], 2), "pct": self.p.sl_pct})

    def _signal_entry(self, leg: dict, c, ts: datetime, spot: float | None, reason: str) -> Signal:
        return Signal(self.name, Action.ENTRY, leg["pid"], c, ts, spot, reason, lots=self.p.lots,
                      ref_price=leg["entry_ref"], is_reentry=leg["is_reentry"],
                      stop={"basis": "premium", "level": round(leg["stop"], 2), "pct": self.p.sl_pct,
                            "ref": leg["entry_ref"]},
                      meta={"entry_time": self.s["entry_time"], "first_entry": leg["first_entry"]})


def walk_forward_chooser(seller, override: time | None = None) -> EntryTimeChooser:
    """Entry-time choice for expiry day d from backtest.ZeroDteStraddleSeller over DuckDB history.

    Refuses to trade (returns None) when any of the lookback expiry days has no
    underlying data, because an all-zero score would silently pick 09:20.
    """
    def choose(d: date):
        if override:
            return override, {"source": "ZERODTE_ENTRY_TIME override"}
        prior = seller.prior_expiries(d)
        missing = [str(x) for x in prior if seller.feed.spot_day(x) is None]
        if len(prior) < seller.params.lookback or missing:
            return None, {"reason": "not enough stored history for the walk-forward entry time",
                          "prior_expiries": [str(x) for x in prior], "missing_spot": missing,
                          "fix": "run scripts/daily_update.py (or set ZERODTE_ENTRY_TIME)"}
        best, scores = seller.choose_entry_time(prior)
        return best, {"source": "walk-forward", "prior_expiries": [str(x) for x in prior],
                      "training_pnl": round(scores[best], 2)}
    return choose


def params_from_config(cfg, lot_size: int) -> tuple[RangeBreakoutParams, ZeroDteParams]:
    """Backtest parameter objects with the configured overrides (defaults are the backtest's)."""
    pos = replace(RangeBreakoutParams(), itm_points=cfg.positional_itm_points, sl_pct=cfg.positional_sl_pct,
                  reentry=cfg.positional_reentry, lots=cfg.positional_lots, range_start=cfg.positional_range_start,
                  range_end=cfg.positional_range_end, last_entry=cfg.positional_last_entry,
                  exit_time=cfg.positional_exit_time, act_until=cfg.positional_act_until)
    zd = replace(ZeroDteParams(), itm_points=cfg.zerodte_itm_points, sl_pct=cfg.zerodte_sl_pct,
                 reentry=cfg.zerodte_reentry, lookback=cfg.zerodte_lookback, lots=cfg.zerodte_lots,
                 first_entry=cfg.zerodte_first_entry, last_entry=cfg.zerodte_last_entry,
                 step_minutes=cfg.zerodte_step_minutes, exit_time=cfg.zerodte_exit_time)
    return pos, zd
