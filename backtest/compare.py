"""Naked vs hedged comparison: one metrics row per strategy variant, plus markdown/CSV writers."""
from __future__ import annotations

import csv
from pathlib import Path

from .engine import Trade, metrics
from .hedged import combine_positions
from .margin import MarginModel, max_loss, pair_legs, peak_margin

COLUMNS = [
    ("strategy", "Strategy"), ("variant", "Variant"), ("trades", "Trades"), ("net_pnl", "Net P&L"),
    ("win_rate", "Win %"), ("max_drawdown", "Max DD"), ("avg_win", "Avg win"), ("avg_loss", "Avg loss"),
    ("profit_factor", "PF"), ("worst", "Largest loss"), ("gross_pnl", "Gross P&L"), ("costs", "Costs"),
    ("margin_est", "Peak margin (est.)"), ("capital_est", "Peak capital (est.)"),
    ("return_on_capital_pct", "Net / capital %"), ("max_loss_per_trade", "Max loss / trade (defined)"),
    ("avg_max_loss_per_trade", "Avg defined risk / trade"), ("unhedged_positions", "Unhedged (no wing data)"),
]


def summarize(strategy: str, variant: str, trades: list[Trade], model: MarginModel = MarginModel()) -> dict:
    m = metrics(combine_positions(trades))
    pairs = pair_legs(trades)
    peak = peak_margin(trades, model)
    losses = [max_loss(s, w) for s, w in pairs]
    defined = [x for x in losses if x is not None]
    naked = len(losses) - len(defined)
    return {
        "strategy": strategy, "variant": variant, "trades": m.trades, "net_pnl": m.net_pnl,
        "win_rate": m.win_rate, "max_drawdown": m.max_drawdown, "avg_win": m.avg_win, "avg_loss": m.avg_loss,
        "profit_factor": m.profit_factor, "worst": m.worst, "gross_pnl": m.gross_pnl, "costs": m.costs,
        "margin_est": peak["margin"], "capital_est": peak["capital"],
        "return_on_capital_pct": round(100 * m.net_pnl / peak["capital"], 1) if peak["capital"] else None,
        "max_loss_per_trade": max(defined) if defined and not naked else None,
        "avg_max_loss_per_trade": round(sum(defined) / len(defined), 2) if defined and not naked else None,
        "unhedged_positions": naked if variant != "naked" else 0,
        "stop_losses": m.stop_losses, "reentries": m.reentries,
    }


def _fmt(key: str, v) -> str:
    if v is None:
        return "unbounded" if key.startswith(("max_loss", "avg_max_loss")) else "–"
    if isinstance(v, float) and key not in ("win_rate", "profit_factor", "return_on_capital_pct"):
        return f"₹{v:,.0f}"
    return f"{v}"


def write_csv(path: str | Path, rows: list[dict]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def markdown_table(rows: list[dict]) -> str:
    """Transposed table: one column per variant, one row per metric."""
    heads = [f"{r['strategy']}<br>{r['variant']}" for r in rows]
    lines = ["| Metric | " + " | ".join(heads) + " |", "|---|" + "---:|" * len(rows)]
    for key, label in COLUMNS[2:]:
        lines.append(f"| {label} | " + " | ".join(_fmt(key, r[key]) for r in rows) + " |")
    return "\n".join(lines)
