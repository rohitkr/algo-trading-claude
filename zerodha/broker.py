"""Broker interface used by the executor, with the live Kite and the paper implementations."""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Callable, Protocol

TERMINAL = {"COMPLETE", "REJECTED", "CANCELLED"}
PriceFn = Callable[[list[str]], dict[str, float]]


@dataclass(frozen=True)
class OrderRequest:
    tradingsymbol: str
    side: str                   # BUY | SELL
    quantity: int
    exchange: str = "NFO"
    product: str = "NRML"
    order_type: str = "LIMIT"
    price: float | None = None
    tag: str = ""

    def kite_params(self) -> dict:
        p = {"exchange": self.exchange, "tradingsymbol": self.tradingsymbol, "transaction_type": self.side,
             "quantity": self.quantity, "product": self.product, "order_type": self.order_type}
        if self.order_type == "LIMIT":
            p["price"] = self.price
        return p


@dataclass
class OrderStatus:
    order_id: str
    status: str                 # Kite status: OPEN, COMPLETE, REJECTED, CANCELLED, ...
    filled_quantity: int = 0
    average_price: float = 0.0
    message: str = ""

    @property
    def done(self) -> bool:
        return self.status in TERMINAL


class Broker(Protocol):
    def place_order(self, req: OrderRequest) -> str: ...
    def modify_order(self, order_id: str, *, price: float | None = None, quantity: int | None = None) -> str: ...
    def cancel_order(self, order_id: str) -> str: ...
    def order_status(self, order_id: str) -> OrderStatus: ...
    def ltp(self, keys: list[str]) -> dict[str, float]: ...
    def basket_margin(self, reqs: list[OrderRequest]) -> float: ...
    def available_margin(self) -> float: ...
    def positions(self) -> dict[str, int]: ...          # tradingsymbol -> net quantity (+long / -short)
    def open_orders(self) -> list[dict]: ...


class KiteBroker:
    """Thin adapter over a kiteconnect.KiteConnect instance (injected, so tests can pass a fake).

    `price_fn` replaces kite.ltp for limit pricing: the Kite Personal (free) plan has
    no market-quote API, so live trading passes prices from the market-data provider.
    """
    VARIETY = "regular"
    OPEN_STATUSES = {"OPEN", "TRIGGER PENDING", "PUT ORDER REQ RECEIVED", "VALIDATION PENDING",
                     "OPEN PENDING", "MODIFY PENDING", "MODIFY VALIDATION PENDING", "AMO REQ RECEIVED"}

    def __init__(self, kite, price_fn: PriceFn | None = None, exchange: str = "NFO"):
        self.kite = kite
        self.price_fn = price_fn
        self.exchange = exchange

    def place_order(self, req: OrderRequest) -> str:
        kw = req.kite_params()
        if req.tag:
            kw["tag"] = req.tag
        return str(self.kite.place_order(variety=self.VARIETY, **kw))

    def modify_order(self, order_id: str, *, price: float | None = None, quantity: int | None = None) -> str:
        kw = {k: v for k, v in (("price", price), ("quantity", quantity)) if v is not None}
        return str(self.kite.modify_order(variety=self.VARIETY, order_id=order_id, **kw))

    def cancel_order(self, order_id: str) -> str:
        return str(self.kite.cancel_order(variety=self.VARIETY, order_id=order_id))

    def order_status(self, order_id: str) -> OrderStatus:
        last = self.kite.order_history(order_id)[-1]
        return OrderStatus(order_id, last["status"], int(last.get("filled_quantity") or 0),
                           float(last.get("average_price") or 0.0), last.get("status_message") or "")

    def ltp(self, keys: list[str]) -> dict[str, float]:
        if self.price_fn is not None:
            return self.price_fn(keys)
        return {k: float(v["last_price"]) for k, v in self.kite.ltp(keys).items()}

    def positions(self) -> dict[str, int]:
        """Net positions on the exchange segment (day + carried), from kite.positions()["net"]."""
        out: dict[str, int] = {}
        for p in self.kite.positions().get("net", []):
            if p.get("exchange") == self.exchange:
                out[p["tradingsymbol"]] = out.get(p["tradingsymbol"], 0) + int(p.get("quantity") or 0)
        return out

    def open_orders(self) -> list[dict]:
        return [o for o in self.kite.orders() if o.get("status") in self.OPEN_STATUSES]

    def basket_margin(self, reqs: list[OrderRequest]) -> float:
        """Kite basket margin incl. spread benefit ('final' = after hedge offsets)."""
        orders = [dict(r.kite_params(), variety=self.VARIETY, price=r.price or 0, trigger_price=0) for r in reqs]
        res = self.kite.basket_order_margins(orders, consider_positions=True, mode="compact")
        return float(res["final"]["total"])

    def available_margin(self) -> float:
        return float(self.kite.margins("equity")["net"])


@dataclass
class PaperBroker:
    """In-memory broker for paper trading and tests.

    MARKET orders fill at the current price; LIMIT orders fill at the current
    price when marketable (BUY limit >= price, SELL limit <= price), otherwise
    stay OPEN until modified. `prices` maps "NFO:SYMBOL" -> price; `price_fn`
    overrides it. `margin_fn` estimates basket margin (default: 0).
    `slippage` (points per unit) makes fills worse by that much; `max_fill_qty`
    caps what one order can fill, to simulate partial fills.
    """
    funds: float = 1_000_000.0
    prices: dict[str, float] = field(default_factory=dict)
    price_fn: Callable[[str], float] | None = None
    margin_fn: Callable[[list[OrderRequest]], float] | None = None
    reject_symbols: set[str] = field(default_factory=set)
    slippage: float = 0.0
    max_fill_qty: int | None = None
    orders: dict[str, dict] = field(default_factory=dict)
    net: dict[str, int] = field(default_factory=dict)            # tradingsymbol -> net qty (+long / -short)
    cash: float = 0.0                                            # premium received - paid
    log: list[tuple] = field(default_factory=list)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    def _price(self, key: str) -> float:
        return float(self.price_fn(key)) if self.price_fn else float(self.prices[key])

    def _try_fill(self, oid: str) -> None:
        o = self.orders[oid]
        if o["status"] != "OPEN":
            return
        req: OrderRequest = o["req"]
        px = self._price(f"{req.exchange}:{req.tradingsymbol}")
        limit = o["price"]
        marketable = req.order_type == "MARKET" or (limit >= px if req.side == "BUY" else limit <= px)
        if not marketable or o["filled"]:
            return
        sign = 1 if req.side == "BUY" else -1
        px = round(px + sign * self.slippage, 2)
        qty = o["quantity"] if self.max_fill_qty is None else min(o["quantity"], self.max_fill_qty)
        self.net[req.tradingsymbol] = self.net.get(req.tradingsymbol, 0) + sign * qty
        self.cash -= sign * px * qty
        o.update(status="COMPLETE" if qty == o["quantity"] else "OPEN", filled=qty, avg=px)

    def place_order(self, req: OrderRequest) -> str:
        oid = f"P{next(self._ids)}"
        self.log.append(("place", oid, req.side, req.tradingsymbol, req.quantity, req.price))
        status = "REJECTED" if req.tradingsymbol in self.reject_symbols else "OPEN"
        self.orders[oid] = {"req": req, "status": status, "price": req.price, "quantity": req.quantity,
                            "filled": 0, "avg": 0.0, "msg": "rejected (paper)" if status == "REJECTED" else ""}
        self._try_fill(oid)
        return oid

    def modify_order(self, order_id: str, *, price: float | None = None, quantity: int | None = None) -> str:
        o = self.orders[order_id]
        self.log.append(("modify", order_id, price, quantity))
        if price is not None:
            o["price"] = price
        if quantity is not None:
            o["quantity"] = quantity
        self._try_fill(order_id)
        return order_id

    def cancel_order(self, order_id: str) -> str:
        o = self.orders[order_id]
        self.log.append(("cancel", order_id))
        if o["status"] == "OPEN":
            o["status"] = "CANCELLED"
        return order_id

    def order_status(self, order_id: str) -> OrderStatus:
        self._try_fill(order_id)
        o = self.orders[order_id]
        return OrderStatus(order_id, o["status"], o["filled"], o["avg"], o["msg"])

    def ltp(self, keys: list[str]) -> dict[str, float]:
        return {k: self._price(k) for k in keys}

    def basket_margin(self, reqs: list[OrderRequest]) -> float:
        return float(self.margin_fn(reqs)) if self.margin_fn else 0.0

    def available_margin(self) -> float:
        return self.funds

    def positions(self) -> dict[str, int]:
        return {k: v for k, v in self.net.items() if v}

    def open_orders(self) -> list[dict]:
        return [{"order_id": oid, "tradingsymbol": o["req"].tradingsymbol, "status": o["status"],
                 "quantity": o["quantity"], "filled_quantity": o["filled"]}
                for oid, o in self.orders.items() if o["status"] == "OPEN"]
