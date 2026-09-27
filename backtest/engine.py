"""Trade records, costs and performance metrics shared by all strategies."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime

import pandas as pd


@dataclass(frozen=True)
class CostModel:
    """Indian index-option charges for one round trip of one leg (sell then buy back), plus slippage."""
    slippage_points: float = 0.5          # per side, per unit
    brokerage_per_order: float = 20.0
    stt_sell_pct: float = 0.1             # on sell-side premium turnover
    exchange_txn_pct: float = 0.03503     # NSE F&O options, on premium turnover
    sebi_per_crore: float = 10.0
    stamp_buy_pct: float = 0.003          # on buy-side turnover
    gst_pct: float = 18.0                 # on brokerage + exchange + SEBI fees

    def cost(self, qty: int, sell_price: float, buy_price: float) -> float:
        sell_turn, buy_turn = sell_price * qty, buy_price * qty
        turnover = sell_turn + buy_turn
        brokerage = 2 * self.brokerage_per_order
        exchange = turnover * self.exchange_txn_pct / 100
        sebi = turnover * self.sebi_per_crore / 1e7
        charges = (brokerage + sell_turn * self.stt_sell_pct / 100 + exchange + sebi
                   + buy_turn * self.stamp_buy_pct / 100 + (brokerage + exchange + sebi) * self.gst_pct / 100)
        return 2 * self.slippage_points * qty + charges


@dataclass
class Trade:
    strategy: str
    trade_id: str
    day: str
    expiry: str
    strike: float
    right: str                   # CALL | PUT
    side: str                    # SELL (all current strategies sell premium)
    qty: int
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime | None = None
    exit_price: float | None = None
    exit_reason: str = ""
    spot_entry: float | None = None
    spot_exit: float | None = None
    is_reentry: bool = False
    note: str = ""
    gross_pnl: float = 0.0
    costs: float = 0.0
    net_pnl: float = 0.0

    def close(self, ts: datetime, price: float, reason: str, costs: CostModel, spot: float | None = None) -> "Trade":
        self.exit_ts, self.exit_price, self.exit_reason, self.spot_exit = ts, price, reason, spot
        sign = 1 if self.side == "SELL" else -1
        self.gross_pnl = round(sign * (self.entry_price - price) * self.qty, 2)
        sell, buy = (self.entry_price, price) if self.side == "SELL" else (price, self.entry_price)
        self.costs = round(costs.cost(self.qty, sell, buy), 2)
        self.net_pnl = round(self.gross_pnl - self.costs, 2)
        return self


@dataclass
class Metrics:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    gross_pnl: float = 0.0
    costs: float = 0.0
    net_pnl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float | None = None
    max_drawdown: float = 0.0
    best: float = 0.0
    worst: float = 0.0
    reentries: int = 0
    stop_losses: int = 0


def trades_frame(trades: list[Trade]) -> pd.DataFrame:
    return pd.DataFrame([asdict(t) for t in trades])


def metrics(trades: list[Trade]) -> Metrics:
    closed = [t for t in trades if t.exit_ts is not None]
    m = Metrics(trades=len(closed))
    if not closed:
        return m
    pnl = [t.net_pnl for t in sorted(closed, key=lambda t: t.exit_ts)]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p <= 0]
    m.wins, m.losses = len(wins), len(losses)
    m.win_rate = round(100 * len(wins) / len(pnl), 1)
    m.gross_pnl = round(sum(t.gross_pnl for t in closed), 2)
    m.costs = round(sum(t.costs for t in closed), 2)
    m.net_pnl = round(sum(pnl), 2)
    m.avg_win = round(sum(wins) / len(wins), 2) if wins else 0.0
    m.avg_loss = round(sum(losses) / len(losses), 2) if losses else 0.0
    m.profit_factor = round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None
    equity = pd.Series(pnl).cumsum()
    m.max_drawdown = round(float((equity - equity.cummax().clip(lower=0)).min()), 2)
    m.best, m.worst = max(pnl), min(pnl)
    m.reentries = sum(t.is_reentry for t in closed)
    m.stop_losses = sum(t.exit_reason.startswith("stop") for t in closed)
    return m
