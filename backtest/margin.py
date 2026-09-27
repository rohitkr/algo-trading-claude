"""Rough SPAN + exposure margin estimates for short index options (estimates only).

This is NOT NSE SPAN. It is a transparent approximation for comparing naked
and hedged books; the live broker adapter (zerodha/margin.py) asks Kite's
basket-margin API for real numbers before placing orders.

  SPAN (naked short)   ~ span_pct of notional (spot x qty) per sold leg
  SPAN (hedged spread) ~ min(wing width x qty, naked SPAN) - a +/-span_pct move
                          is far wider than any 200-300 pt wing, so the scenario
                          loss is capped at the width
  Calls and puts offset: the book's SPAN is the larger of the call side and the
  put side (a straddle cannot lose on both sides at once).
  Exposure             ~ exposure_pct of notional on every sold leg, hedged or not
  Wing premium         paid in full for every bought leg (capital, not margin)
"""
from __future__ import annotations

from dataclasses import dataclass

from .engine import Trade
from .hedged import position_id


@dataclass(frozen=True)
class MarginModel:
    span_pct: float = 9.0        # approx. NIFTY option scan range
    exposure_pct: float = 2.0    # NSE exposure margin on short index options

    def book(self, positions: list[tuple[Trade, Trade | None]]) -> dict:
        """Margin for sold legs (with optional wing) that are open at the same time."""
        span = {"CALL": 0.0, "PUT": 0.0}
        exposure = premium = 0.0
        for short, wing in positions:
            spot = short.spot_entry or short.strike
            notional = spot * short.qty
            naked_span = self.span_pct / 100 * notional
            if wing is None:
                leg_span = naked_span
            else:
                leg_span = min(abs(wing.strike - short.strike) * short.qty, naked_span)
                premium += wing.entry_price * wing.qty
            span[short.right] += leg_span
            exposure += self.exposure_pct / 100 * notional
        margin = max(span.values()) + exposure
        return {"span": round(max(span.values()), 2), "exposure": round(exposure, 2),
                "margin": round(margin, 2), "wing_premium": round(premium, 2),
                "capital": round(margin + premium, 2)}


def pair_legs(trades: list[Trade]) -> list[tuple[Trade, Trade | None]]:
    """(sold leg, its wing or None) for every position."""
    by_pos: dict[str, dict] = {}
    for t in trades:
        slot = by_pos.setdefault(position_id(t), {"short": None, "wing": None})
        slot["short" if t.side == "SELL" else "wing"] = t
    return [(v["short"], v["wing"]) for v in by_pos.values() if v["short"] is not None]


def peak_margin(trades: list[Trade], model: MarginModel = MarginModel()) -> dict:
    """Largest estimated margin/capital over time, checking the open book at every entry."""
    pairs = pair_legs(trades)
    best = {"span": 0.0, "exposure": 0.0, "margin": 0.0, "wing_premium": 0.0, "capital": 0.0}
    for short, _ in pairs:
        t = short.entry_ts
        open_now = [(s, w) for s, w in pairs if s.entry_ts <= t and (s.exit_ts is None or s.exit_ts > t)]
        b = model.book(open_now)
        if b["capital"] > best["capital"]:
            best = b
    return best


def max_loss(short: Trade, wing: Trade | None) -> float | None:
    """Defined maximum loss of one position (incl. its actual costs); None when naked (unbounded)."""
    if wing is None:
        return None
    width = abs(wing.strike - short.strike)
    net_credit = short.entry_price - wing.entry_price
    return round((width - net_credit) * short.qty + short.costs + wing.costs, 2)
