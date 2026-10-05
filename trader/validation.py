"""Trade request parsing and price validation (all of it runs before any order is placed)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time


@dataclass
class TradeRequest:
    underlying: str
    expiry: date
    strike: float
    option_type: str               # CE | PE
    side: str                      # BUY | SELL
    lots: int
    entry_price: float
    stop_loss: float
    target: float | None = None
    product: str = "MIS"
    trail_enabled: bool = False
    trail_type: str = "POINTS"     # POINTS | PERCENT
    trail_value: float | None = None
    trail_step: float | None = None
    partial_enabled: bool = False
    partial_lots: int | None = None
    partial_price: float | None = None
    auto_exit_time: time | None = None

    @classmethod
    def from_json(cls, d: dict, default_product: str) -> "TradeRequest":
        def num(k, cast=float, required=True):
            v = d.get(k)
            if v in (None, ""):
                if required:
                    raise ValueError(f"{k} is required")
                return None
            try:
                return cast(v)
            except (TypeError, ValueError):
                raise ValueError(f"{k} must be a number, got {v!r}") from None

        def flag(k):
            return str(d.get(k, "")).lower() in ("1", "true", "on", "yes")

        at = (d.get("auto_exit_time") or "").strip()
        return cls(
            underlying=str(d.get("underlying", "")).upper(),
            expiry=date.fromisoformat(str(d.get("expiry"))),
            strike=num("strike"),
            option_type=str(d.get("option_type", "")).upper(),
            side=str(d.get("side", "")).upper(),
            lots=num("lots", int),
            entry_price=num("entry_price"),
            stop_loss=num("stop_loss"),
            target=num("target", required=False),
            product=str(d.get("product") or default_product).upper(),
            trail_enabled=flag("trail_enabled"),
            trail_type=str(d.get("trail_type") or "POINTS").upper(),
            trail_value=num("trail_value", required=False),
            trail_step=num("trail_step", required=False),
            partial_enabled=flag("partial_enabled"),
            partial_lots=num("partial_lots", int, required=False),
            partial_price=num("partial_price", required=False),
            auto_exit_time=time.fromisoformat(at) if at else None,
        )


def _on_tick(price: float, tick: float) -> bool:
    return abs(round(price / tick) * tick - price) < 1e-6


def validate(req: TradeRequest, lot_size: int, tick: float, now: datetime) -> list[str]:
    """Errors (empty = valid). BUY: SL < entry < target; SELL: target < entry < SL."""
    e: list[str] = []
    if req.option_type not in ("CE", "PE"):
        e.append("option type must be CE or PE")
    if req.side not in ("BUY", "SELL"):
        e.append("side must be BUY or SELL")
    if req.product not in ("MIS", "NRML"):
        e.append("product must be MIS or NRML")
    if req.lots < 1:
        e.append("lots must be at least 1")
    if lot_size < 1:
        e.append("lot size unknown")
    if req.entry_price <= 0:
        e.append("entry price must be > 0")
    if req.stop_loss <= 0:
        e.append("stop-loss must be > 0")
    if req.target is not None and req.target <= 0:
        e.append("target must be > 0")
    for name, v in (("entry price", req.entry_price), ("stop-loss", req.stop_loss), ("target", req.target),
                    ("partial price", req.partial_price if req.partial_enabled else None)):
        if v is not None and v > 0 and not _on_tick(v, tick):
            e.append(f"{name} {v} is not a multiple of the tick size {tick}")
    buy = req.side == "BUY"
    if buy:
        if req.stop_loss >= req.entry_price:
            e.append(f"BUY: stop-loss ₹{req.stop_loss:g} must be BELOW the entry ₹{req.entry_price:g} "
                     f"(you exit if the price falls)")
        if req.target is not None and req.target <= req.entry_price:
            e.append(f"BUY: target ₹{req.target:g} must be ABOVE the entry ₹{req.entry_price:g}")
    elif req.side == "SELL":
        if req.stop_loss <= req.entry_price:
            d = req.entry_price - req.stop_loss
            e.append(f"SELL: stop-loss ₹{req.stop_loss:g} must be ABOVE the entry ₹{req.entry_price:g} "
                     f"(a SELL loses when the price rises; {d:g} points of risk is a stop-loss of "
                     f"₹{req.entry_price + d:g})")
        if req.target is not None and req.target >= req.entry_price:
            e.append(f"SELL: target ₹{req.target:g} must be BELOW the entry ₹{req.entry_price:g}")
    if req.trail_enabled:
        if req.trail_type not in ("POINTS", "PERCENT"):
            e.append("trailing type must be POINTS or PERCENT")
        if not req.trail_value or req.trail_value <= 0:
            e.append("trailing amount must be > 0")
        elif req.trail_type == "PERCENT" and req.trail_value >= 100:
            e.append("trailing percentage must be < 100")
        if req.trail_step is not None and req.trail_step < 0:
            e.append("trailing step must be >= 0")
    if req.partial_enabled:
        if not req.partial_lots or req.partial_lots < 1:
            e.append("partial booking lots must be at least 1")
        elif req.partial_lots >= req.lots:
            e.append("partial booking lots must be fewer than the total lots (the rest stays managed)")
        if req.partial_price is None:
            e.append("partial booking price is required")
        else:
            if buy and req.partial_price <= req.entry_price:
                e.append("BUY: partial booking price must be above the entry price")
            if not buy and req.partial_price >= req.entry_price:
                e.append("SELL: partial booking price must be below the entry price")
            if req.target is not None and ((buy and req.partial_price > req.target)
                                           or (not buy and req.partial_price < req.target)):
                e.append("partial booking price must be between the entry price and the target")
    if req.auto_exit_time is not None and req.auto_exit_time <= now.time():
        e.append(f"auto-exit time {req.auto_exit_time:%H:%M} has already passed")
    if req.expiry < now.date():
        e.append("expiry is in the past")
    return e
