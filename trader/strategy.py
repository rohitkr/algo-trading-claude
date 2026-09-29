"""Multi-leg strategies: a strategies row + N ordinary `trades` rows (group_id = the strategy id).

Every leg is placed and managed by the EXACT same engine a single manual trade uses - preview(), confirm(),
its own SL/target/trailing, reconciliation, crash recovery, all of it, completely unchanged. This module
adds exactly one thing on top: strategy-level rules that watch the combined P&L of a group's legs and, when
one fires, set `pending_exit_reason` on each open leg - the SAME field TradeService's own SL/target/
daily-limit exits already set (see `_begin_exit` / `_daily_limits` in service.py). The next tick's ordinary
per-trade processing (`TradeService._process`) takes it from there: broker position verified, resting orders
cancelled, one marketable exit sent. This module never places, modifies or cancels a broker order itself.

Order type (MIS | CNC | BTST) is a workflow label, not a Zerodha product:
  MIS  -> product MIS  (Zerodha's own intraday square-off applies)
  CNC  -> product NRML ("normal" / carry-forward, no special handling here)
  BTST -> product NRML, and TradeService._time_exit_reason skips the GLOBAL TRADER_SQUARE_OFF_TIME for it
          (see service.py). A BTST leg's own per-trade auto_exit_time - or none at all - is what decides
          when it closes, so it carries overnight and is picked back up by the ordinary engine on the next
          day's run: trades already resume across restarts regardless of which day they were opened.

Not implemented here (future work, see trader/README.md "Future: multi-leg"): broker-side atomic multi-leg
entry (hedges first), unwinding partially-placed legs, combined margin, and saved/recurring strategies.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime, time

from zerodha.orders import round_to_tick

from . import lifecycle as L
from .repository import Repository
from .service import ActionError, TradeService

log = logging.getLogger("trader.strategy")

TRAILING_MODES = ("NONE", "LOCK_FIX", "TRAIL", "LOCK_AND_TRAIL")
SL_TP_TYPES = ("POINTS", "PERCENT", "PRICE")


def _time(v) -> time | None:
    v = (v or "").strip() if isinstance(v, str) else v
    return time.fromisoformat(v) if v else None


def _num(v) -> float | None:
    return float(v) if v not in (None, "") else None


@dataclass
class GlobalConfig:
    name: str = ""
    order_type: str = "MIS"                     # MIS | CNC | BTST
    start_time: time | None = None               # informational only in Phase 1: no scheduled/recurring runs yet
    square_off_time: time | None = None          # -> each leg's own auto_exit_time (blank = no auto square-off)
    days: tuple[str, ...] = ()                   # informational only in Phase 1 (future: scheduled runs)
    exit_profit_amount: float | None = None      # combined P&L across all legs
    exit_loss_amount: float | None = None        # positive number; combined P&L <= -this exits
    no_trade_after: time | None = None           # refuses Trade All after this time
    trailing_mode: str = "NONE"                  # NONE | LOCK_FIX | TRAIL | LOCK_AND_TRAIL
    lock_if_profit_reaches: float | None = None
    lock_profit_at: float | None = None
    trail_every_increase: float | None = None
    trail_profit_by: float | None = None
    move_sl_to_cost_enabled: bool = False
    move_sl_to_cost_at: float | None = None       # points of favourable move (per leg) that triggers it

    @classmethod
    def from_json(cls, d: dict) -> "GlobalConfig":
        order_type = str(d.get("order_type") or "MIS").upper()
        if order_type not in ("MIS", "CNC", "BTST"):
            raise ValueError("order_type must be MIS, CNC or BTST")
        trailing_mode = str(d.get("trailing_mode") or "NONE").upper()
        if trailing_mode not in TRAILING_MODES:
            raise ValueError(f"trailing_mode must be one of {TRAILING_MODES}")
        days = tuple(str(x).upper()[:3] for x in (d.get("days") or ()))
        return cls(name=str(d.get("name") or "").strip(), order_type=order_type,
                   start_time=_time(d.get("start_time")), square_off_time=_time(d.get("square_off_time")),
                   days=days, exit_profit_amount=_num(d.get("exit_profit_amount")),
                   exit_loss_amount=_num(d.get("exit_loss_amount")), no_trade_after=_time(d.get("no_trade_after")),
                   trailing_mode=trailing_mode, lock_if_profit_reaches=_num(d.get("lock_if_profit_reaches")),
                   lock_profit_at=_num(d.get("lock_profit_at")), trail_every_increase=_num(d.get("trail_every_increase")),
                   trail_profit_by=_num(d.get("trail_profit_by")),
                   move_sl_to_cost_enabled=bool(d.get("move_sl_to_cost_enabled")),
                   move_sl_to_cost_at=_num(d.get("move_sl_to_cost_at")))

    def to_json(self) -> dict:
        d = asdict(self)
        d["start_time"] = self.start_time.strftime("%H:%M") if self.start_time else None
        d["square_off_time"] = self.square_off_time.strftime("%H:%M") if self.square_off_time else None
        d["no_trade_after"] = self.no_trade_after.strftime("%H:%M") if self.no_trade_after else None
        return d


def _product_for(order_type: str) -> str:
    return "MIS" if order_type == "MIS" else "NRML"


def _resolve_price(entry_price: float, side: str, kind: str, value: float | None, typ: str) -> float | None:
    """kind: 'sl' or 'tp'. typ: POINTS | PERCENT | PRICE. Returns an absolute price, or None.
    BUY: target above entry, stop-loss below. SELL: target below entry, stop-loss above."""
    if value is None:
        return None
    if typ == "PRICE":
        return value
    buy = side == "BUY"
    up = buy if kind == "tp" else not buy
    delta = entry_price * value / 100 if typ == "PERCENT" else value
    return entry_price + delta if up else entry_price - delta


class StrategyService:
    def __init__(self, svc: TradeService, repo: Repository):
        self.svc, self.repo = svc, repo

    # -- create + place every leg ------------------------------------------------------------------
    def create_and_trade(self, payload: dict) -> dict:
        with self.svc.lock:
            try:
                cfg = GlobalConfig.from_json(payload.get("config") or {})
            except ValueError as exc:
                return {"ok": False, "errors": [str(exc)]}
            now = self.svc.clock()
            if cfg.no_trade_after and now.time() >= cfg.no_trade_after:
                return {"ok": False, "errors": [f"past this strategy's no-trade-after time ({cfg.no_trade_after:%H:%M})"]}
            legs_in = payload.get("legs") or []
            if not legs_in:
                return {"ok": False, "errors": ["at least one leg is required"]}
            if len(legs_in) > 12:
                return {"ok": False, "errors": ["too many legs (12 max)"]}
            # BUY legs go in before SELL legs (margin: a short's margin requirement drops once its hedge is
            # already on) - the order the user arranged them in the UI is kept within each side.
            legs_in = sorted(legs_in, key=lambda l: 0 if str(l.get("side") or "").upper() == "BUY" else 1)

            auto_exit = cfg.square_off_time.strftime("%H:%M") if cfg.square_off_time else None
            previews = []
            for i, leg in enumerate(legs_in, 1):
                try:
                    req = self._leg_payload(leg, cfg, auto_exit)
                except (KeyError, ValueError) as exc:
                    return {"ok": False, "errors": [f"leg {i}: {exc}"]}
                p = self.svc.preview(req)           # preview only STORES a DRAFT row; nothing is placed yet
                if not p["ok"]:
                    tag = f"{req.get('side')} {req.get('option_type')} {req.get('strike')}"
                    errs = [f"leg {i} ({tag}): {e}" for e in p["errors"]]
                    failed = [f"leg {i} ({tag}): {c['name']} ({c['detail']})"
                              for c in (p.get("risk") or []) if not c["passed"]]
                    return {"ok": False, "errors": errs + failed}
                previews.append((leg, p))

            sid = self.repo.insert_strategy(dict(mode=self.svc.cfg.mode,
                                                 name=cfg.name or f"Strategy {now:%H:%M:%S}",
                                                 config=json.dumps(cfg.to_json())))
            confirmed, failed = [], []
            for leg, p in previews:
                try:
                    self.svc.confirm(p["trade_id"], p["token"])
                    self.repo.update_trade(p["trade_id"], group_id=sid, leg_role=str(leg.get("leg_role") or ""))
                    confirmed.append(p["trade_id"])
                except ActionError as exc:
                    failed.append({"trade_id": p["trade_id"], "error": str(exc)})
            if not confirmed:
                self.repo.update_strategy(sid, status="CANCELLED", exit_reason="every leg failed to confirm")
            self.svc.audit(None, "STRATEGY_CREATED", "WARNING" if failed else "INFO",
                           {"strategy_id": sid, "confirmed": confirmed, "failed": failed})
            return {"ok": bool(confirmed), "strategy_id": sid, "confirmed": confirmed, "failed": failed}

    def _leg_payload(self, leg: dict, cfg: GlobalConfig, auto_exit: str | None) -> dict:
        side = str(leg.get("side") or "").upper()
        if side not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
        entry_price = _num(leg.get("entry_price"))
        if not entry_price or entry_price <= 0:
            raise ValueError("entry price is required")
        inst = self.svc.instruments.resolve(str(leg.get("underlying") or "").upper(),
                                            date.fromisoformat(str(leg.get("expiry"))),
                                            float(leg["strike"]), str(leg.get("option_type") or "").upper())
        qty = (leg.get("lots") or 1) * inst.lot_size
        stop_loss = _resolve_price(entry_price, side, "sl", _num(leg.get("sl_value")), str(leg.get("sl_type") or "POINTS").upper())
        target = _resolve_price(entry_price, side, "tp", _num(leg.get("tp_value")), str(leg.get("tp_type") or "POINTS").upper())
        if stop_loss is None:
            # No per-leg stop was given: the strategy's own combined profit/loss exit covers this leg
            # instead. The engine still needs SOME numeric stop_loss (every trade has one), so one is set
            # here wide enough that it is very unlikely to be the thing that actually exits the leg - capped
            # at 95% of TRADER_MAX_LOSS_PER_TRADE so a leg with no chosen stop still can't exceed the
            # account's own configured per-trade loss limit.
            wide_points = (self.svc.cfg.max_loss_per_trade * 0.95) / qty
            stop_loss = entry_price - wide_points if side == "BUY" else entry_price + wide_points
        stop_loss = round_to_tick(stop_loss, inst.tick_size, "SELL" if side == "BUY" else "BUY")
        if side == "BUY" and stop_loss <= 0:
            raise ValueError("no stop-loss given and the account's max-loss-per-trade limit is too small "
                             "for this quantity to derive a safe wide one - set a stop-loss for this leg")
        if target is not None:
            target = round_to_tick(target, inst.tick_size, "BUY" if side == "BUY" else "SELL")
        return dict(underlying=inst.name, expiry=inst.expiry.isoformat(), strike=inst.strike,
                   option_type=leg.get("option_type"), side=side, lots=leg.get("lots") or 1,
                   entry_price=entry_price, stop_loss=stop_loss, target=target,
                   product=_product_for(cfg.order_type), order_type=cfg.order_type,
                   auto_exit_time=auto_exit)

    # -- per-tick monitoring: combined P&L rules only, nothing per-leg is touched except as noted -------
    def tick(self, now: datetime) -> None:
        for s in self.repo.strategies(status="ACTIVE"):
            try:
                self._tick_one(s, now)
            except Exception:
                log.exception("strategy %s tick failed", s["id"])

    def _tick_one(self, s: dict, now: datetime) -> None:
        cfg = GlobalConfig.from_json(json.loads(s["config"]))
        legs = self.repo.trades_by_group(s["id"])
        if not legs:
            return
        open_legs = [t for t in legs if t["status"] in L.OPEN_STATUSES]
        if cfg.move_sl_to_cost_enabled and cfg.move_sl_to_cost_at:
            for t in open_legs:
                self._move_sl_to_cost(t, cfg)
        if not open_legs:
            self.repo.update_strategy(s["id"], status="DONE", exit_reason=s["exit_reason"] or "all legs closed",
                                      exit_time=self.repo.now())
            return
        combined = round(sum((t["realized_pnl"] or 0) for t in legs)
                         + sum((t["unrealized_pnl"] or 0) for t in open_legs), 2)
        reason = None
        if cfg.exit_profit_amount is not None and combined >= cfg.exit_profit_amount:
            reason = "STRATEGY_PROFIT_TARGET"
        elif cfg.exit_loss_amount is not None and combined <= -abs(cfg.exit_loss_amount):
            reason = "STRATEGY_LOSS_LIMIT"
        if reason is None and cfg.trailing_mode != "NONE":
            reason = self._trail(s, cfg, combined)
        if reason:
            self._exit_all(s, open_legs, reason)

    def _move_sl_to_cost(self, t: dict, cfg: GlobalConfig) -> None:
        """Once a leg has moved `move_sl_to_cost_at` points in its own favour, pull its stop up/down to
        break-even. Mirrors the direction-only-tightens rule the per-leg trailing SL already uses."""
        if t["last_ltp"] is None or t["entry_avg_price"] is None:
            return
        buy = t["side"] == "BUY"
        moved = (t["last_ltp"] - t["entry_avg_price"]) if buy else (t["entry_avg_price"] - t["last_ltp"])
        if moved < cfg.move_sl_to_cost_at:
            return
        cost = t["entry_avg_price"]
        tightens = cost > t["current_sl"] if buy else cost < t["current_sl"]
        if not tightens:
            return
        self.repo.update_trade(t["id"], current_sl=cost, user_sl=cost, stop_breached_at=None)
        self.svc.audit(t["id"], "SL_MOVED_TO_COST", "INFO",
                       {"cost": cost, "trigger_points": cfg.move_sl_to_cost_at, "moved": round(moved, 2)})

    def _trail(self, s: dict, cfg: GlobalConfig, combined: float) -> str | None:
        """Trailing stop on the GROUP's combined P&L (rupees), same step logic as the per-leg price
        trailing in service.py._trail, just applied to a P&L number instead of an option price."""
        best = max(s["best_pnl"], combined) if s["best_pnl"] is not None else combined
        locked = s["locked_pnl"]
        if cfg.trailing_mode in ("LOCK_FIX", "LOCK_AND_TRAIL") and locked is None:
            if cfg.lock_if_profit_reaches is not None and best >= cfg.lock_if_profit_reaches:
                locked = cfg.lock_profit_at
        if cfg.trailing_mode in ("TRAIL", "LOCK_AND_TRAIL") and cfg.trail_every_increase and cfg.trail_profit_by is not None \
                and cfg.trail_every_increase > 0:
            steps = int(best // cfg.trail_every_increase)
            if steps > 0:
                trailed = steps * cfg.trail_profit_by
                locked = trailed if locked is None else max(locked, trailed)
        if best != s["best_pnl"] or locked != s["locked_pnl"]:
            self.repo.update_strategy(s["id"], best_pnl=best, locked_pnl=locked)
        if locked is not None and combined <= locked:
            return "STRATEGY_TRAIL_STOP"
        return None

    def _exit_all(self, s: dict, open_legs: list[dict], reason: str) -> None:
        for t in open_legs:
            if not t["pending_exit_reason"]:
                self.repo.update_trade(t["id"], pending_exit_reason=reason)
                self.svc.audit(t["id"], "EXIT_TRIGGERED", "WARNING", {"reason": reason, "strategy_id": s["id"]})
        self.repo.update_strategy(s["id"], status="DONE", exit_reason=reason, exit_time=self.repo.now())

    # -- manual exit-all (user pressed "Exit strategy") --------------------------------------------
    def exit_all(self, strategy_id: int) -> dict:
        with self.svc.lock:
            s = self.repo.strategy(strategy_id)
            if s is None:
                raise ActionError(f"no strategy {strategy_id}")
            legs = self.repo.trades_by_group(strategy_id)
            open_legs = [t for t in legs if t["status"] in L.OPEN_STATUSES]
            self._exit_all(s, open_legs, "USER_EXIT")
            return self.view(strategy_id)

    # -- views ---------------------------------------------------------------------------------------
    def view(self, strategy_id: int) -> dict:
        s = self.repo.strategy(strategy_id)
        if s is None:
            raise ActionError(f"no strategy {strategy_id}")
        return self._view(s)

    def _view(self, s: dict) -> dict:
        legs = self.repo.trades_by_group(s["id"])
        open_legs = [t for t in legs if t["status"] in L.OPEN_STATUSES]
        combined = round(sum((t["realized_pnl"] or 0) for t in legs)
                         + sum((t["unrealized_pnl"] or 0) for t in open_legs), 2)
        v = dict(s)
        v["config"] = json.loads(s["config"])
        v["legs"] = [self.svc.trade_view(t["id"]) for t in legs]
        v["combined_pnl"] = combined
        v["open_legs"] = len(open_legs)
        return v

    def dashboard(self) -> list[dict]:
        return [self._view(s) for s in self.repo.strategies()]
