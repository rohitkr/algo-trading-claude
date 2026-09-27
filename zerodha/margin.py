"""Pre-trade margin check via the broker's basket-margin API, plus an offline estimate for paper mode."""
from __future__ import annotations

from dataclasses import dataclass

from .broker import Broker, OrderRequest
from .instruments import InstrumentBook


@dataclass(frozen=True)
class MarginCheck:
    required: float
    available: float
    buffer_pct: float

    @property
    def needed(self) -> float:
        return self.required * (1 + self.buffer_pct / 100)

    @property
    def ok(self) -> bool:
        return self.available >= self.needed


def check_margin(broker: Broker, reqs: list[OrderRequest], buffer_pct: float) -> MarginCheck:
    """Kite basket margin for all legs together (so the hedge benefit is applied) vs. available funds."""
    return MarginCheck(broker.basket_margin(reqs), broker.available_margin(), buffer_pct)


def estimate_basket_margin(reqs: list[OrderRequest], book: InstrumentBook, spot: float,
                           span_pct: float = 9.0, exposure_pct: float = 2.0) -> float:
    """Rough SPAN + exposure for a basket (paper mode only; NOT NSE SPAN).

    Each SELL leg: SPAN ~ span_pct of notional, capped at the distance to a BUY
    leg of the same right/expiry/quantity-or-more further OTM (a wing); calls and
    puts offset (larger side charged). Exposure exposure_pct of notional on
    every SELL leg. BUY legs add their premium (price x qty).
    """
    legs = [(book.by_symbol(r.tradingsymbol), r) for r in reqs]
    span = {"CALL": 0.0, "PUT": 0.0}
    exposure = premium = 0.0
    for inst, r in legs:
        if r.side == "BUY":
            premium += (r.price or 0.0) * r.quantity
            continue
        notional = spot * r.quantity
        wings = [i.strike for i, w in legs if w.side == "BUY" and i.right == inst.right and i.expiry == inst.expiry
                 and w.quantity >= r.quantity
                 and (i.strike < inst.strike if inst.right == "PUT" else i.strike > inst.strike)]
        naked = span_pct / 100 * notional
        leg_span = min(naked, min(abs(inst.strike - w) for w in wings) * r.quantity) if wings else naked
        span[inst.right] += leg_span
        exposure += exposure_pct / 100 * notional
    return round(max(span.values()) + exposure + premium, 2)
