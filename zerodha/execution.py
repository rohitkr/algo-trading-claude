"""ExecutionBroker adapter (strategy_signals.execution) over the existing Zerodha Executor.

The same adapter serves PAPER (over PaperBroker) and LIVE (over KiteBroker), so
paper trading exercises the real order path: basket-margin check, hedge-first
leg ordering, freeze-quantity slicing, marketable-limit pricing, fill waiting,
re-pricing, and unwinding on rejections or partial fills.

`build_live_broker` is the ONLY place a KiteBroker that can send real orders is
created, and it refuses unless every live-trading switch is on.
"""
from __future__ import annotations

import logging

from strategy_signals import OrderIntent
from strategy_signals.execution import ExecutionResult, LegFill

from .broker import Broker, KiteBroker, OrderRequest, PriceFn
from .config import ZerodhaConfig
from .executor import Executor
from .instruments import InstrumentBook
from .orders import Fill

log = logging.getLogger("zerodha.execution")


class LiveTradingNotEnabled(RuntimeError):
    pass


class ZerodhaExecutionBroker:
    def __init__(self, broker: Broker, book: InstrumentBook, cfg: ZerodhaConfig, *, live: bool, name: str = ""):
        if live and not isinstance(broker, KiteBroker):
            raise ValueError("live=True needs a KiteBroker")
        if not live and isinstance(broker, KiteBroker):
            raise ValueError("a KiteBroker can only be used through build_live_broker")
        self.broker, self.book, self.cfg, self.live = broker, book, cfg, live
        self.name = name or ("zerodha-live" if live else "zerodha-paper")
        self.executor = Executor(broker, book, cfg)

    # -- ExecutionBroker ----------------------------------------------------------------------
    def execute(self, intent: OrderIntent) -> ExecutionResult:
        roles = {l.key: l.role.value for l in intent.legs}
        try:
            rep = self.executor.handle(intent)
            self.executor.positions.pop(intent.position_id, None)   # the caller's book drives exits
        except (KeyError, ValueError) as exc:        # unknown contract / bad quantity: nothing was sent
            return ExecutionResult(intent.intent_id, False, message=f"not sent: {exc}")
        except Exception as exc:                     # network / broker error mid-way: state unknown
            log.exception("broker error on %s", intent.intent_id)
            return ExecutionResult(intent.intent_id, False, uncertain=True,
                                   message=f"broker error ({type(exc).__name__}: {exc}); reconcile before retrying")
        res = ExecutionResult(intent.intent_id, rep.ok, [self._leg(f, roles) for f in rep.fills],
                              [self._leg(f, roles) for f in rep.unwound], rep.message,
                              margin_required=rep.margin.required if rep.margin else None,
                              margin_available=rep.margin.available if rep.margin else None,
                              plan=[f"{p.step}. {p.side} {p.tradingsymbol} x{p.quantity} ({p.role})" for p in rep.plan])
        if rep.ok and rep.dry_run:                   # KITE_DRY_RUN=1: nothing was placed, so nothing opened
            res.ok, res.message = False, "KITE_DRY_RUN=1: plan only, no orders sent"
        return res

    def positions(self) -> dict:
        out = {}
        for sym, qty in self.broker.positions().items():
            try:
                i = self.book.by_symbol(sym)
                out[(i.name, i.expiry, i.strike, i.right)] = out.get((i.name, i.expiry, i.strike, i.right), 0) + qty
            except KeyError:
                out[("UNKNOWN", sym)] = qty          # not an option this book knows: reported, never traded
        return out

    def open_orders(self) -> list[dict]:
        return self.broker.open_orders()

    def available_margin(self) -> float:
        return self.broker.available_margin()

    def required_margin(self, intent: OrderIntent) -> float:
        reqs = []
        for l in intent.hedges + intent.mains:
            inst = self.book.option(l.underlying, l.expiry, l.strike, l.right.value)
            reqs.append(OrderRequest(inst.tradingsymbol, l.side.value, l.quantity, inst.exchange, self.cfg.product,
                                     self.cfg.order_type, self.executor.orders.limit_price(inst, l.side.value),
                                     self.cfg.tag))
        return self.broker.basket_margin(reqs)

    # -- helpers ----------------------------------------------------------------------------
    def _leg(self, f: Fill, roles: dict) -> LegFill:
        i = self.book.by_symbol(f.tradingsymbol)
        key = (i.name, i.expiry, i.strike, i.right)
        return LegFill(key, f.tradingsymbol, f.side, f.quantity, f.average_price, roles.get(key, "MAIN"),
                       [f.order_id], [f.status])


def build_live_broker(cfg: ZerodhaConfig, *, trading_mode: str, enable_live_trading: bool, cli_confirmed: bool,
                      price_fn: PriceFn, kite=None, book: InstrumentBook | None = None) -> ZerodhaExecutionBroker:
    """A ZerodhaExecutionBroker that places REAL orders. Every switch must be on:
    TRADING_MODE=LIVE, ENABLE_LIVE_TRADING=true, KITE_DRY_RUN=0 and the --live CLI flag."""
    missing = [name for name, ok in (("TRADING_MODE=LIVE", trading_mode == "LIVE"),
                                     ("ENABLE_LIVE_TRADING=true", enable_live_trading),
                                     ("KITE_DRY_RUN=0", not cfg.dry_run),
                                     ("--live flag", cli_confirmed)) if not ok]
    if missing:
        raise LiveTradingNotEnabled("live trading is not enabled; missing: " + ", ".join(missing))
    if kite is None:
        from .auth import connected_kite
        kite = connected_kite(cfg)
    book = book or InstrumentBook.from_kite(kite)
    return ZerodhaExecutionBroker(KiteBroker(kite, price_fn=price_fn), book, cfg, live=True)
