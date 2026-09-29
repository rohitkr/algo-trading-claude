"""TradeService: the trade lifecycle, driven by user actions (preview / confirm / cancel / exit) and by the
monitor's tick(). All of it runs under one lock, so the web threads and the monitor never interleave.

Each tick:
  1. snapshot = Zerodha order book + net positions (a failed read means NO action this tick)
  2. resolve uncertain orders by tag, copy statuses/fills into `orders`        (orders.OrderPlacer.sync)
  3. per open trade, derive quantities from the order rows (filled, exited, open) - never from memory
  4. reconcile: Zerodha net position == what our fills say?  If not, repeat-confirm, then
       flat          -> MANUALLY_EXITED: cancel OUR working orders, place nothing else
       smaller       -> adopt the smaller quantity (closed outside), resize the SL order
       larger/flip   -> UNKNOWN_REQUIRES_RECONCILIATION: stop sending orders for this trade
     Any unresolved (INTENT/UNCERTAIN) order of the trade also blocks new orders for it this tick.
  5. manage: exit in progress, square-off / auto-exit time, stop (resting SL order + software check),
     target, trailing SL, partial booking, keep the SL order sized and priced.

Stops rest at Zerodha as SL (stop-limit) orders, so a crash of this process leaves the position protected.
(SL-M is not used: Zerodha blocks SL-M for F&O options.) Targets, partial booking and trailing are
monitored on Breeze prices, because a resting target order next to a resting SL order could both fill.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, timedelta

from zerodha.orders import round_to_tick

from . import lifecycle as L
from .broker import LOCAL_PENDING, KiteTraderBroker, Snapshot, is_terminal, is_working
from .config import TraderConfig, exchange_for
from .instruments import InstrumentService
from .orders import OrderPlacer
from .repository import Repository
from .risk import daily_pnl, pre_trade
from .validation import TradeRequest, validate

log = logging.getLogger("trader")
LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING, "ERROR": logging.ERROR,
          "CRITICAL": logging.CRITICAL}


class ActionError(RuntimeError):
    pass


def _same(a, b) -> bool:
    if a in (None, "") and b in (None, ""):
        return True
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return abs(float(a) - float(b)) < 1e-9
    return a == b


class TradeService:
    def __init__(self, cfg: TraderConfig, repo: Repository, broker: KiteTraderBroker, instruments: InstrumentService,
                 quotes, clock, audit_log=None, extra_tick=None):
        self.cfg, self.repo, self.broker, self.instruments, self.quotes = cfg, repo, broker, instruments, quotes
        self.clock, self.audit_log = clock, audit_log
        self.lock = threading.RLock()
        self.placer = OrderPlacer(repo, broker, self.audit, cfg.order_lookup_grace_s, clock)
        # Called at the end of every tick, under the same lock, after the per-trade engine has run - e.g.
        # StrategyService.tick for multi-leg combined-P&L rules. Optional; never required by TradeService.
        self.extra_tick = extra_tick
        self._resync = False

    # -- audit -------------------------------------------------------------------------------------
    def audit(self, trade_id: int | None, event: str, level: str = "INFO", detail: dict | None = None) -> None:
        self.repo.event(trade_id, event, level, detail)
        if self.audit_log is not None:
            self.audit_log.write(event, level=LEVELS.get(level, logging.INFO), trade_id=trade_id, detail=detail)
        else:
            log.log(LEVELS.get(level, logging.INFO), "trade %s %s %s", trade_id, event, detail or "")

    def _transition(self, t: dict, new: str, event: str, level: str = "INFO", detail: dict | None = None,
                    **fields) -> bool:
        ok = self.repo.set_status(t["id"], new, event, expect=t["status"], level=level, detail=detail, **fields)
        if ok and self.audit_log is not None:
            self.audit_log.write(event, level=LEVELS.get(level, logging.INFO), trade_id=t["id"],
                                 detail={"from": t["status"], "to": new, **(detail or {})})
        if ok:
            t.update(self.repo.trade(t["id"]))
        return ok

    # -- system state ------------------------------------------------------------------------------
    def halted(self) -> str | None:
        h = self.repo.status_values().get("halt", {}).get("value")
        if not h:
            return None
        if h.get("daily") and h.get("day") != self.clock().date().isoformat():
            return None                     # a daily-limit halt ends with the day
        return h.get("reason")

    def halt(self, reason: str, daily: bool = False) -> None:
        if self.halted() == reason:
            return
        self.repo.set_status_value("halt", {"reason": reason, "daily": daily, "day": self.clock().date().isoformat()})
        self.audit(None, "NEW_TRADES_BLOCKED", "CRITICAL" if not daily else "WARNING", {"reason": reason})

    def resume(self) -> None:
        with self.lock:
            self.repo.set_status_value("halt", None)
            self.audit(None, "NEW_TRADES_RESUMED", "WARNING", {})

    # -- user actions --------------------------------------------------------------------------------
    def instrument_for(self, req: TradeRequest):
        if req.underlying not in self.cfg.underlyings:
            raise ActionError(f"{req.underlying} is not enabled (TRADER_UNDERLYINGS)")
        try:
            return self.instruments.resolve(req.underlying, req.expiry, req.strike, req.option_type)
        except KeyError as exc:
            raise ActionError(str(exc).strip('"')) from None

    def _risk(self, *, inst, req: TradeRequest, quantity: int, ltp, snap: Snapshot | None) -> list:
        now = self.clock()
        open_trades = self.repo.trades(L.OPEN_STATUSES)
        pnl = daily_pnl(self.repo.closed_on(now.date()), open_trades)
        return pre_trade(self.cfg, now=now, tradingsymbol=inst.tradingsymbol, exchange=inst.exchange,
                         underlying=req.underlying, side=req.side, lots=req.lots, quantity=quantity,
                         entry=req.entry_price, stop=req.stop_loss, ltp=ltp, open_trades=open_trades,
                         trades_today=self.repo.confirmed_on(now.date()), day_pnl=pnl,
                         broker_net=snap.net_any_product(inst.exchange, inst.tradingsymbol) if snap else None,
                         halted=self.halted())

    def preview(self, payload: dict) -> dict:
        """Validate + risk-check a trade and store it (DRAFT -> READY). Places nothing. Returns a single-use
        confirm token when everything passes."""
        with self.lock:
            now = self.clock()
            try:
                req = TradeRequest.from_json(payload, self.cfg.product)
            except (ValueError, TypeError) as exc:
                return {"ok": False, "errors": [str(exc)]}
            try:
                inst = self.instrument_for(req)
            except ActionError as exc:
                return {"ok": False, "errors": [str(exc)]}
            qty = req.lots * inst.lot_size
            errors = validate(req, inst.lot_size, inst.tick_size, now)
            if req.product == "NRML" and self.cfg.square_off_time:
                pass                      # NRML trades are squared off too while TRADER_SQUARE_OFF_TIME is set
            partial_qty = req.partial_lots * inst.lot_size if req.partial_enabled and req.partial_lots else None
            auto_exit_at = datetime.combine(now.date(), req.auto_exit_time) if req.auto_exit_time else None
            # order_type is a display/workflow label (MIS | CNC | BTST): CNC and BTST both place as product
            # NRML (Zerodha has neither for F&O); BTST additionally skips the global square-off time below,
            # so the position is meant to carry overnight and gets picked back up by this same engine on
            # the next day's run - trades already resume across restarts regardless of which day they opened.
            order_type = str(payload.get("order_type") or req.product).upper()
            tid = self.repo.insert_trade(dict(
                mode=self.cfg.mode, trade_date=now.date().isoformat(), underlying=req.underlying,
                exchange=inst.exchange, tradingsymbol=inst.tradingsymbol, expiry=inst.expiry.isoformat(),
                strike=inst.strike, option_type=req.option_type, side=req.side, product=req.product,
                order_type=order_type,
                lot_size=inst.lot_size, tick_size=inst.tick_size, lots=req.lots, quantity=qty,
                entry_price=req.entry_price, initial_sl=req.stop_loss, current_sl=req.stop_loss, target=req.target,
                trail_enabled=int(req.trail_enabled), trail_type=req.trail_type if req.trail_enabled else None,
                trail_value=req.trail_value if req.trail_enabled else None,
                trail_step=(req.trail_step if req.trail_step is not None else inst.tick_size) if req.trail_enabled else None,
                partial_enabled=int(req.partial_enabled), partial_lots=req.partial_lots if req.partial_enabled else None,
                partial_qty=partial_qty, partial_price=req.partial_price if req.partial_enabled else None,
                auto_exit_at=auto_exit_at.isoformat(timespec="seconds") if auto_exit_at else None,
                status=L.DRAFT))
            self.audit(tid, "TRADE_CREATED", "INFO", {"request": payload, "tradingsymbol": inst.tradingsymbol,
                                                       "lot_size": inst.lot_size, "quantity": qty})
            ltp = self._ltp_safe(inst, max_age=self.cfg.quote_ttl_s)
            try:
                snap = self.broker.snapshot(now)
            except Exception as exc:
                snap = None
                self._broker_error("snapshot (preview)", exc)
            checks = self._risk(inst=inst, req=req, quantity=qty, ltp=ltp, snap=snap)
            risk_failed = [c for c in checks if not c.passed]
            self.audit(tid, "VALIDATION_FAILED" if errors else "VALIDATION_PASSED", "WARNING" if errors else "INFO",
                       {"errors": errors})
            self.audit(tid, "RISK_CHECK", "WARNING" if risk_failed else "INFO",
                       {"checks": [vars(c) for c in checks]})
            summary = self._summary(self.repo.trade(tid), ltp)
            if errors or risk_failed:
                self.repo.update_trade(tid, error="; ".join(errors + [f"{c.name}: {c.detail}" for c in risk_failed]))
                return {"ok": False, "trade_id": tid, "errors": errors,
                        "risk": [vars(c) for c in checks], "summary": summary}
            expires = now + timedelta(seconds=self.cfg.confirm_token_s)
            self.repo.set_status(tid, L.READY, "READY_FOR_CONFIRMATION", detail={"expires_at": expires.isoformat()})
            token = self.repo.issue_token(tid, "CONFIRM", expires)
            return {"ok": True, "trade_id": tid, "token": token, "expires_at": expires.isoformat(timespec="seconds"),
                    "risk": [vars(c) for c in checks], "summary": summary, "mode": self.cfg.mode}

    def confirm(self, trade_id: int, token: str) -> dict:
        """Place the entry order for a READY trade. The token is single-use and the READY -> ENTRY_ORDER_PLACED
        move is compare-and-set, so a double click / resubmitted form can never place two entries."""
        with self.lock:
            t = self._get(trade_id)
            if not self.repo.consume_token(token, trade_id, "CONFIRM"):
                self.audit(trade_id, "CONFIRM_REFUSED", "WARNING", {"reason": "invalid, used or expired token"})
                raise ActionError("confirmation token is invalid, already used or expired: preview the trade again")
            if t["status"] != L.READY:
                raise ActionError(f"trade {trade_id} is {t['status']}, not READY")
            inst = self.instruments.by_symbol(t["exchange"], t["tradingsymbol"])
            req = self._request_of(t)
            ltp = self._ltp_safe(inst, max_age=self.cfg.quote_ttl_s)
            try:
                snap = self.broker.snapshot(self.clock())
            except Exception as exc:
                self._broker_error("snapshot (confirm)", exc)
                raise ActionError(f"cannot reach Zerodha to check positions: {exc}") from None
            checks = self._risk(inst=inst, req=req, quantity=t["quantity"], ltp=ltp, snap=snap)
            failed = [c for c in checks if not c.passed]
            self.audit(trade_id, "RISK_CHECK", "WARNING" if failed else "INFO",
                       {"at": "confirm", "checks": [vars(c) for c in checks]})
            if failed:
                self._transition(t, L.EXPIRED, "RISK_BLOCKED", "WARNING",
                                 {"failed": [vars(c) for c in failed]},
                                 error="; ".join(f"{c.name}: {c.detail}" for c in failed))
                raise ActionError("blocked by risk limits: " + "; ".join(f"{c.name} ({c.detail})" for c in failed))
            if not self._transition(t, L.ENTRY_ORDER_PLACED, "ENTRY_CONFIRMED", "INFO", {"ltp": ltp},
                                    confirmed_at=self.repo.now()):
                raise ActionError("trade changed while confirming; nothing was placed")
            row = self.placer.place(t, "ENTRY", t["side"], t["quantity"], "LIMIT", t["entry_price"])
            if row is None:
                raise ActionError("an entry order already exists for this trade")
            if row["status"] == "SUBMITTED":
                self._transition(t, L.ENTRY_PENDING, "ENTRY_ORDER_ACCEPTED", "INFO",
                                 {"order_id": row["broker_order_id"]}, entry_order_id=row["broker_order_id"])
            return {"ok": True, "trade": self.trade_view(trade_id), "order_status": row["status"]}

    def prepare(self, trade_id: int, action: str) -> dict:
        """Token + summary for a dangerous action (EXIT / CANCEL), shown in a confirmation dialog."""
        with self.lock:
            t = self._get(trade_id)
            if action not in ("EXIT", "CANCEL"):
                raise ActionError("unknown action")
            if action == "CANCEL" and not any(is_working(o["status"]) for o in self.repo.orders(trade_id, "ENTRY")):
                raise ActionError("no working entry order to cancel")
            if action == "EXIT" and t["status"] not in L.LIVE_STATUSES:
                raise ActionError(f"trade is {t['status']}; nothing to exit")
            token = self.repo.issue_token(trade_id, action, self.clock() + timedelta(seconds=self.cfg.confirm_token_s))
            return {"token": token, "action": action, "trade": self.trade_view(trade_id)}

    def cancel_entry(self, trade_id: int, token: str) -> dict:
        with self.lock:
            t = self._get(trade_id)
            if not self.repo.consume_token(token, trade_id, "CANCEL"):
                raise ActionError("confirmation token is invalid, used or expired")
            working = [o for o in self.repo.orders(trade_id, "ENTRY") if is_working(o["status"])]
            if not working:
                raise ActionError("no working entry order")
            for o in working:
                if o["status"] in LOCAL_PENDING:
                    raise ActionError("the entry order's placement is still being confirmed with Zerodha; retry shortly")
                self.placer.cancel(o, "user cancelled the entry")
            self.audit(trade_id, "USER_CANCEL_ENTRY", "INFO", {"filled_qty": t["filled_qty"]})
            self._tick_locked()
            return {"ok": True, "trade": self.trade_view(trade_id)}

    def request_exit(self, trade_id: int, token: str) -> dict:
        with self.lock:
            t = self._get(trade_id)
            if not self.repo.consume_token(token, trade_id, "EXIT"):
                raise ActionError("confirmation token is invalid, used or expired")
            if t["status"] not in L.LIVE_STATUSES:
                raise ActionError(f"trade is {t['status']}; nothing to exit")
            if t["pending_exit_reason"]:
                raise ActionError(f"an exit is already in progress ({t['pending_exit_reason']})")
            self.repo.update_trade(trade_id, pending_exit_reason=L.USER_EXIT)
            self.audit(trade_id, "EXIT_REQUESTED", "INFO", {"reason": L.USER_EXIT})
            self._tick_locked()           # fresh broker snapshot, position verified, then the exit
            return {"ok": True, "trade": self.trade_view(trade_id)}

    def prepare_partial_exit(self, trade_id: int, qty: int) -> dict:
        """Token + summary for a user-requested partial exit of `qty` units (a whole number of lots,
        1 <= qty < the open quantity - the rest stays open and managed as before). Nothing is placed until
        confirm_partial_exit(token)."""
        with self.lock:
            t = self._get(trade_id)
            if t["status"] not in L.LIVE_STATUSES:
                raise ActionError(f"trade is {t['status']}; nothing to exit")
            if t["pending_exit_reason"]:
                raise ActionError(f"an exit is already in progress ({t['pending_exit_reason']})")
            if t["pending_partial_qty"]:
                raise ActionError("a partial exit is already queued for this trade")
            q = self._derive(t)
            if q["uncertain"]:
                raise ActionError("an order of this trade is still being confirmed with Zerodha; retry shortly")
            open_qty, lot = q["open"], t["lot_size"]
            if open_qty <= 0:
                raise ActionError("nothing open on this trade to exit")
            qty = int(qty) if qty else 0
            if qty <= 0 or qty % lot != 0:
                raise ActionError(f"quantity must be a positive multiple of the lot size ({lot})")
            if qty >= open_qty:
                raise ActionError(f"quantity must be less than the open quantity ({open_qty}); use Exit for all of it")
            token = self.repo.issue_token(trade_id, "PARTIAL_EXIT",
                                          self.clock() + timedelta(seconds=self.cfg.confirm_token_s), payload={"qty": qty})
            return {"token": token, "qty": qty, "open_qty": open_qty, "trade": self.trade_view(trade_id)}

    def confirm_partial_exit(self, trade_id: int, token: str) -> dict:
        with self.lock:
            t = self._get(trade_id)
            tok = self.repo.consume_token(token, trade_id, "PARTIAL_EXIT")
            if not tok:
                raise ActionError("confirmation token is invalid, used or expired: review the partial exit again")
            if t["status"] not in L.LIVE_STATUSES:
                raise ActionError(f"trade is {t['status']}; nothing to exit")
            if t["pending_exit_reason"]:
                raise ActionError(f"an exit is already in progress ({t['pending_exit_reason']})")
            qty = int(tok["payload"]["qty"])
            self.repo.update_trade(trade_id, pending_partial_qty=qty)
            self.audit(trade_id, "PARTIAL_EXIT_REQUESTED", "INFO", {"qty": qty})
            self._tick_locked()           # fresh broker snapshot, position verified, then the partial exit
            return {"ok": True, "trade": self.trade_view(trade_id)}

    # -- edits ----------------------------------------------------------------------------------------
    EDITABLE = ("entry_price", "lots", "stop_loss", "target", "trail_enabled", "trail_type", "trail_value",
                "trail_step", "partial_enabled", "partial_lots", "partial_price", "auto_exit_time")

    def prepare_edit(self, trade_id: int, changes: dict) -> dict:
        """Validate + risk-check an edit; returns the old -> new diff and a single-use token bound to exactly
        these changes. Nothing is modified until apply_edit(token)."""
        with self.lock:
            t = self._get(trade_id)
            try:
                plan = self._plan_edit(t, changes)
            except ActionError as exc:
                return {"ok": False, "errors": [str(exc)]}
            if plan["errors"]:
                self.audit(trade_id, "EDIT_VALIDATION_FAILED", "WARNING", {"changes": changes, "errors": plan["errors"]})
                return {"ok": False, "errors": plan["errors"], "risk": plan["risk"]}
            if not plan["diff"]:
                return {"ok": False, "errors": ["nothing changed"]}
            token = self.repo.issue_token(trade_id, "EDIT", self.clock() + timedelta(seconds=self.cfg.confirm_token_s),
                                          payload=plan["changes"])
            return {"ok": True, "token": token, "diff": plan["diff"], "risk": plan["risk"], "mode": self.cfg.mode,
                    "modifies_entry_order": bool(plan["entry_mod"]), "trade": self.trade_view(trade_id)}

    def apply_edit(self, trade_id: int, token: str) -> dict:
        with self.lock:
            tok = self.repo.consume_token(token, trade_id, "EDIT")
            if not tok:
                raise ActionError("confirmation token is invalid, used or expired: review the edit again")
            t = self._get(trade_id)
            plan = self._plan_edit(t, tok["payload"] or {})      # re-checked against the state right now
            if plan["errors"]:
                self.audit(trade_id, "EDIT_REFUSED", "WARNING", {"errors": plan["errors"]})
                raise ActionError("; ".join(plan["errors"]))
            if plan["entry_mod"]:
                entry = plan["entry_order"]
                if not self.placer.modify(entry, **plan["entry_mod"]):
                    raise ActionError("Zerodha did not accept the entry order change; nothing was changed "
                                      "(see the trade's audit trail)")
            if plan["fields"]:
                self.repo.update_trade(trade_id, **plan["fields"])
            self.audit(trade_id, "TRADE_EDITED", "INFO", {"diff": plan["diff"], "entry_order_modified": plan["entry_mod"]})
            self._tick_locked()                   # resting SL order resized / re-priced right away
            return {"ok": True, "trade": self.trade_view(trade_id)}

    def _plan_edit(self, t: dict, changes: dict) -> dict:
        if t["status"] not in L.LIVE_STATUSES:
            raise ActionError(f"trade is {t['status']}; it can no longer be edited")
        if t["pending_exit_reason"]:
            raise ActionError(f"an exit is in progress ({t['pending_exit_reason']}); it can no longer be edited")
        unknown = set(changes) - set(self.EDITABLE)
        if unknown:
            raise ActionError(f"not editable: {sorted(unknown)}")
        q = self._derive(t)
        if q["uncertain"]:
            raise ActionError("an order of this trade is still being confirmed with Zerodha; retry in a few seconds")
        entry = q["entry"]
        entry_working = bool(entry and entry["broker_order_id"] and is_working(entry["status"])
                             and not entry["cancel_requested"])
        inst = self.instruments.by_symbol(t["exchange"], t["tradingsymbol"])
        lot, tick = t["lot_size"], t["tick_size"]
        cur = {"entry_price": t["entry_price"], "lots": t["lots"], "stop_loss": t["current_sl"], "target": t["target"],
               "trail_enabled": bool(t["trail_enabled"]), "trail_type": t["trail_type"] or "POINTS",
               "trail_value": t["trail_value"], "trail_step": t["trail_step"],
               "partial_enabled": bool(t["partial_enabled"]), "partial_lots": t["partial_lots"],
               "partial_price": t["partial_price"],
               "auto_exit_time": t["auto_exit_at"][11:16] if t["auto_exit_at"] else ""}
        merged = {**cur, **{k: v for k, v in changes.items()}}
        try:
            req = TradeRequest.from_json({**merged, "underlying": t["underlying"], "expiry": t["expiry"],
                                          "strike": t["strike"], "option_type": t["option_type"], "side": t["side"],
                                          "product": t["product"]}, t["product"])
        except (ValueError, TypeError) as exc:
            return {"errors": [str(exc)], "risk": [], "diff": {}, "fields": {}, "entry_mod": {}, "changes": changes}
        new = {"entry_price": req.entry_price, "lots": req.lots, "stop_loss": req.stop_loss, "target": req.target,
               "trail_enabled": req.trail_enabled, "trail_type": req.trail_type if req.trail_enabled else cur["trail_type"],
               "trail_value": req.trail_value, "trail_step": req.trail_step,
               "partial_enabled": req.partial_enabled, "partial_lots": req.partial_lots,
               "partial_price": req.partial_price,
               "auto_exit_time": req.auto_exit_time.strftime("%H:%M") if req.auto_exit_time else ""}
        diff = {k: [cur[k], new[k]] for k in new if not _same(cur[k], new[k])}
        errors: list[str] = []
        now = self.clock()
        ltp = self._ltp_safe(inst, max_age=self.cfg.quote_ttl_s)
        entry_changed = any(k in diff for k in ("entry_price", "lots"))
        if entry_changed and not entry_working:
            errors.append("the entry order is no longer working: entry price and lots can't change")
        if "auto_exit_time" not in diff:
            req.auto_exit_time = None                 # an unchanged (maybe past) time is not re-validated
        if t["partial_done"] and "partial_enabled" in diff or (t["partial_done"] and
                                                               any(k in diff for k in ("partial_lots", "partial_price"))):
            errors.append("partial booking has already happened")
        if q["filled"] == 0:
            errors += validate(req, lot, tick, now)   # nothing filled: exactly the creation rules
        else:
            # A position exists: SL / target / partial are checked against the current price (a SL in profit is
            # fine), and partial lots against the open quantity.
            ref = ltp if ltp is not None else q["entry_avg"]
            ref = round(round(ref / tick) * tick, 2)
            open_lots = q["open"] // lot if not entry_changed else (req.lots * lot - q["exited"]) // lot
            chk = TradeRequest(**{**req.__dict__, "entry_price": ref, "lots": max(open_lots, 1)})
            if t["partial_done"]:
                chk.partial_enabled = False
            errs = validate(chk, lot, tick, now)
            errors += [e.replace("entry price", f"current price ({'LTP' if ltp is not None else 'entry avg'})")
                       for e in errs if "expiry" not in e]
        if req.lots * lot < q["filled"]:
            errors.append(f"quantity can't go below the filled {q['filled']}")
        risk = []
        if not errors:
            others = [x for x in self.repo.trades(L.OPEN_STATUSES) if x["id"] != t["id"]]
            if entry_changed:
                try:
                    snap = self.broker.snapshot(now)
                    net = snap.net_any_product(t["exchange"], t["tradingsymbol"]) - L.direction(t["side"]) * q["open"]
                except Exception as exc:
                    self._broker_error("snapshot (edit)", exc)
                    net = None
                risk = pre_trade(self.cfg, now=now, tradingsymbol=t["tradingsymbol"], exchange=t["exchange"],
                                 underlying=t["underlying"], side=t["side"], lots=req.lots, quantity=req.lots * lot,
                                 entry=req.entry_price, stop=req.stop_loss, ltp=ltp, open_trades=others,
                                 trades_today=self.repo.confirmed_on(now.date()) - 1,
                                 day_pnl=daily_pnl(self.repo.closed_on(now.date()), self.repo.trades(L.OPEN_STATUSES)),
                                 broker_net=net, halted=self.halted())
                # a working entry is not re-checked against things that only apply to opening a new trade
                risk = [c for c in risk if c.name not in ("ltp_not_through_stop",) or q["filled"] == 0]
            else:
                basis = q["entry_avg"] or req.entry_price
                qty = q["open"] if q["filled"] else req.lots * lot
                loss = max(0.0, (basis - req.stop_loss) if t["side"] == "BUY" else (req.stop_loss - basis)) * qty
                from .risk import RiskCheck
                risk = [RiskCheck("max_loss_per_trade", loss <= self.cfg.max_loss_per_trade,
                                  f"₹{loss:,.0f} at the new SL, limit ₹{self.cfg.max_loss_per_trade:,.0f}")]
            errors += [f"{c.name}: {c.detail}" for c in risk if not c.passed]
        fields: dict = {}
        if "entry_price" in diff:
            fields["entry_price"] = req.entry_price
        if "lots" in diff:
            fields.update(lots=req.lots, quantity=req.lots * lot)
        if "stop_loss" in diff:
            fields.update(current_sl=req.stop_loss, user_sl=req.stop_loss, stop_breached_at=None)
            if q["filled"] == 0:
                fields["initial_sl"] = req.stop_loss          # not started yet: this IS the initial SL
            if req.trail_enabled:
                fields["best_price"] = t["last_ltp"]          # trail from here, not from an older best price
        if "target" in diff:
            fields["target"] = req.target
        if any(k in diff for k in ("trail_enabled", "trail_type", "trail_value", "trail_step")):
            fields.update(trail_enabled=int(req.trail_enabled), trail_type=req.trail_type if req.trail_enabled else None,
                          trail_value=req.trail_value if req.trail_enabled else None,
                          trail_step=(req.trail_step if req.trail_step is not None else tick) if req.trail_enabled else None,
                          best_price=t["last_ltp"] if req.trail_enabled else None)
        if any(k in diff for k in ("partial_enabled", "partial_lots", "partial_price")):
            fields.update(partial_enabled=int(req.partial_enabled),
                          partial_lots=req.partial_lots if req.partial_enabled else None,
                          partial_qty=req.partial_lots * lot if req.partial_enabled and req.partial_lots else None,
                          partial_price=req.partial_price if req.partial_enabled else None)
        if "auto_exit_time" in diff:
            fields["auto_exit_at"] = (datetime.combine(now.date(), req.auto_exit_time).isoformat(timespec="seconds")
                                      if req.auto_exit_time else None)
        entry_mod = {}
        if entry_changed and entry_working:
            if "entry_price" in diff:
                entry_mod["price"] = req.entry_price
            if "lots" in diff:
                entry_mod["quantity"] = req.lots * lot       # Kite: total order quantity (>= filled)
        return {"errors": errors, "risk": [vars(c) for c in risk], "diff": diff, "fields": fields,
                "entry_mod": entry_mod, "entry_order": entry, "changes": changes}

    def _adopt_entry_terms(self, t: dict, entry: dict) -> None:
        """The working entry order's price/quantity at Zerodha is the truth (an edit whose response was lost,
        or a change made by hand in Kite)."""
        upd = {}
        if entry["quantity"] != t["quantity"]:
            upd.update(quantity=entry["quantity"], lots=entry["quantity"] // t["lot_size"])
            if t["partial_qty"] and t["partial_qty"] >= entry["quantity"]:
                upd.update(partial_enabled=0)
        if entry["price"] is not None and abs(entry["price"] - t["entry_price"]) > 1e-9:
            upd["entry_price"] = entry["price"]
        if upd:
            self.repo.update_trade(t["id"], **upd)
            self.audit(t["id"], "ENTRY_TERMS_SYNCED", "WARNING",
                       {"changes": {k: [t.get(k), v] for k, v in upd.items()}, "detail": "taken from the Zerodha order"})
            t.update(self.repo.trade(t["id"]))

    # -- monitor ------------------------------------------------------------------------------------
    def startup(self) -> dict:
        with self.lock:
            open_ = self.repo.trades(L.OPEN_STATUSES)
            self.audit(None, "APP_RECOVERY", "WARNING" if open_ else "INFO",
                       {"open_trades": [(t["id"], t["tradingsymbol"], t["status"]) for t in open_],
                        "uncertain_orders": [o["tag"] for o in self.repo.orders(statuses=LOCAL_PENDING)],
                        "mode": self.cfg.mode})
            self.repo.set_status_value("process", {"state": "running", "started_at": self.repo.now(),
                                                   "mode": self.cfg.mode})
            return self._tick_locked()

    def tick(self) -> dict:
        with self.lock:
            return self._tick_locked()

    def _tick_locked(self) -> dict:
        result = {}
        for _ in range(3):                    # an extra pass right after a cancel we must see confirmed
            self._resync = False
            result = self._one_pass()
            if not self._resync or not result.get("ok"):
                break
        return result

    def _one_pass(self) -> dict:
        now = self.clock()
        self.repo.set_status_value("heartbeat", now.isoformat(timespec="seconds"))
        try:
            snap = self.broker.snapshot(now)
        except Exception as exc:
            self._broker_error("snapshot", exc)
            return {"ok": False, "error": str(exc)}
        self.repo.set_status_value("broker", {"ok": True, "last_sync": now.isoformat(timespec="seconds"),
                                              "broker": self.broker.name})
        changed = self.placer.sync(snap)
        for o in changed:
            t = self.repo.trade(o["trade_id"])
            if t and t["status"] in L.TERMINAL and o["filled_qty"] > o.get("prev_filled", 0):
                self._fill_after_close(t, o)
        mismatches = 0
        for t in self.repo.trades(L.OPEN_STATUSES):
            try:
                if t["status"] in (L.ERROR, L.UNKNOWN):
                    self._recheck(t, snap)
                else:
                    self._process(t, snap, now)
            except Exception as exc:          # one trade's failure never stops the others
                log.exception("processing trade %s", t["id"])
                self.audit(t["id"], "PROCESSING_ERROR", "ERROR", {"error": f"{type(exc).__name__}: {exc}"})
            t2 = self.repo.trade(t["id"])
            mismatches += int(bool(t2 and t2["mismatch_count"]))
        self._expire_unconfirmed(now)
        self._daily_limits(now)
        if self.extra_tick is not None:
            try:
                self.extra_tick(now)
            except Exception:
                log.exception("extra_tick failed")
        self.repo.set_status_value("reconciliation", {"at": now.isoformat(timespec="seconds"),
                                                      "pending_mismatches": mismatches,
                                                      "needs_attention": len(self.repo.trades([L.ERROR, L.UNKNOWN]))})
        return {"ok": True, "changed_orders": len(changed)}

    def _broker_error(self, what: str, exc: Exception) -> None:
        msg = f"{what}: {type(exc).__name__}: {exc}"
        prev = self.repo.status_values().get("broker", {}).get("value") or {}
        self.repo.set_status_value("broker", {**prev, "ok": False, "last_error": msg, "error_at": self.repo.now()})
        self.repo.set_status_value("last_error", msg)
        self.audit(None, "BROKER_ERROR", "ERROR", {"error": msg})

    # -- per-trade processing ---------------------------------------------------------------------------
    def _derive(self, t: dict) -> dict:
        orders = self.repo.orders(t["id"])
        entries = [o for o in orders if o["kind"] == "ENTRY"]
        exits = [o for o in orders if o["kind"] in ("SL", "PARTIAL", "EXIT", "TARGET")]
        filled = sum(o["filled_qty"] for o in entries)
        entry_val = sum(o["filled_qty"] * (o["avg_price"] or 0) for o in entries)
        exited = sum(o["filled_qty"] for o in exits)
        exit_val = sum(o["filled_qty"] * (o["avg_price"] or 0) for o in exits)
        entry_avg = entry_val / filled if filled else None
        d = L.direction(t["side"])
        realized = (d * (exit_val - exited * entry_avg) if filled and exited else 0.0) + (t["outside_pnl"] or 0)
        return {"orders": orders, "entry": entries[-1] if entries else None, "filled": filled, "entry_avg": entry_avg,
                "exited": exited, "exit_avg": exit_val / exited if exited else None,
                "open": filled - exited - (t["outside_qty"] or 0), "realized": round(realized, 2),
                "uncertain": [o for o in orders if o["status"] in LOCAL_PENDING]}

    def _apply(self, t: dict, q: dict) -> None:
        upd = {}
        if q["filled"] != t["filled_qty"]:
            ev = "ENTRY_EXECUTED" if q["entry"] and q["filled"] >= q["entry"]["quantity"] else "ENTRY_PARTIAL_FILL"
            self.audit(t["id"], ev, "INFO", {"filled_qty": q["filled"], "avg_price": q["entry_avg"]})
            upd.update(filled_qty=q["filled"], entry_avg_price=q["entry_avg"])
            if not t["entry_time"]:
                upd["entry_time"] = self.repo.now()
        if q["exited"] != t["exited_qty"]:
            self.audit(t["id"], "EXIT_FILL", "INFO", {"exited_qty": q["exited"], "avg_price": q["exit_avg"]})
            upd["exited_qty"] = q["exited"]
        if q["open"] != t["open_qty"]:
            upd["open_qty"] = q["open"]
        if round(q["realized"], 2) != round(t["realized_pnl"] or 0, 2):
            upd["realized_pnl"] = q["realized"]
        pos = "NONE" if q["filled"] == 0 else ("CLOSED" if q["open"] == 0 else
                                               ("PARTIAL" if q["open"] < t["quantity"] else "OPEN"))
        if pos != t["position_status"]:
            upd["position_status"] = pos
        if q["entry"] and q["entry"]["status"] != t["broker_status"]:
            upd["broker_status"] = q["entry"]["status"]
        if upd:
            self.repo.update_trade(t["id"], **upd)
            t.update(self.repo.trade(t["id"]))

    def _process(self, t: dict, snap: Snapshot, now: datetime) -> None:
        q = self._derive(t)
        self._apply(t, q)
        entry = q["entry"]
        if entry and entry["broker_order_id"] and is_working(entry["status"]):
            self._adopt_entry_terms(t, entry)

        # --- entry order outcome -----------------------------------------------------------------
        if entry is None:
            if t["status"] == L.ENTRY_ORDER_PLACED:
                # The intent row is written before Kite is called, so no row = nothing was ever sent.
                self._transition(t, L.REJECTED, "ENTRY_NEVER_SENT", "WARNING",
                                 {"detail": "crash between confirm and the order intent; nothing reached Zerodha"},
                                 error="entry order was never sent")
            return
        if t["status"] == L.ENTRY_ORDER_PLACED and entry["broker_order_id"] and is_working(entry["status"]):
            self._transition(t, L.ENTRY_PENDING, "ENTRY_ORDER_ACCEPTED", "INFO", {"order_id": entry["broker_order_id"]},
                             entry_order_id=entry["broker_order_id"])
        if entry["broker_order_id"] and t["entry_order_id"] != entry["broker_order_id"]:
            self.repo.update_trade(t["id"], entry_order_id=entry["broker_order_id"])
        if q["filled"] == 0:
            if entry["status"] == "NOT_PLACED":
                self._transition(t, L.REJECTED, "ENTRY_NOT_PLACED", "WARNING",
                                 {"detail": entry["status_message"]},
                                 error=f"entry order never reached Zerodha: {entry['status_message']}")
            elif entry["status"] == "REJECTED":
                self._transition(t, L.REJECTED, "ENTRY_REJECTED", "WARNING", {"message": entry["status_message"]},
                                 error=entry["status_message"])
            elif entry["status"] in ("CANCELLED", "EXPIRED", "CANCELLED AMO"):
                self._transition(t, L.CANCELLED, "ENTRY_CANCELLED", "INFO",
                                 {"by": "user" if entry["cancel_requested"] else "broker/outside",
                                  "message": entry["status_message"]})
            else:
                reason = self._time_exit_reason(t, now)
                if reason and is_working(entry["status"]) and not entry["cancel_requested"]:
                    self.placer.cancel(entry, f"{reason} before the entry filled")
                    self._resync = True
                elif is_working(entry["status"]):
                    # Still resting, unfilled: show a live market price anyway (slow cadence - there's no
                    # position to protect yet) so the user can judge whether their limit entry is realistic.
                    inst = self.instruments.by_symbol(t["exchange"], t["tradingsymbol"])
                    ltp = self._ltp_safe(inst, max_age=self.cfg.quote_slow_s)
                    self._mark(t, q, ltp, now)
            return
        if is_terminal(entry["status"]) and t["status"] in (L.ENTRY_ORDER_PLACED, L.ENTRY_PENDING):
            self._transition(t, L.ENTRY_EXECUTED, "ENTRY_COMPLETE", "INFO",
                             {"filled_qty": q["filled"], "avg_price": q["entry_avg"], "entry_status": entry["status"]})

        # --- reconciliation (before ANY order for this trade) ---------------------------------------
        if q["uncertain"]:
            self.repo.update_trade(t["id"], reconcile_info=json.dumps(
                {"at": now.isoformat(), "waiting_on": [o["tag"] for o in q["uncertain"]]}))
            return
        verdict = self._reconcile(t, q, snap, now)
        if verdict == "stop":
            return
        if verdict == "adopted":
            q = self._derive(t)
            self._apply(t, q)

        # --- fully closed by our own orders ---------------------------------------------------------
        if q["open"] <= 0:
            if q["open"] < 0:
                self._transition(t, L.UNKNOWN, "OVER_EXIT", "CRITICAL", {"open_qty": q["open"]},
                                 error="our exit orders filled more than the position")
                self.halt(f"trade {t['id']} over-exited")
                return
            if is_working(entry["status"]):
                if not entry["cancel_requested"]:
                    self.placer.cancel(entry, "position closed; cancel the unfilled entry remainder")
                    self._resync = True
                return
            self._finish(t, q, now)
            return

        # --- manage the open position -----------------------------------------------------------------
        inst = self.instruments.by_symbol(t["exchange"], t["tradingsymbol"])
        ltp = self._ltp_safe(inst, max_age=self._price_age(t, now))
        self._mark(t, q, ltp, now)
        self._mark_kite_ltp(t, snap)   # display only (Active-trades table); SL/target/trailing stay on Breeze
        if t["pending_exit_reason"]:
            self._continue_exit(t, q, ltp, now)
            return
        reason = self._time_exit_reason(t, now)
        if reason:
            self._begin_exit(t, q, ltp, now, reason)
            return
        if ltp is not None:
            if self._stop_breached(t, ltp):
                if not t["stop_breached_at"]:
                    self.repo.update_trade(t["id"], stop_breached_at=now.isoformat(timespec="seconds"))
                    t["stop_breached_at"] = now.isoformat(timespec="seconds")
                sl_live = [o for o in q["orders"] if o["kind"] == "SL" and is_working(o["status"])
                           and o["broker_order_id"] and not o["cancel_requested"]]
                waited = (now - datetime.fromisoformat(t["stop_breached_at"])).total_seconds()
                if not sl_live or waited >= self.cfg.stop_grace_s:
                    self.audit(t["id"], "STOP_TRIGGERED", "WARNING",
                               {"ltp": ltp, "sl": t["current_sl"], "resting_sl_order": bool(sl_live),
                                "breached_for_s": waited})
                    self._begin_exit(t, q, ltp, now, self._sl_reason(t))
                    return
            elif t["stop_breached_at"]:
                self.repo.update_trade(t["id"], stop_breached_at=None)
            if t["target"] is not None and self._reached(t, ltp, t["target"]):
                self.audit(t["id"], "TARGET_REACHED", "INFO", {"ltp": ltp, "target": t["target"]})
                self._begin_exit(t, q, ltp, now, L.TARGET_HIT)
                return
            if t["trail_enabled"]:
                self._trail(t, ltp)
            if t["partial_enabled"] and not t["partial_done"]:
                self._partial(t, q, ltp, now)
        if t["pending_partial_qty"]:
            self._manual_partial(t, q, ltp, now)
        self._reprice_working(t, q, ltp, now, kinds=("PARTIAL",))
        self._ensure_sl(t, q, ltp, now)

    # -- reconciliation ---------------------------------------------------------------------------------
    def _reconcile(self, t: dict, q: dict, snap: Snapshot, now: datetime) -> str:
        expected = L.direction(t["side"]) * q["open"]
        actual = snap.net(t["exchange"], t["tradingsymbol"], t["product"])
        if actual == expected:
            if t["mismatch_count"]:
                self.audit(t["id"], "RECONCILED", "INFO", {"expected": expected, "actual": actual,
                                                           "detail": "mismatch cleared (broker position lag)"})
                self.repo.update_trade(t["id"], mismatch_count=0, reconcile_info=None)
                t["mismatch_count"] = 0
            return "ok"
        n = t["mismatch_count"] + 1
        info = {"at": now.isoformat(timespec="seconds"), "expected": expected, "actual": actual, "checks": n}
        self.repo.update_trade(t["id"], mismatch_count=n, reconcile_info=json.dumps(info))
        t["mismatch_count"] = n
        if n < self.cfg.reconcile_confirmations:
            self.audit(t["id"], "RECONCILE_MISMATCH", "WARNING", {**info, "action": "no orders until confirmed"})
            return "stop"
        d = L.direction(t["side"])
        if actual == 0:
            self._manual_exit(t, q, now, info)
            return "stop"
        if actual * d > 0 and abs(actual) < q["open"]:
            gone = q["open"] - abs(actual)
            ltp = t["last_ltp"] or q["entry_avg"]
            est = d * gone * (ltp - q["entry_avg"])
            self.repo.update_trade(t["id"], outside_qty=(t["outside_qty"] or 0) + gone,
                                   outside_pnl=round((t["outside_pnl"] or 0) + est, 2), mismatch_count=0)
            t.update(self.repo.trade(t["id"]))
            self.audit(t["id"], "MANUAL_PARTIAL_EXIT_DETECTED", "WARNING",
                       {**info, "closed_outside": gone, "pnl_estimated_at": ltp, "estimated_pnl": round(est, 2),
                        "action": "managing the remaining quantity; SL order resized"})
            return "adopted"
        self._transition(t, L.UNKNOWN, "RECONCILIATION_FAILED", "CRITICAL",
                         {**info, "action": "stopped sending orders for this trade; resolve in Kite"},
                         error=f"Zerodha shows {actual}, expected {expected}")
        return "stop"

    def _manual_exit(self, t: dict, q: dict, now: datetime, info: dict) -> None:
        # Cancel OUR resting orders (a leftover SL would open a new position), place nothing else.
        cancelled = []
        for o in q["orders"]:
            if is_working(o["status"]) and o["broker_order_id"]:
                self.placer.cancel(o, "position closed outside this system")
                cancelled.append(o["broker_order_id"])
        self._resync = True               # see the cancels confirmed in this tick
        ltp = t["last_ltp"]
        est = L.direction(t["side"]) * q["open"] * (ltp - q["entry_avg"]) if ltp and q["entry_avg"] else 0.0
        self._transition(t, L.MANUALLY_EXITED, "MANUAL_EXIT_DETECTED", "CRITICAL",
                         {**info, "cancelled_our_orders": cancelled, "pnl_estimated_at": ltp,
                          "action": "no exit order placed, trade will not be re-entered"},
                         exit_reason=L.MANUAL_EXIT, exit_time=self.repo.now(), exit_avg_price=ltp,
                         position_status="CLOSED", outside_qty=(t["outside_qty"] or 0) + q["open"], open_qty=0,
                         outside_pnl=round((t["outside_pnl"] or 0) + est, 2),
                         realized_pnl=round(q["realized"] + est, 2), unrealized_pnl=0, mismatch_count=0,
                         pending_exit_reason=None)

    def _recheck(self, t: dict, snap: Snapshot) -> None:
        """ERROR / UNKNOWN trades: read-only. Resume only when Zerodha matches our books again."""
        q = self._derive(t)
        if q["uncertain"]:
            return
        expected = L.direction(t["side"]) * q["open"]
        actual = snap.net(t["exchange"], t["tradingsymbol"], t["product"])
        working = [o for o in q["orders"] if is_working(o["status"])]
        if actual == expected and q["open"] > 0 and t["status"] == L.UNKNOWN:
            self._transition(t, L.POSITION_ACTIVE, "RECONCILED_RESUMED", "WARNING",
                             {"expected": expected, "actual": actual}, error=None, mismatch_count=0)
        elif actual == 0 and not working and (q["open"] > 0 or q["filled"] > 0):
            self._manual_exit(t, q, self.clock(), {"expected": expected, "actual": actual, "from": t["status"]})

    def _fill_after_close(self, t: dict, o: dict) -> None:
        self.audit(t["id"], "FILL_AFTER_CLOSE", "CRITICAL",
                   {"order_id": o["broker_order_id"], "kind": o["kind"], "filled": o["filled_qty"],
                    "detail": "an order of a closed trade executed; check positions in Kite"})
        self.halt(f"order {o['broker_order_id']} of closed trade {t['id']} executed")

    # -- exits ---------------------------------------------------------------------------------------
    def _begin_exit(self, t: dict, q: dict, ltp, now: datetime, reason: str) -> None:
        self.repo.update_trade(t["id"], pending_exit_reason=reason)
        t["pending_exit_reason"] = reason
        self.audit(t["id"], "EXIT_TRIGGERED", "INFO", {"reason": reason, "ltp": ltp, "open_qty": q["open"]})
        self._continue_exit(t, q, ltp, now)

    def _continue_exit(self, t: dict, q: dict, ltp, now: datetime) -> None:
        """Exit the open quantity: stop the entry remainder, take the SL/partial orders off (confirmed), then
        one marketable LIMIT exit. The position was verified against Zerodha earlier in this same tick."""
        orders = q["orders"]
        exits = [o for o in orders if o["kind"] == "EXIT" and is_working(o["status"])]
        if exits:
            self._reprice_working(t, q, ltp, now, kinds=("EXIT",))
            return
        last_exit = next((o for o in reversed(orders) if o["kind"] == "EXIT"), None)
        if t["status"] in (L.EXIT_ORDER_PLACED, L.EXIT_PENDING) and last_exit and is_terminal(last_exit["status"]):
            self._transition(t, L.POSITION_ACTIVE,
                             "EXIT_ORDER_ENDED_POSITION_LEFT" if last_exit["status"] == "COMPLETE" else "EXIT_ORDER_FAILED",
                             "WARNING",
                             {"order": last_exit["broker_order_id"], "status": last_exit["status"],
                              "message": last_exit["status_message"], "open_qty": q["open"]})
        blocking = False
        for o in orders:
            if o["kind"] in ("ENTRY", "SL", "PARTIAL") and is_working(o["status"]):
                if o["broker_order_id"] and not o["cancel_requested"]:
                    self.placer.cancel(o, f"exit ({t['pending_exit_reason']})")
                    self._resync = True
                if o["kind"] != "ENTRY":
                    blocking = True       # never have an exit and an SL/partial working together
        if blocking:
            return
        if t["exit_attempts"] >= self.cfg.max_exit_reprices + 1:
            self._transition(t, L.ERROR, "EXIT_FAILED_REPEATEDLY", "CRITICAL",
                             {"attempts": t["exit_attempts"], "open_qty": q["open"]},
                             error="exit orders keep failing; exit manually in Kite")
            return
        price = self._marketable(t, ltp, L.exit_side(t["side"]))
        if price is None:
            self.audit(t["id"], "EXIT_WAITING_FOR_PRICE", "ERROR", {"detail": "no Breeze price to price the exit"})
            return
        self._transition(t, L.EXIT_ORDER_PLACED, "EXIT_STARTED", "INFO",
                         {"reason": t["pending_exit_reason"], "qty": q["open"], "price": price, "ltp": ltp},
                         exit_attempts=t["exit_attempts"] + 1)
        row = self.placer.place(t, "EXIT", L.exit_side(t["side"]), q["open"], "LIMIT", price,
                                purpose=t["pending_exit_reason"])
        if row and row["status"] == "SUBMITTED":
            self._transition(t, L.EXIT_PENDING, "EXIT_ORDER_ACCEPTED", "INFO", {"order_id": row["broker_order_id"]},
                             exit_order_id=row["broker_order_id"])
            self._resync = True               # read the fill right away instead of on the next tick

    def _finish(self, t: dict, q: dict, now: datetime) -> None:
        reason = t["pending_exit_reason"]
        if not reason:
            # What closed the rest: an exit order (its purpose) or the SL order. A partial never closes it all.
            exits = [o for o in q["orders"] if o["kind"] == "EXIT" and o["filled_qty"]]
            reason = exits[-1]["purpose"] if exits and exits[-1]["purpose"] else self._sl_reason(t)
        self._cancel_leftovers(t, q, "trade closed")
        self._transition(t, L.EXITED, "TRADE_EXITED", "INFO",
                         {"reason": reason, "exit_avg_price": q["exit_avg"], "realized_pnl": q["realized"]},
                         exit_reason=reason, exit_avg_price=q["exit_avg"], exit_time=self.repo.now(),
                         position_status="CLOSED", realized_pnl=q["realized"], unrealized_pnl=0,
                         pending_exit_reason=None, open_qty=0)

    def _cancel_leftovers(self, t: dict, q: dict, why: str) -> None:
        for o in q["orders"]:
            if is_working(o["status"]) and o["broker_order_id"] and not o["cancel_requested"]:
                self.placer.cancel(o, why)

    # -- stop-loss order --------------------------------------------------------------------------------
    def _ensure_sl(self, t: dict, q: dict, ltp, now: datetime) -> None:
        partial_left = sum(o["quantity"] - o["filled_qty"] for o in q["orders"]
                           if o["kind"] == "PARTIAL" and is_working(o["status"]))
        want = q["open"] - partial_left
        sls = [o for o in q["orders"] if o["kind"] == "SL"]
        working = [o for o in sls if is_working(o["status"])]
        if working:
            w = working[-1]
            if len(working) > 1:
                self.audit(t["id"], "MULTIPLE_SL_ORDERS", "CRITICAL", {"orders": [o["broker_order_id"] for o in working]})
            if w["cancel_requested"] or not w["broker_order_id"]:
                return
            if want <= 0:
                self.placer.cancel(w, "nothing left to protect")
                return
            if w["quantity"] - w["filled_qty"] != want:
                self.placer.modify(w, quantity=w["filled_qty"] + want)
            if abs((w["trigger_price"] or 0) - t["current_sl"]) > 1e-9:
                if w["modifications"] >= self.cfg.sl_max_modifications:
                    self.audit(t["id"], "SL_ORDER_REPLACING", "INFO", {"modifications": w["modifications"]})
                    self.placer.cancel(w, "too many modifications; replacing")
                    self._resync = True
                else:
                    self.placer.modify(w, trigger_price=t["current_sl"], price=self._sl_limit(t, t["current_sl"]))
            self._activate(t)
            return
        if t["sl_software_only"] or want <= 0:
            self._activate(t)
            return
        last = sls[-1] if sls else None
        today = now.date().isoformat()
        if last and last["status"] == "CANCELLED" and not last["cancel_requested"] and last["order_date"] == today:
            self._software_only(t, "SL_ORDER_CANCELLED_OUTSIDE", "the SL order was cancelled outside this system")
            return
        if last and last["status"] == "REJECTED":
            self._software_only(t, "SL_ORDER_REJECTED", last["status_message"] or "rejected")
            return
        if len([o for o in sls if o["status"] == "NOT_PLACED"]) >= 3:
            self._software_only(t, "SL_ORDER_NOT_PLACED", "3 SL placements never reached Zerodha")
            return
        if ltp is not None and self._stop_breached(t, ltp):
            return                          # the software stop handles it on the next tick
        row = self.placer.place(t, "SL", L.exit_side(t["side"]), want, "SL", self._sl_limit(t, t["current_sl"]),
                                trigger=t["current_sl"])
        if row and row["broker_order_id"]:
            self.repo.update_trade(t["id"], sl_order_id=row["broker_order_id"])
            self.audit(t["id"], "SL_CREATED", "INFO", {"trigger": t["current_sl"], "qty": want,
                                                        "order_id": row["broker_order_id"]})
            self._activate(t)

    def _activate(self, t: dict) -> None:
        if t["status"] == L.ENTRY_EXECUTED:
            self._transition(t, L.POSITION_ACTIVE, "POSITION_ACTIVE", "INFO",
                             {"sl": t["current_sl"], "software_stop_only": bool(t["sl_software_only"])})

    def _software_only(self, t: dict, event: str, why: str) -> None:
        self.repo.update_trade(t["id"], sl_software_only=1)
        t["sl_software_only"] = 1
        self.audit(t["id"], event, "CRITICAL",
                   {"detail": why, "action": "stop is now enforced by this process on Breeze prices only; "
                                             "the position is unprotected if this process stops"})
        self._activate(t)

    # -- trailing / partial ------------------------------------------------------------------------------
    def _trail(self, t: dict, ltp: float) -> None:
        buy = t["side"] == "BUY"
        best = t["best_price"] if t["best_price"] is not None else (t["entry_avg_price"] or t["entry_price"])
        best = max(best, ltp) if buy else min(best, ltp)
        if best != t["best_price"]:
            self.repo.update_trade(t["id"], best_price=best)
            t["best_price"] = best
        v = t["trail_value"]
        cand = (best - v if t["trail_type"] == "POINTS" else best * (1 - v / 100)) if buy else \
               (best + v if t["trail_type"] == "POINTS" else best * (1 + v / 100))
        cand = round_to_tick(cand, t["tick_size"], "SELL" if buy else "BUY")    # BUY: round down, SELL: up
        step = t["trail_step"] or t["tick_size"]
        old = t["current_sl"]
        if (buy and cand >= old + step - 1e-9) or (not buy and cand <= old - step + 1e-9):
            self.repo.update_trade(t["id"], current_sl=cand)
            t["current_sl"] = cand
            self.audit(t["id"], "TRAILING_SL_UPDATED", "INFO", {"old_sl": old, "new_sl": cand, "best": best, "ltp": ltp})

    def _partial(self, t: dict, q: dict, ltp: float, now: datetime) -> None:
        parts = [o for o in q["orders"] if o["kind"] == "PARTIAL"]
        if any(is_working(o["status"]) for o in parts):
            return
        done = [o for o in parts if o["filled_qty"]]
        if done:
            self.repo.update_trade(t["id"], partial_done=1)
            self.audit(t["id"], "PARTIAL_BOOKED", "INFO",
                       {"qty": sum(o["filled_qty"] for o in done), "avg_price": done[-1]["avg_price"]})
            return
        if len(parts) >= 3 or not self._reached(t, ltp, t["partial_price"]):
            return
        pq = t["partial_qty"]
        if pq >= q["open"]:
            self.audit(t["id"], "PARTIAL_SKIPPED", "WARNING", {"partial_qty": pq, "open_qty": q["open"]})
            self.repo.update_trade(t["id"], partial_done=1)
            return
        # Shrink the SL order FIRST: briefly under-protected is safe, over-exiting (SL + partial) is not.
        sl = next((o for o in q["orders"] if o["kind"] == "SL" and is_working(o["status"])), None)
        if sl is not None:
            if sl["status"] in LOCAL_PENDING or sl["cancel_requested"]:
                return
            if not self.placer.modify(sl, quantity=sl["filled_qty"] + q["open"] - pq):
                return
        price = self._marketable(t, ltp, L.exit_side(t["side"]))
        self.audit(t["id"], "PARTIAL_TRIGGERED", "INFO", {"ltp": ltp, "partial_price": t["partial_price"], "qty": pq})
        self.placer.place(t, "PARTIAL", L.exit_side(t["side"]), pq, "LIMIT", price, purpose="PARTIAL_BOOKING")
        q["orders"] = self.repo.orders(t["id"])
        self._resync = True

    def _manual_partial(self, t: dict, q: dict, ltp, now: datetime) -> None:
        """A user-requested partial exit (confirm_partial_exit), placed the next tick after a fresh broker
        snapshot - never synchronously in the request handler. Same PARTIAL order kind and shrink-the-SL-
        first sequencing as the automatic partial_price booking above; unlike that one, this can be repeated
        (each request is independent, not a one-shot flag) as long as the previous PARTIAL order has
        resolved and there is still enough open quantity left."""
        pq = t["pending_partial_qty"]
        if any(o["kind"] == "PARTIAL" and is_working(o["status"]) for o in q["orders"]):
            return                          # wait for the last PARTIAL order (manual or automatic) to resolve
        if pq >= q["open"]:
            self.repo.update_trade(t["id"], pending_partial_qty=None)
            self.audit(t["id"], "PARTIAL_EXIT_SKIPPED", "WARNING",
                      {"requested": pq, "open_qty": q["open"], "detail": "requested >= open quantity; use Exit"})
            return
        sl = next((o for o in q["orders"] if o["kind"] == "SL" and is_working(o["status"])), None)
        if sl is not None:
            if sl["status"] in LOCAL_PENDING or sl["cancel_requested"]:
                return
            if not self.placer.modify(sl, quantity=sl["filled_qty"] + q["open"] - pq):
                return
        price = self._marketable(t, ltp, L.exit_side(t["side"]))
        if price is None:
            return                          # no Breeze price yet; retried next tick, pending_partial_qty stays set
        self.audit(t["id"], "PARTIAL_EXIT_TRIGGERED", "INFO", {"ltp": ltp, "qty": pq, "reason": "user requested"})
        row = self.placer.place(t, "PARTIAL", L.exit_side(t["side"]), pq, "LIMIT", price, purpose="USER_PARTIAL_EXIT")
        if row is not None:
            self.repo.update_trade(t["id"], pending_partial_qty=None)
            self._resync = True

    def _reprice_working(self, t: dict, q: dict, ltp, now: datetime, kinds: tuple) -> None:
        for o in q["orders"]:
            if o["kind"] not in kinds or not is_working(o["status"]) or not o["broker_order_id"] or o["cancel_requested"]:
                continue
            last = datetime.fromisoformat(o["last_priced_at"] or o["created_at"])
            if (now - last).total_seconds() < self.cfg.exit_reprice_s or ltp is None:
                continue
            if o["reprices"] >= self.cfg.max_exit_reprices:
                if o["reprices"] == self.cfg.max_exit_reprices:
                    self.audit(t["id"], "EXIT_NOT_FILLING", "CRITICAL",
                               {"order_id": o["broker_order_id"], "reprices": o["reprices"], "ltp": ltp})
                    self.repo.update_order(o["id"], reprices=o["reprices"] + 1)
                continue
            price = self._marketable(t, ltp, o["side"])
            if self.placer.modify(o, price=price):
                self.repo.update_order(o["id"], reprices=o["reprices"] + 1)

    # -- helpers -----------------------------------------------------------------------------------------
    def _mark(self, t: dict, q: dict, ltp, now: datetime) -> None:
        if ltp is None:
            return
        upd = {"last_ltp": ltp, "last_ltp_at": now.isoformat(timespec="seconds")}
        if q["entry_avg"] is not None:      # nothing has filled yet: still show the market price, just no P&L
            upd["unrealized_pnl"] = round(L.direction(t["side"]) * q["open"] * (ltp - q["entry_avg"]), 2)
        self.repo.update_trade(t["id"], **upd)
        t.update(upd)

    def _mark_kite_ltp(self, t: dict, snap: Snapshot) -> None:
        """Kite's own last_price for this position (free, from positions()), display-only for the
        Active-trades table. None in PAPER (PaperExchange carries no such field) and before an entry has
        an actual Kite position - callers fall back to the Breeze `last_ltp` already shown."""
        px = snap.kite_ltp(t["exchange"], t["tradingsymbol"], t["product"])
        if px != t.get("kite_ltp"):
            self.repo.update_trade(t["id"], kite_ltp=px)
            t["kite_ltp"] = px

    def _price_age(self, t: dict, now: datetime) -> float:
        """How fresh the price must be for this trade. Fast only while a rule acts on it; a trade whose only
        rule is the SL resting at Zerodha is priced slowly (display + gap check), saving Breeze calls."""
        fast = (t["pending_exit_reason"] or t["sl_software_only"] or t["stop_breached_at"] or t["target"] is not None
                or t["trail_enabled"] or (t["partial_enabled"] and not t["partial_done"]))
        return self.cfg.quote_ttl_s if fast else self.cfg.quote_slow_s

    def _time_exit_reason(self, t: dict, now: datetime) -> str | None:
        if t["auto_exit_at"] and now >= datetime.fromisoformat(t["auto_exit_at"]):
            return L.AUTO_EXIT
        # BTST is an app-level "carry overnight" label (Zerodha has no BTST product for F&O): its trades
        # skip the global intraday square-off and simply stay open, resumed by this engine on restart same
        # as any other open trade, until their own auto-exit time, SL, target or a manual exit closes them.
        if t.get("order_type") == "BTST":
            return None
        square_off = self.cfg.session_for(t["underlying"])[2]
        if square_off and now.time() >= square_off:
            return L.SQUARE_OFF
        return None

    @staticmethod
    def _stop_breached(t: dict, ltp: float) -> bool:
        return ltp <= t["current_sl"] if t["side"] == "BUY" else ltp >= t["current_sl"]

    @staticmethod
    def _reached(t: dict, ltp: float, level: float) -> bool:
        return ltp >= level if t["side"] == "BUY" else ltp <= level

    @staticmethod
    def _sl_reason(t: dict) -> str:
        base = t["user_sl"] if t.get("user_sl") is not None else t["initial_sl"]
        return L.TRAILING_SL_HIT if abs(t["current_sl"] - base) > 1e-9 else L.STOP_LOSS_HIT

    def _sl_limit(self, t: dict, trigger: float) -> float:
        side = L.exit_side(t["side"])
        k = 1 + self.cfg.sl_limit_buffer_pct / 100 if side == "BUY" else 1 - self.cfg.sl_limit_buffer_pct / 100
        return round_to_tick(trigger * k, t["tick_size"], side)

    def _marketable(self, t: dict, ltp, side: str) -> float | None:
        ref = ltp or t["last_ltp"]
        if not ref:
            return None
        k = 1 + self.cfg.exit_buffer_pct / 100 if side == "BUY" else 1 - self.cfg.exit_buffer_pct / 100
        return round_to_tick(ref * k, t["tick_size"], side)

    def _ltp_safe(self, inst, max_age: float | None = None) -> float | None:
        try:
            return self.quotes.ltp(inst, max_age=max_age)
        except Exception as exc:
            self.repo.set_status_value("last_error", f"price {inst.tradingsymbol}: {exc}")
            return None

    def _expire_unconfirmed(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self.cfg.confirm_token_s)
        for t in self.repo.trades([L.DRAFT, L.READY]):
            if datetime.fromisoformat(t["created_at"]) < cutoff:
                self.repo.set_status(t["id"], L.EXPIRED, "NOT_CONFIRMED", expect=[L.DRAFT, L.READY])

    def _daily_limits(self, now: datetime) -> None:
        pnl = daily_pnl(self.repo.closed_on(now.date()), self.repo.trades(L.OPEN_STATUSES))
        self.repo.set_status_value("daily_pnl", pnl)
        hit = None
        if self.cfg.max_daily_loss and pnl <= -self.cfg.max_daily_loss:
            hit = f"max daily loss reached (₹{pnl:,.0f})"
        elif self.cfg.max_daily_profit and pnl >= self.cfg.max_daily_profit:
            hit = f"max daily profit reached (₹{pnl:,.0f})"
        if not hit:
            return
        self.halt(hit, daily=True)
        if self.cfg.square_off_on_daily_limit:
            for t in self.repo.trades(L.LIVE_STATUSES):
                if not t["pending_exit_reason"] and t["open_qty"] > 0:
                    self.repo.update_trade(t["id"], pending_exit_reason=L.DAILY_LIMIT)
                    self.audit(t["id"], "EXIT_TRIGGERED", "WARNING", {"reason": L.DAILY_LIMIT, "detail": hit})
                    self._resync = True

    def _get(self, trade_id: int) -> dict:
        t = self.repo.trade(trade_id)
        if t is None:
            raise ActionError(f"no trade {trade_id}")
        return t

    @staticmethod
    def _request_of(t: dict) -> TradeRequest:
        return TradeRequest(underlying=t["underlying"], expiry=date.fromisoformat(t["expiry"]), strike=t["strike"],
                            option_type=t["option_type"], side=t["side"], lots=t["lots"], entry_price=t["entry_price"],
                            stop_loss=t["initial_sl"], target=t["target"], product=t["product"])

    # -- views -------------------------------------------------------------------------------------------
    def _summary(self, t: dict, ltp) -> dict:
        return {"instrument": t["underlying"], "exchange": t["exchange"], "tradingsymbol": t["tradingsymbol"],
                "expiry": t["expiry"], "strike": t["strike"], "option_type": t["option_type"], "side": t["side"],
                "product": t["product"], "lots": t["lots"], "lot_size": t["lot_size"], "quantity": t["quantity"],
                "entry_price": t["entry_price"], "order_value": round(t["entry_price"] * t["quantity"], 2),
                "stop_loss": t["initial_sl"], "target": t["target"],
                "max_loss_at_sl": round(abs(t["entry_price"] - t["initial_sl"]) * t["quantity"], 2),
                "reward_at_target": round(abs(t["target"] - t["entry_price"]) * t["quantity"], 2) if t["target"] else None,
                "trailing": {"type": t["trail_type"], "value": t["trail_value"], "step": t["trail_step"]}
                if t["trail_enabled"] else None,
                "partial": {"lots": t["partial_lots"], "qty": t["partial_qty"], "price": t["partial_price"]}
                if t["partial_enabled"] else None,
                "auto_exit_at": t["auto_exit_at"], "square_off_time": self.cfg.square_off_time.strftime("%H:%M")
                if self.cfg.square_off_time else None, "ltp": ltp, "mode": self.cfg.mode}

    def refresh_ltp(self, trade_id: int) -> dict:
        """On-demand LTP refresh for one open trade (e.g. the Active Strategies table's own refresh button) -
        always bypasses the quote-interval cache (max_age=0, same as contract()/spot()'s force=True), with
        no click-count limit of our own; Breeze's own daily budget is the only real ceiling, same as any
        other explicit "get me a price now" action in this app. Display only (last_ltp/unrealized_pnl) -
        SL/target/trailing decisions still run on the monitor's own regular tick, never on this.

        Also re-fetches the broker's own snapshot to update kite_ltp: the table displays
        `kite_ltp ?? last_ltp`, so if kite_ltp is set (even to a value gone stale because the regular
        tick's own snapshot call is failing/timing out), refreshing only last_ltp would be invisible -
        the click has to move whichever field the table is actually showing.

        The two network calls (Breeze quote, broker snapshot) run WITHOUT self.lock held: refreshing
        several legs at once fires one of these per leg concurrently, and holding the lock across a slow
        network round-trip would queue them up behind each other one at a time (4 legs x ~2s each = ~8s
        for what should overlap). Only the trade-state read/write is done under the lock, and briefly -
        re-checking the trade's status after the network calls in case it closed while we were fetching."""
        with self.lock:
            t = self._get(trade_id)
            if t["status"] not in L.OPEN_STATUSES:
                raise ActionError(f"trade is {t['status']}; nothing to refresh")
            inst = self.instruments.by_symbol(t["exchange"], t["tradingsymbol"])
        ltp = self._ltp_safe(inst, max_age=0)
        try:
            snap = self.broker.snapshot(self.clock())
        except Exception as exc:
            snap, snap_exc = None, exc
        else:
            snap_exc = None
        with self.lock:
            t = self._get(trade_id)
            if t["status"] in L.OPEN_STATUSES:
                if ltp is not None:
                    q = self._derive(t)
                    self._mark(t, q, ltp, self.clock())
                if snap_exc is not None:
                    self._broker_error("snapshot", snap_exc)
                elif snap is not None:
                    self._mark_kite_ltp(t, snap)
            return self.trade_view(trade_id)

    def trade_view(self, trade_id: int) -> dict:
        t = self.repo.trade(trade_id)
        return self._view(t)

    def _view(self, t: dict) -> dict:
        filled = t["filled_qty"] or 0
        cost = (t["entry_avg_price"] or 0) * filled
        total = (t["realized_pnl"] or 0) + (t["unrealized_pnl"] or 0 if t["status"] in L.OPEN_STATUSES else 0)
        v = dict(t)
        v.pop("confirm_token", None)
        v["pnl"] = round(total, 2)
        v["pnl_pct"] = round(total / cost * 100, 2) if cost else None
        v["reconcile_info"] = json.loads(t["reconcile_info"]) if t["reconcile_info"] else None
        if t["entry_time"] and t["exit_time"]:
            v["duration_s"] = (datetime.fromisoformat(t["exit_time"]) - datetime.fromisoformat(t["entry_time"])).total_seconds()
        return v

    def dashboard(self) -> dict:
        with self.lock:
            active = [self._view(t) for t in self.repo.trades(L.OPEN_STATUSES)]
            done = [self._view(t) for t in self.repo.trades(L.TERMINAL - {L.EXPIRED}, limit=100)]
            st = self.repo.status_values()
            return {"mode": self.cfg.mode, "live": self.cfg.live, "now": self.repo.now(), "active": active,
                    "completed": done, "halted": self.halted(),
                    "system": {k: v for k, v in st.items() if k != "halt"},
                    "quotes": self.quotes.status(), "broker": self.broker.name,
                    "risk_limits": {"max_open_trades": self.cfg.max_open_trades,
                                    "max_trades_per_day": self.cfg.max_trades_per_day,
                                    "max_daily_loss": self.cfg.max_daily_loss,
                                    "max_daily_profit": self.cfg.max_daily_profit,
                                    "max_loss_per_trade": self.cfg.max_loss_per_trade,
                                    "max_lots_per_trade": self.cfg.max_lots_per_trade,
                                    "max_qty_per_trade": self.cfg.max_qty_per_trade,
                                    "trading_window": [self.cfg.trading_start.strftime("%H:%M") if self.cfg.trading_start else None,
                                                       self.cfg.trading_end.strftime("%H:%M") if self.cfg.trading_end else None],
                                    "square_off_time": self.cfg.square_off_time.strftime("%H:%M") if self.cfg.square_off_time else None,
                                    "mcx": [x.strftime("%H:%M") if x else None for x in self.cfg.session_for("CRUDEOIL")]},
                    "trades_today": self.repo.confirmed_on(self.clock().date())}

    def meta(self) -> dict:
        """Every configured underlying's expiries/lot size, for the page's dropdowns on load. Deliberately
        NOT under self.lock: the first call of the day for an exchange not yet cached (e.g. BFO, if nothing
        has touched SENSEX yet) loads and parses that exchange's whole instrument dump - measurably slow,
        and holding the service-wide lock for it would stall the live monitor tick (SL/target checks on
        real open positions) for however long that load takes. instruments/self.cfg are read-only here."""
        out = {}
        for u in self.cfg.underlyings:
            try:
                exps = self.instruments.expiries(u)
                lot = None
                if exps:
                    strikes = self.instruments.strikes(u, exps[0])
                    if strikes:
                        lot = self.instruments.resolve(u, exps[0], strikes[len(strikes) // 2], "CE").lot_size
                out[u] = {"exchange": exchange_for(u), "expiries": [e.isoformat() for e in exps], "lot_size": lot}
            except Exception as exc:
                out[u] = {"exchange": exchange_for(u), "error": str(exc)}
        return {"underlyings": out, "mode": self.cfg.mode, "default_product": self.cfg.product}

    def spot(self, underlying: str, with_ltp: bool = False, force: bool = False) -> dict:
        """The underlying index's own LTP (display only, e.g. the "NIFTY 22810" banner) - a Breeze call only
        when asked, same as contract()'s "Get LTP". Never used for any trading decision.
        force=True (an explicit refresh click) bypasses the quote_ttl_s cache with max_age=0, so a click
        right after another one still gets a real Breeze call instead of silently returning the same
        cached price - see market.py's BreezeQuotes.ltp() for what max_age=0 does.

        Deliberately does NOT take self.lock: this is a pure lookup (no trade/order state touched) and the
        Breeze call is the slow part (network round-trip) - self.repo and self.quotes each guard their own
        state internally, so holding the service-wide lock here would only serialize unrelated legs'
        concurrent "refresh price" clicks behind each other for no reason (see market.py's _quote())."""
        px = None
        note = self._no_price_source(underlying)
        if note:
            return {"underlying": underlying, "spot": None, "error": note}
        if with_ltp and hasattr(self.quotes, "spot"):
            try:
                px = self.quotes.spot(underlying, max_age=0 if force else self.cfg.quote_ttl_s)
            except Exception as exc:
                self.repo.set_status_value("last_error", f"spot {underlying}: {exc}")
        return {"underlying": underlying, "spot": px}

    def _no_price_source(self, underlying: str) -> str | None:
        """Why this underlying can have no price at all with the configured source (else None)."""
        if exchange_for(underlying) == "MCX" and getattr(self.quotes, "name", "") not in ("kite", "manual"):
            return "MCX prices come only from Kite: set MARKET_DATA_PROVIDER=KITE in .env and restart"
        return None

    def contract(self, underlying: str, expiry: str, strike: float, option_type: str, with_ltp: bool = False,
                force: bool = False) -> dict:
        """Contract details; the Breeze price only when asked (the form's "Get LTP" button), not on every
        change. force=True: see spot()'s docstring - an explicit refresh always gets a fresh Breeze call.
        No self.lock either, for the same reason as spot() - see its docstring."""
        inst = self.instruments.resolve(underlying, date.fromisoformat(expiry), strike, option_type)
        note = self._no_price_source(underlying)
        return {"tradingsymbol": inst.tradingsymbol, "exchange": inst.exchange, "lot_size": inst.lot_size,
                "tick_size": inst.tick_size, "price_error": note,
                "ltp": self._ltp_safe(inst, max_age=0 if force else self.cfg.quote_ttl_s)
                if with_ltp and not note else None}
