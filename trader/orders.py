"""Idempotent order placement: intent row first, unique Kite tag, never re-place blindly.

    1. insert an `orders` row (status INTENT) with a unique tag and COMMIT      <- crash here: nothing sent,
    2. kite.place_order(..., tag=tag)                                             the tag is never found,
    3a. response: store the broker order id (SUBMITTED)                           row becomes NOT_PLACED
    3b. exception / timeout: status UNCERTAIN (the order may or may not exist)

INTENT and UNCERTAIN rows are resolved ONLY by finding the tag in Zerodha's order book. An order that is
still missing after TRADER_ORDER_LOOKUP_GRACE_SECONDS becomes NOT_PLACED; only then may the service create
a NEW row (new tag) for the same purpose. While any order of a kind is unresolved or working, placing
another of that kind for the trade is refused (DUPLICATE_PREVENTED).
"""
from __future__ import annotations

import logging
from datetime import datetime

from .broker import LOCAL_PENDING, KiteTraderBroker, OrderSpec, Snapshot, is_terminal, is_working
from .repository import Repository

log = logging.getLogger("trader.orders")


class OrderPlacer:
    def __init__(self, repo: Repository, broker: KiteTraderBroker, audit, grace_s: float, clock):
        self.repo, self.broker, self.audit, self.grace_s, self.clock = repo, broker, audit, grace_s, clock

    def working(self, trade_id: int, kind: str | None = None) -> list[dict]:
        return [o for o in self.repo.orders(trade_id, kind) if is_working(o["status"])]

    def place(self, trade: dict, kind: str, side: str, qty: int, order_type: str, price: float | None,
              trigger: float | None = None, purpose: str | None = None) -> dict | None:
        """Returns the order row (SUBMITTED or UNCERTAIN), or None when refused as a duplicate."""
        dup = self.working(trade["id"], kind)
        if dup:
            self.audit(trade["id"], "DUPLICATE_PREVENTED", "WARNING",
                       {"kind": kind, "existing": [(o["tag"], o["broker_order_id"], o["status"]) for o in dup]})
            return None
        tag = self.repo.new_tag(trade["id"], kind)
        row = self.repo.insert_order({
            "trade_id": trade["id"], "tag": tag, "kind": kind, "purpose": purpose, "exchange": trade["exchange"],
            "tradingsymbol": trade["tradingsymbol"], "product": trade["product"], "side": side,
            "order_type": order_type, "quantity": int(qty), "price": price, "trigger_price": trigger,
            "status": "INTENT"})
        spec = OrderSpec(trade["exchange"], trade["tradingsymbol"], side, int(qty), trade["product"], order_type,
                         price, trigger, tag)
        self.audit(trade["id"], f"{kind}_ORDER_SUBMITTING", "INFO", {"tag": tag, **spec.kite_params()})
        try:
            oid = self.broker.place(spec)
        except Exception as exc:
            self.repo.update_order(row["id"], status="UNCERTAIN", status_message=f"{type(exc).__name__}: {exc}",
                                   placed_at=self.repo.now())
            self.audit(trade["id"], "ORDER_UNCERTAIN", "WARNING",
                       {"kind": kind, "tag": tag, "error": f"{type(exc).__name__}: {exc}",
                        "action": "will look the tag up in the order book before anything is re-placed"})
            return self.repo.order(row["id"])
        self.repo.update_order(row["id"], broker_order_id=oid, status="SUBMITTED", placed_at=self.repo.now(),
                               last_priced_at=self.repo.now())
        self.audit(trade["id"], f"{kind}_ORDER_PLACED", "INFO", {"tag": tag, "order_id": oid})
        return self.repo.order(row["id"])

    def modify(self, order: dict, **kw) -> bool:
        if not order.get("broker_order_id") or is_terminal(order["status"]) or order["status"] in LOCAL_PENDING:
            return False
        try:
            self.broker.modify(order["broker_order_id"], **kw)
        except Exception as exc:
            self.audit(order["trade_id"], "ORDER_MODIFY_FAILED", "WARNING",
                       {"order_id": order["broker_order_id"], "kind": order["kind"], "changes": kw,
                        "error": f"{type(exc).__name__}: {exc}"})
            return False
        upd = {"modifications": order["modifications"] + 1}
        if "quantity" in kw:
            upd["quantity"] = kw["quantity"]
        if "price" in kw:
            upd["price"] = kw["price"]
            upd["last_priced_at"] = self.repo.now()
        if "trigger_price" in kw:
            upd["trigger_price"] = kw["trigger_price"]
        if "order_type" in kw:
            upd["order_type"] = kw["order_type"]
        self.repo.update_order(order["id"], **upd)
        self.audit(order["trade_id"], "ORDER_MODIFIED", "INFO",
                   {"order_id": order["broker_order_id"], "kind": order["kind"], "changes": kw})
        return True

    def cancel(self, order: dict, why: str) -> bool:
        if not order.get("broker_order_id") or is_terminal(order["status"]):
            return False
        self.repo.update_order(order["id"], cancel_requested=1)      # before the call: a cancel we asked for
        try:
            self.broker.cancel(order["broker_order_id"])
        except Exception as exc:
            self.audit(order["trade_id"], "ORDER_CANCEL_FAILED", "WARNING",
                       {"order_id": order["broker_order_id"], "kind": order["kind"], "why": why,
                        "error": f"{type(exc).__name__}: {exc}"})
            return False
        self.audit(order["trade_id"], "ORDER_CANCEL_REQUESTED", "INFO",
                   {"order_id": order["broker_order_id"], "kind": order["kind"], "why": why})
        return True

    # -- sync with the broker's order book -----------------------------------------------------
    def sync(self, snap: Snapshot) -> list[dict]:
        """Resolve INTENT/UNCERTAIN rows by tag, then copy status/fills of every non-final order.
        Returns the order rows that changed."""
        changed = []
        now = snap.fetched_at
        today = now.date().isoformat()
        for o in self.repo.orders(statuses=None):
            if is_terminal(o["status"]):                  # COMPLETE / CANCELLED / REJECTED are final at Kite
                continue
            if o["status"] in LOCAL_PENDING and not o["broker_order_id"]:
                changed += self._resolve(o, snap, now)
                continue
            b = snap.by_id.get(o["broker_order_id"] or "")
            if b is None:
                if o["order_date"] < today:
                    # A DAY order from an earlier session: Kite expired it at the close.
                    self.repo.update_order(o["id"], status="EXPIRED", status_message="DAY order from an earlier session")
                    changed.append(self.repo.order(o["id"]))
                continue          # not in the book yet (lag): keep what we know
            terms = {k: v for k, v in (("quantity", b.quantity), ("price", b.price), ("trigger_price", b.trigger_price))
                     if v not in (None, 0) and (o[k] is None or abs(float(o[k]) - float(v)) > 1e-9)}
            if terms:       # modified at the broker (our uncertain modify, or by hand in Kite): adopt it
                self.repo.update_order(o["id"], **terms)
                self.audit(o["trade_id"], "ORDER_TERMS_FROM_BROKER", "WARNING",
                           {"order_id": b.order_id, "kind": o["kind"],
                            "changes": {k: [o[k], v] for k, v in terms.items()}})
                o = {**o, **terms}
            if (b.status, b.filled_qty, round(b.avg_price, 4)) != (o["status"], o["filled_qty"], round(o["avg_price"] or 0, 4)):
                self.repo.update_order(o["id"], status=b.status, filled_qty=b.filled_qty, avg_price=b.avg_price,
                                       status_message=b.message or o["status_message"])
                self.audit(o["trade_id"], "ORDER_STATUS", "WARNING" if b.status == "REJECTED" else "INFO",
                           {"order_id": b.order_id, "kind": o["kind"], "status": b.status, "filled": b.filled_qty,
                            "avg_price": b.avg_price, "message": b.message})
                changed.append({**self.repo.order(o["id"]), "prev_filled": o["filled_qty"]})
        return changed

    def _resolve(self, o: dict, snap: Snapshot, now: datetime) -> list[dict]:
        found = snap.find_tag(o["tag"])
        if len(found) > 1:
            self.audit(o["trade_id"], "ORDER_TAG_AMBIGUOUS", "CRITICAL",
                       {"tag": o["tag"], "orders": [b.order_id for b in found]})
        if found:
            b = found[0]
            self.repo.update_order(o["id"], broker_order_id=b.order_id, status=b.status, filled_qty=b.filled_qty,
                                   avg_price=b.avg_price, status_message=b.message, missing_since=None)
            self.audit(o["trade_id"], "ORDER_RECOVERED", "WARNING",
                       {"tag": o["tag"], "order_id": b.order_id, "status": b.status, "kind": o["kind"],
                        "detail": "found in the Zerodha order book by tag; not re-placed"})
            return [{**self.repo.order(o["id"]), "prev_filled": 0}]
        if not o["missing_since"]:
            self.repo.update_order(o["id"], missing_since=now.isoformat(timespec="seconds"))
            return []
        waited = (now - datetime.fromisoformat(o["missing_since"])).total_seconds()
        if waited >= self.grace_s:
            why = f"tag not in the order book for {waited:.0f}s"
            self.repo.update_order(o["id"], status="NOT_PLACED",     # keep Kite's error: it says WHY
                                   status_message=f"{o['status_message']} ({why})" if o["status_message"] else why)
            self.audit(o["trade_id"], "ORDER_NOT_PLACED", "WARNING",
                       {"tag": o["tag"], "kind": o["kind"], "waited_s": waited})
            return [self.repo.order(o["id"])]
        return []
