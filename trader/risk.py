"""Pre-trade risk checks (server-side, at preview AND again at confirm, right before the entry order).

Every check is returned (passed or failed) and written to the audit trail. Exits are never blocked.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from . import lifecycle as L
from .config import TraderConfig


@dataclass
class RiskCheck:
    name: str
    passed: bool
    detail: str = ""


def daily_pnl(trades_today_closed: list[dict], open_trades: list[dict]) -> float:
    """Realised P&L of trades closed today + realised and unrealised P&L of open trades."""
    closed = sum(float(t["realized_pnl"] or 0) for t in trades_today_closed)
    open_ = sum(float(t["realized_pnl"] or 0) + float(t["unrealized_pnl"] or 0) for t in open_trades)
    return round(closed + open_, 2)


def open_positions(trades: list[dict], exclude_group: int | None = None) -> set[tuple[str, int]]:
    """Distinct open positions: every multi-leg strategy is one, every standalone trade is one."""
    out: set[tuple[str, int]] = set()
    for t in trades:
        if t["status"] not in L.OPEN_STATUSES:
            continue
        gid = t.get("group_id")
        if gid is not None and gid == exclude_group:
            continue
        out.add(("strategy", gid) if gid is not None else ("trade", t["id"]))
    return out


def pre_trade(cfg: TraderConfig, *, now: datetime, tradingsymbol: str, exchange: str, underlying: str, side: str,
              lots: int, quantity: int, entry: float, stop: float, ltp: float | None, open_trades: list[dict],
              trades_today: int, day_pnl: float, broker_net: int | None, halted: str | None,
              group_id: int | None = None) -> list[RiskCheck]:
    """Checks for opening one trade. A multi-leg strategy counts as ONE position toward max_open_trades and
    max_trades_per_day, however many legs it has: pass the strategy's group_id for a leg, and `trades_today`
    already excluding that strategy (Repository.confirmed_on(exclude_group=...)). Its other legs are then not
    counted against it, so an iron condor never blocks its own SELL legs."""
    c: list[RiskCheck] = []
    add = lambda name, ok, detail="": c.append(RiskCheck(name, bool(ok), detail))  # noqa: E731
    add("not_halted", not halted, halted or "")
    t = now.time()
    add("weekday", now.weekday() < 5, f"{now:%A}")
    start, end, square_off = cfg.session_for(underlying)       # MCX has its own evening session
    if start:
        add("trading_start", t >= start, f"now {t:%H:%M}, start {start:%H:%M}")
    if end:
        add("trading_end", t <= end, f"now {t:%H:%M}, end {end:%H:%M}")
    if square_off:
        add("before_square_off", t < square_off, f"square-off {square_off:%H:%M}")
    n_open = len(open_positions(open_trades, exclude_group=group_id))
    add("max_open_trades", n_open < cfg.max_open_trades,
        f"{n_open} open (a multi-leg strategy counts as one), limit {cfg.max_open_trades}")
    add("max_trades_per_day", trades_today < cfg.max_trades_per_day,
        f"{trades_today} today, limit {cfg.max_trades_per_day}")
    add("max_lots_per_trade", lots <= cfg.max_lots_per_trade, f"{lots} lots, limit {cfg.max_lots_per_trade}")
    add("max_qty_per_trade", quantity <= cfg.max_qty_per_trade, f"{quantity} units, limit {cfg.max_qty_per_trade}")
    freeze = cfg.freeze_for(underlying)
    add("freeze_qty", quantity <= freeze, f"{quantity} units, {underlying} freeze limit {freeze} (no slicing)")
    value = entry * quantity
    if cfg.max_order_value:
        add("max_order_value", value <= cfg.max_order_value, f"₹{value:,.0f}, limit ₹{cfg.max_order_value:,.0f}")
    risk = abs(entry - stop) * quantity
    add("max_loss_per_trade", risk <= cfg.max_loss_per_trade,
        f"₹{risk:,.0f} at the stop, limit ₹{cfg.max_loss_per_trade:,.0f}")
    if cfg.max_daily_loss:
        add("max_daily_loss", day_pnl > -cfg.max_daily_loss, f"today ₹{day_pnl:,.0f}, limit -₹{cfg.max_daily_loss:,.0f}")
    if cfg.max_daily_profit:
        add("max_daily_profit", day_pnl < cfg.max_daily_profit,
            f"today ₹{day_pnl:,.0f}, cap ₹{cfg.max_daily_profit:,.0f}")
    mine = [x["id"] for x in open_trades if x["tradingsymbol"] == tradingsymbol and x["status"] in L.OPEN_STATUSES]
    add("one_trade_per_symbol", not mine, f"trade(s) {mine} already hold/work {tradingsymbol}" if mine else "")
    if broker_net is None:
        add("broker_position_known", False, "could not read Zerodha positions")
    else:
        add("no_outside_position", broker_net == 0 or bool(mine),
            f"Zerodha already shows {broker_net} of {tradingsymbol} not opened here" if broker_net and not mine else "")
    if ltp is None:
        add("ltp_available", not cfg.require_ltp_for_entry, "no live price for this contract")
    elif cfg.max_entry_deviation_pct:
        dev = abs(entry - ltp) / ltp * 100
        add("entry_near_ltp", dev <= cfg.max_entry_deviation_pct,
            f"entry {entry} is {dev:.1f}% from LTP {ltp}, limit {cfg.max_entry_deviation_pct}%")
    if ltp is not None:
        wrong_side = (side == "BUY" and ltp <= stop) or (side == "SELL" and ltp >= stop)
        add("ltp_not_through_stop", not wrong_side,
            f"LTP {ltp} is already through the stop {stop}" if wrong_side else f"LTP {ltp}, stop {stop}")
    return c
