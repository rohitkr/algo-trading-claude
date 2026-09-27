"""Order management: freeze-quantity slicing, marketable-limit pricing, fill waiting and re-pricing."""
from __future__ import annotations

import logging
import math
import time as _time
from dataclasses import dataclass, field
from typing import Callable

from .broker import Broker, OrderRequest, OrderStatus
from .config import ZerodhaConfig
from .instruments import Instrument

log = logging.getLogger("zerodha.orders")


class OrderFailed(RuntimeError):
    def __init__(self, msg: str, fills: list["Fill"]):
        super().__init__(msg)
        self.fills = fills


@dataclass
class Fill:
    order_id: str
    tradingsymbol: str
    side: str
    quantity: int
    average_price: float
    status: str


def round_to_tick(price: float, tick: float, side: str) -> float:
    """BUY rounds up, SELL rounds down, so the limit stays marketable."""
    steps = price / tick
    steps = math.ceil(steps - 1e-9) if side == "BUY" else math.floor(steps + 1e-9)
    return round(max(steps, 1) * tick, 2)


def slice_quantity(qty: int, lot: int, freeze: int) -> list[int]:
    if qty % lot:
        raise ValueError(f"quantity {qty} is not a multiple of lot size {lot}")
    per = max(lot, freeze // lot * lot)
    out = [per] * (qty // per)
    if qty % per:
        out.append(qty % per)
    return out


@dataclass
class OrderManager:
    broker: Broker
    cfg: ZerodhaConfig
    sleep: Callable[[float], None] = _time.sleep
    clock: Callable[[], float] = _time.monotonic
    placed: list[Fill] = field(default_factory=list)

    def limit_price(self, inst: Instrument, side: str) -> float | None:
        if self.cfg.order_type == "MARKET":
            return None
        ltp = self.broker.ltp([inst.key])[inst.key]
        k = 1 + self.cfg.limit_buffer_pct / 100 if side == "BUY" else 1 - self.cfg.limit_buffer_pct / 100
        return round_to_tick(ltp * k, inst.tick_size, side)

    def execute(self, inst: Instrument, side: str, quantity: int) -> list[Fill]:
        """Buy/sell `quantity` units, sliced at the freeze limit; every slice must fill completely."""
        fills: list[Fill] = []
        for q in slice_quantity(quantity, inst.lot_size, self.cfg.freeze_qty):
            try:
                fills.append(self._one(inst, side, q))
            except OrderFailed as exc:
                raise OrderFailed(str(exc), fills + exc.fills) from None
        return fills

    def _one(self, inst: Instrument, side: str, qty: int) -> Fill:
        req = OrderRequest(inst.tradingsymbol, side, qty, inst.exchange, self.cfg.product, self.cfg.order_type,
                           self.limit_price(inst, side), self.cfg.tag)
        oid = self.broker.place_order(req)
        log.info("placed %s %s x%d @ %s -> %s", side, inst.tradingsymbol, qty, req.price or "MKT", oid)
        for attempt in range(self.cfg.max_reprices + 1):
            st = self._wait(oid)
            if st.status == "COMPLETE":
                return self._record(Fill(oid, inst.tradingsymbol, side, qty, st.average_price, st.status))
            if st.done:
                partial = [Fill(oid, inst.tradingsymbol, side, st.filled_quantity, st.average_price, st.status)] \
                    if st.filled_quantity else []
                raise OrderFailed(f"{side} {inst.tradingsymbol} {st.status}: {st.message}", partial)
            if attempt < self.cfg.max_reprices and self.cfg.order_type == "LIMIT":
                price = self.limit_price(inst, side)
                log.info("re-pricing %s to %s (attempt %d)", oid, price, attempt + 1)
                self.broker.modify_order(oid, price=price)
        self.broker.cancel_order(oid)
        st = self.broker.order_status(oid)
        partial = [Fill(oid, inst.tradingsymbol, side, st.filled_quantity, st.average_price, "CANCELLED")] \
            if st.filled_quantity else []
        raise OrderFailed(f"{side} {inst.tradingsymbol} not filled after {self.cfg.max_reprices} re-prices", partial)

    def _wait(self, oid: str) -> OrderStatus:
        deadline = self.clock() + self.cfg.fill_timeout_s
        while True:
            st = self.broker.order_status(oid)
            if st.done or self.clock() >= deadline:
                return st
            self.sleep(self.cfg.poll_interval_s)

    def _record(self, f: Fill) -> Fill:
        self.placed.append(f)
        return f
