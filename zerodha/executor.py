"""Turns broker-agnostic OrderIntents into Zerodha orders.

Leg ordering protects against ever being naked-short by accident:
  ENTRY: margin check for the whole basket -> BUY hedge wings -> SELL main legs
         (a hedge that does not fill aborts the entry; a main leg that fails
         unwinds whatever was filled, hedges last)
  EXIT:  BUY back main legs -> SELL hedge wings
         (if a main leg cannot be closed the hedges are left in place)
With cfg.dry_run the executor checks margin and returns the plan without
placing any order.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from strategy_signals import Action, OptionLeg, OrderIntent

from .broker import Broker, OrderRequest
from .config import ZerodhaConfig
from .instruments import Instrument, InstrumentBook
from .margin import MarginCheck, check_margin
from .orders import Fill, OrderFailed, OrderManager

log = logging.getLogger("zerodha.executor")


@dataclass
class PlannedOrder:
    step: int
    role: str
    side: str
    tradingsymbol: str
    quantity: int


@dataclass
class ExecutionReport:
    intent_id: str
    action: str
    ok: bool
    dry_run: bool
    plan: list[PlannedOrder] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    unwound: list[Fill] = field(default_factory=list)
    margin: MarginCheck | None = None
    message: str = ""


@dataclass
class OpenLeg:
    leg: OptionLeg
    inst: Instrument
    quantity: int               # filled units, in the leg's entry direction


class Executor:
    def __init__(self, broker: Broker, book: InstrumentBook, cfg: ZerodhaConfig,
                 orders: OrderManager | None = None):
        self.broker, self.book, self.cfg = broker, book, cfg
        self.orders = orders or OrderManager(broker, cfg)
        self.positions: dict[str, list[OpenLeg]] = {}
        self.done: dict[str, ExecutionReport] = {}

    # -- public -------------------------------------------------------------------------
    def handle(self, intent: OrderIntent) -> ExecutionReport:
        if intent.intent_id in self.done:                       # idempotent replays
            return self.done[intent.intent_id]
        rep = self._entry(intent) if intent.action is Action.ENTRY else self._exit(intent)
        if rep.ok or not rep.dry_run:
            self.done[intent.intent_id] = rep
        log.log(logging.INFO if rep.ok else logging.ERROR, "%s %s: %s", intent.intent_id,
                "ok" if rep.ok else "FAILED", rep.message)
        return rep

    # -- entry --------------------------------------------------------------------------
    def _entry(self, intent: OrderIntent) -> ExecutionReport:
        legs = [(l, self._instrument(l)) for l in intent.hedges + intent.mains]   # hedges first
        for l, inst in legs:
            if l.quantity % inst.lot_size:
                raise ValueError(f"{inst.tradingsymbol}: qty {l.quantity} not a multiple of lot {inst.lot_size}")
        plan = [PlannedOrder(i + 1, l.role.value, l.side.value, inst.tradingsymbol, l.quantity)
                for i, (l, inst) in enumerate(legs)]
        rep = ExecutionReport(intent.intent_id, "ENTRY", True, self.cfg.dry_run, plan)
        reqs = [OrderRequest(inst.tradingsymbol, l.side.value, l.quantity, inst.exchange, self.cfg.product,
                             self.cfg.order_type, self.orders.limit_price(inst, l.side.value), self.cfg.tag)
                for l, inst in legs]
        rep.margin = check_margin(self.broker, reqs, self.cfg.margin_buffer_pct)
        if not rep.margin.ok:
            rep.ok, rep.message = False, (f"insufficient margin: need {rep.margin.needed:,.0f} "
                                          f"(incl. {self.cfg.margin_buffer_pct}% buffer), "
                                          f"available {rep.margin.available:,.0f}")
            return rep
        if self.cfg.dry_run:
            rep.message = "dry run: no orders sent"
            return rep

        opened: list[OpenLeg] = []
        for l, inst in legs:
            try:
                fills = self.orders.execute(inst, l.side.value, l.quantity)
            except OrderFailed as exc:
                rep.fills += exc.fills
                filled = sum(f.quantity for f in exc.fills)
                if filled:
                    opened.append(OpenLeg(l, inst, filled))
                rep.unwound = self._unwind(opened)
                rep.ok = False
                rep.message = (f"{l.role.value} leg failed ({exc}); "
                               f"{'no short was sold' if l.role.value == 'HEDGE' else 'position unwound'}")
                return rep
            rep.fills += fills
            opened.append(OpenLeg(l, inst, sum(f.quantity for f in fills)))
        self.positions[intent.position_id] = opened
        rep.message = f"opened {len(opened)} legs"
        return rep

    def _unwind(self, opened: list[OpenLeg]) -> list[Fill]:
        """Reverse filled legs: mains first, hedges last."""
        out: list[Fill] = []
        for ol in sorted(opened, key=lambda o: o.leg.role.value == "HEDGE"):
            try:
                out += self.orders.execute(ol.inst, ol.leg.side.opposite.value, ol.quantity)
            except OrderFailed as exc:
                out += exc.fills
                log.critical("could not unwind %s x%d: %s - CHECK POSITIONS MANUALLY",
                             ol.inst.tradingsymbol, ol.quantity, exc)
        return out

    # -- exit ---------------------------------------------------------------------------
    def _exit(self, intent: OrderIntent) -> ExecutionReport:
        open_legs = self.positions.get(intent.position_id) or [
            OpenLeg(l, self._instrument(l), l.quantity) for l in intent.legs]
        ordered = [o for o in open_legs if o.leg.role.value != "HEDGE"] + \
                  [o for o in open_legs if o.leg.role.value == "HEDGE"]            # mains first, hedges last
        plan = [PlannedOrder(i + 1, o.leg.role.value, o.leg.side.opposite.value, o.inst.tradingsymbol, o.quantity)
                for i, o in enumerate(ordered)]
        rep = ExecutionReport(intent.intent_id, "EXIT", True, self.cfg.dry_run, plan)
        if self.cfg.dry_run:
            rep.message = "dry run: no orders sent"
            return rep
        for i, o in enumerate(ordered):
            try:
                rep.fills += self.orders.execute(o.inst, o.leg.side.opposite.value, o.quantity)
            except OrderFailed as exc:
                rep.fills += exc.fills
                rep.ok = False
                self.positions[intent.position_id] = ordered[i:]
                rep.message = (f"exit of {o.inst.tradingsymbol} failed ({exc}); "
                               f"{'hedges kept open' if o.leg.role.value != 'HEDGE' else 'wing still open'}")
                return rep
        self.positions.pop(intent.position_id, None)
        rep.message = f"closed {len(ordered)} legs"
        return rep

    # -- helpers ------------------------------------------------------------------------
    def _instrument(self, leg: OptionLeg) -> Instrument:
        return self.book.option(leg.underlying, leg.expiry, leg.strike, leg.right.value)

