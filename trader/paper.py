"""Simulated exchange with the kiteconnect.KiteConnect method surface the trader uses.

PAPER mode wraps it in the same KiteTraderBroker LIVE uses, so every line of order handling,
idempotency, reconciliation and manual-exit detection runs unchanged. The tests use it as the fake Kite
(with failure injection: lost responses, rejections, partial fills, book lag, manual exits).

Matching (on every orders()/positions() call, against `price_fn(exchange, tradingsymbol)`):
  LIMIT   BUY fills when LTP <= limit, at min(limit, LTP); SELL when LTP >= limit, at max(limit, LTP)
  SL      "TRIGGER PENDING" until LTP crosses the trigger (BUY: LTP >= trigger, SELL: LTP <= trigger),
          then works as a LIMIT at its price (it may not fill in a gap, like the real thing)
  MARKET  fills at LTP
Fills are worse by `slippage` points per unit. `max_fill_qty` caps each match to simulate partial fills.

State (orders + net positions) is saved to `state_path` after every change, so a PAPER restart finds the
exchange as it left it. Edit that file (set a position's quantity to 0) to rehearse a manual exit in Kite.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

WORKING = {"OPEN", "TRIGGER PENDING"}


class PaperKiteError(Exception):
    """Stands in for kiteconnect's exceptions."""


class PaperExchange:
    def __init__(self, price_fn: Callable[[str, str], float | None], state_path: str | Path | None = None,
                 clock: Callable[[], datetime] = datetime.now, slippage: float = 0.0,
                 max_fill_qty: int | None = None, auto_match: bool = True, user_id: str = "PAPER"):
        self.price_fn, self.clock, self.slippage = price_fn, clock, slippage
        self.max_fill_qty, self.auto_match, self.user_id = max_fill_qty, auto_match, user_id
        self.state_path = Path(state_path) if state_path else None
        self._lock = threading.RLock()
        self.orders_: dict[str, dict] = {}
        self.net: dict[str, dict] = {}                 # "EXCH|SYMBOL|PRODUCT" -> {qty, buy_value, sell_value}
        self.seq = 0
        # failure injection (tests)
        self.reject_symbols: set[str] = set()
        self.raise_after_place: int = 0                # next N place_order calls create the order, then raise
        self.raise_before_place: int = 0               # next N place_order calls raise without creating it
        self.hide_new_orders: int = 0                  # next N orders are missing from orders() once (book lag)
        self.fail_reads: int = 0                       # next N orders()/positions() calls raise
        self.calls: list[tuple] = []
        self._load()

    # -- persistence -----------------------------------------------------------------------------
    def _load(self) -> None:
        if self.state_path and self.state_path.exists():
            d = json.loads(self.state_path.read_text())
            self.orders_, self.net, self.seq = d.get("orders", {}), d.get("net", {}), int(d.get("seq", 0))
            for p in self.net.values():                # hand-edited files may only carry qty
                p.setdefault("buy_value", 0.0)
                p.setdefault("sell_value", 0.0)

    def _save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"orders": self.orders_, "net": self.net, "seq": self.seq}, indent=1))
        os.replace(tmp, self.state_path)

    # -- kite surface --------------------------------------------------------------------------
    def place_order(self, variety: str, exchange: str, tradingsymbol: str, transaction_type: str, quantity: int,
                    product: str, order_type: str, validity: str = "DAY", price=None, trigger_price=None,
                    tag: str | None = None, **_):
        with self._lock:
            self.calls.append(("place", tradingsymbol, transaction_type, quantity, order_type, price, trigger_price, tag))
            if self.raise_before_place:
                self.raise_before_place -= 1
                raise PaperKiteError("simulated network error (order NOT created)")
            self.seq += 1
            oid = f"PPR{self.clock():%y%m%d}{self.seq:06d}"
            o = {"order_id": oid, "exchange": exchange, "tradingsymbol": tradingsymbol,
                 "transaction_type": transaction_type, "quantity": int(quantity), "product": product,
                 "order_type": order_type, "price": price, "trigger_price": trigger_price, "tag": tag or "",
                 "tags": [tag] if tag else [], "status": "OPEN", "filled_quantity": 0, "average_price": 0.0,
                 "status_message": "", "order_timestamp": self.clock().isoformat(timespec="seconds"),
                 "order_date": self.clock().date().isoformat(), "hidden": 0}
            reason = self._validate(o)
            if reason:
                o.update(status="REJECTED", status_message=reason)
            elif order_type == "SL":
                o["status"] = "TRIGGER PENDING"
            if self.hide_new_orders:
                self.hide_new_orders -= 1
                o["hidden"] = 1
            self.orders_[oid] = o
            if self.auto_match:
                self._match(o)
            self._save()
            if self.raise_after_place:
                self.raise_after_place -= 1
                raise PaperKiteError("simulated timeout (order WAS created)")
            return oid

    def modify_order(self, variety: str, order_id: str, quantity=None, price=None, trigger_price=None,
                     order_type=None, **_):
        with self._lock:
            self.calls.append(("modify", order_id, quantity, price, trigger_price, order_type))
            o = self.orders_.get(order_id)
            if o is None:
                raise PaperKiteError(f"order {order_id} not found")
            if o["status"] not in WORKING:
                raise PaperKiteError(f"order {order_id} is {o['status']}, cannot modify")
            if quantity is not None:
                if int(quantity) < o["filled_quantity"]:
                    raise PaperKiteError("quantity below filled quantity")
                o["quantity"] = int(quantity)
            if price is not None:
                o["price"] = price
            if trigger_price is not None:
                o["trigger_price"] = trigger_price
            if order_type is not None:
                o["order_type"] = order_type
                if order_type != "SL" and o["status"] == "TRIGGER PENDING":
                    o["status"] = "OPEN"
            if o["filled_quantity"] >= o["quantity"]:
                o["status"] = "COMPLETE"
            elif self.auto_match:
                self._match(o)
            self._save()
            return order_id

    def cancel_order(self, variety: str, order_id: str, **_):
        with self._lock:
            self.calls.append(("cancel", order_id))
            o = self.orders_.get(order_id)
            if o is None:
                raise PaperKiteError(f"order {order_id} not found")
            if o["status"] not in WORKING:
                raise PaperKiteError(f"order {order_id} is {o['status']}, cannot cancel")
            o["status"] = "CANCELLED"
            self._save()
            return order_id

    def orders(self):
        with self._lock:
            self._read_fault()
            today = self.clock().date().isoformat()
            if self.auto_match:
                self.match_all()
            out = []
            for o in self.orders_.values():
                if o["order_date"] != today:           # Kite's order book only has today's orders
                    continue
                if o.get("hidden"):
                    o["hidden"] = 0                    # visible from the next call on
                    continue
                out.append({k: v for k, v in o.items() if k not in ("hidden", "order_date")})
            return out

    def positions(self):
        with self._lock:
            self._read_fault()
            if self.auto_match:
                self.match_all()
            net = []
            for key, p in self.net.items():
                exch, sym, prod = key.split("|")
                net.append({"exchange": exch, "tradingsymbol": sym, "product": prod, "quantity": p["qty"],
                            "buy_value": p["buy_value"], "sell_value": p["sell_value"]})
            return {"net": net, "day": net}

    def profile(self):
        return {"user_id": self.user_id, "user_name": "Paper exchange"}

    # -- simulation ----------------------------------------------------------------------------
    def _read_fault(self) -> None:
        if self.fail_reads:
            self.fail_reads -= 1
            raise PaperKiteError("simulated network error on read")

    def _validate(self, o: dict) -> str:
        if o["tradingsymbol"] in self.reject_symbols:
            return "RMS: rejected (paper)"
        if o["quantity"] <= 0:
            return "invalid quantity"
        if o["order_type"] in ("LIMIT", "SL") and not o["price"]:
            return "price required"
        if o["order_type"] == "SL":
            ltp = self._ltp(o)
            trig = float(o["trigger_price"] or 0)
            if ltp is not None and ((o["transaction_type"] == "BUY" and trig < ltp)
                                    or (o["transaction_type"] == "SELL" and trig > ltp)):
                return "Trigger price for stoploss orders should be beyond the last traded price"
        return ""

    def _ltp(self, o: dict) -> float | None:
        try:
            return self.price_fn(o["exchange"], o["tradingsymbol"])
        except Exception:
            return None

    def match_all(self) -> None:
        with self._lock:
            changed = False
            for o in list(self.orders_.values()):
                if o["status"] in WORKING:
                    changed |= self._match(o)
            if changed:
                self._save()

    def _match(self, o: dict) -> bool:
        ltp = self._ltp(o)
        if ltp is None or o["status"] not in WORKING:
            return False
        buy = o["transaction_type"] == "BUY"
        if o["status"] == "TRIGGER PENDING":
            trig = float(o["trigger_price"])
            if (buy and ltp >= trig) or (not buy and ltp <= trig):
                o["status"] = "OPEN"
            else:
                return False
        if o["order_type"] == "MARKET":
            px = ltp
        else:
            lim = float(o["price"])
            if (buy and ltp > lim) or (not buy and ltp < lim):
                return False
            px = min(lim, ltp) if buy else max(lim, ltp)
        return self.fill(o["order_id"], None, px + (self.slippage if buy else -self.slippage)) > 0

    def fill(self, order_id: str, qty: int | None = None, price: float | None = None) -> int:
        """Execute (part of) a working order. Tests call it directly with auto_match=False."""
        with self._lock:
            o = self.orders_[order_id]
            if o["status"] not in WORKING:
                return 0
            left = o["quantity"] - o["filled_quantity"]
            q = left if qty is None else min(qty, left)
            if self.max_fill_qty is not None:
                q = min(q, self.max_fill_qty)
            if q <= 0:
                return 0
            px = float(price if price is not None else (o["price"] or self._ltp(o)))
            px = round(px, 2)
            filled = o["filled_quantity"]
            o["average_price"] = round((o["average_price"] * filled + px * q) / (filled + q), 4)
            o["filled_quantity"] = filled + q
            if o["filled_quantity"] >= o["quantity"]:
                o["status"] = "COMPLETE"
            elif o["status"] == "TRIGGER PENDING":
                o["status"] = "OPEN"
            key = f"{o['exchange']}|{o['tradingsymbol']}|{o['product']}"
            p = self.net.setdefault(key, {"qty": 0, "buy_value": 0.0, "sell_value": 0.0})
            if o["transaction_type"] == "BUY":
                p["qty"] += q
                p["buy_value"] += px * q
            else:
                p["qty"] -= q
                p["sell_value"] += px * q
            self._save()
            return q

    def reject(self, order_id: str, message: str = "rejected (paper)") -> None:
        with self._lock:
            self.orders_[order_id].update(status="REJECTED", status_message=message)
            self._save()

    def set_position(self, exchange: str, tradingsymbol: str, product: str, qty: int) -> None:
        """What a manual exit / manual trade in the Kite app does to the account."""
        with self._lock:
            p = self.net.setdefault(f"{exchange}|{tradingsymbol}|{product}",
                                    {"qty": 0, "buy_value": 0.0, "sell_value": 0.0})
            p["qty"] = int(qty)
            self._save()

    def manual_order(self, exchange: str, tradingsymbol: str, product: str, side: str, qty: int,
                     price: float) -> str:
        """An untagged order placed by hand in Kite, filled at once."""
        with self._lock:
            auto, self.auto_match = self.auto_match, False
            try:
                oid = self.place_order("regular", exchange, tradingsymbol, side, qty, product, "LIMIT", price=price)
            finally:
                self.auto_match = auto
            self.fill(oid, qty, price)
            return oid

    def working(self, tradingsymbol: str | None = None) -> list[dict]:
        return [o for o in self.orders_.values() if o["status"] in WORKING
                and (tradingsymbol is None or o["tradingsymbol"] == tradingsymbol)]
