"""Broker seam for the trader: place / modify / cancel plus ONE snapshot call (order book + positions).

The service never asks "did my order fill?" order by order; it reads Zerodha's whole day order book and
net positions once per monitor tick (two Kite calls) and reconciles everything against that. The order
book and positions are the source of truth; local state is only what we expect.

KiteTraderBroker wraps a kiteconnect.KiteConnect (or anything with the same methods: the paper exchange
and the tests' FakeKite), so PAPER runs exactly the adapter code LIVE runs. `build_broker` is the only
place a broker that can reach Zerodha is created, and it refuses unless every live switch is on.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

TERMINAL = {"COMPLETE", "CANCELLED", "REJECTED", "CANCELLED AMO", "EXPIRED"}
# statuses we set ourselves before/without a broker order id
LOCAL_PENDING = {"INTENT", "UNCERTAIN"}
LOCAL_TERMINAL = {"NOT_PLACED"}


def is_terminal(status: str | None) -> bool:
    return (status or "") in TERMINAL or (status or "") in LOCAL_TERMINAL


def is_working(status: str | None) -> bool:
    """At the broker and still able to fill (or not yet known: INTENT/UNCERTAIN count as working)."""
    return not is_terminal(status)


class LiveTradingNotEnabled(RuntimeError):
    pass


@dataclass(frozen=True)
class OrderSpec:
    exchange: str
    tradingsymbol: str
    side: str                    # BUY | SELL
    quantity: int
    product: str                 # MIS | NRML
    order_type: str              # LIMIT | SL | MARKET
    price: float | None = None
    trigger_price: float | None = None
    tag: str = ""

    def kite_params(self) -> dict:
        p = {"exchange": self.exchange, "tradingsymbol": self.tradingsymbol, "transaction_type": self.side,
             "quantity": int(self.quantity), "product": self.product, "order_type": self.order_type,
             "validity": "DAY"}
        if self.order_type in ("LIMIT", "SL"):
            p["price"] = self.price
        if self.order_type in ("SL", "SL-M"):
            p["trigger_price"] = self.trigger_price
        if self.tag:
            p["tag"] = self.tag
        return p


@dataclass
class BrokerOrder:
    order_id: str
    status: str
    tradingsymbol: str
    exchange: str
    side: str
    quantity: int
    filled_qty: int
    avg_price: float
    order_type: str = ""
    product: str = ""
    price: float | None = None
    trigger_price: float | None = None
    tag: str = ""
    tags: tuple = ()
    message: str = ""
    ts: str = ""

    @classmethod
    def from_kite(cls, o: dict) -> "BrokerOrder":
        tags = tuple(o.get("tags") or ())
        return cls(order_id=str(o["order_id"]), status=str(o.get("status") or ""),
                   tradingsymbol=o.get("tradingsymbol", ""), exchange=o.get("exchange", ""),
                   side=o.get("transaction_type", ""), quantity=int(o.get("quantity") or 0),
                   filled_qty=int(o.get("filled_quantity") or 0), avg_price=float(o.get("average_price") or 0.0),
                   order_type=o.get("order_type", ""), product=o.get("product", ""),
                   price=_f(o.get("price")), trigger_price=_f(o.get("trigger_price")),
                   tag=o.get("tag") or "", tags=tags, message=o.get("status_message") or "",
                   ts=str(o.get("order_timestamp") or ""))

    def has_tag(self, tag: str) -> bool:
        return bool(tag) and (self.tag == tag or tag in self.tags)


def _f(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


@dataclass
class Snapshot:
    """Zerodha's view at one moment: today's order book and net positions per (exchange, symbol, product)."""
    orders: list[BrokerOrder]
    positions: dict[tuple[str, str, str], int]
    fetched_at: datetime
    by_id: dict[str, BrokerOrder] = field(default_factory=dict)
    # Kite's own last_price per position, display-only (never used for SL/target/trailing/risk, which stay
    # on Breeze). Free on Kite's Personal plan - positions() carries it regardless of quote-API access.
    # PaperExchange.positions() has no such field, so this is always empty in PAPER: callers fall back to
    # Breeze automatically by treating a missing key as "no Kite price".
    last_price: dict[tuple[str, str, str], float] = field(default_factory=dict)

    def __post_init__(self):
        self.by_id = {o.order_id: o for o in self.orders}

    def find_tag(self, tag: str) -> list[BrokerOrder]:
        return [o for o in self.orders if o.has_tag(tag)]

    def net(self, exchange: str, tradingsymbol: str, product: str) -> int:
        return self.positions.get((exchange, tradingsymbol, product), 0)

    def net_any_product(self, exchange: str, tradingsymbol: str) -> int:
        return sum(q for (e, s, _), q in self.positions.items() if e == exchange and s == tradingsymbol)

    def kite_ltp(self, exchange: str, tradingsymbol: str, product: str) -> float | None:
        return self.last_price.get((exchange, tradingsymbol, product))


class KiteTraderBroker:
    """Quantities: the trader counts UNITS everywhere; Kite counts units on NFO/BFO but LOTS on MCX.
    `units(exchange, tradingsymbol)` gives the units in one Kite quantity step (1 unless MCX), and this
    adapter is the only place that converts: units -> lots on place/modify, lots -> units on the order book
    and positions it reads back."""
    VARIETY = "regular"

    def __init__(self, kite, *, live: bool, name: str = "", units=None):
        self.kite, self.live = kite, live
        self.name = name or ("zerodha-live" if live else "paper")
        self.units = units or (lambda exchange, tradingsymbol: 1)

    def _to_kite(self, exchange: str, tradingsymbol: str, qty: int) -> int:
        u = int(self.units(exchange, tradingsymbol) or 1)
        if qty % u:
            raise ValueError(f"{qty} is not a whole number of {exchange} lots of {u} for {tradingsymbol}")
        return qty // u

    def place(self, spec: OrderSpec) -> str:
        p = spec.kite_params()
        p["quantity"] = self._to_kite(spec.exchange, spec.tradingsymbol, int(spec.quantity))
        return str(self.kite.place_order(variety=self.VARIETY, **p))

    def modify(self, order_id: str, *, quantity: int | None = None, price: float | None = None,
               trigger_price: float | None = None, order_type: str | None = None,
               exchange: str | None = None, tradingsymbol: str | None = None) -> str:
        if quantity is not None:
            if exchange is None or tradingsymbol is None:
                if int(self.units(exchange or "", tradingsymbol or "") or 1) != 1:
                    raise ValueError("modifying a quantity needs the order's exchange and tradingsymbol")
            else:
                quantity = self._to_kite(exchange, tradingsymbol, int(quantity))
        kw = {k: v for k, v in (("quantity", quantity), ("price", price), ("trigger_price", trigger_price),
                                ("order_type", order_type)) if v is not None}
        return str(self.kite.modify_order(variety=self.VARIETY, order_id=order_id, **kw))

    def cancel(self, order_id: str) -> str:
        return str(self.kite.cancel_order(variety=self.VARIETY, order_id=order_id))

    def _units_of(self, exchange: str, tradingsymbol: str) -> int:
        try:
            return int(self.units(exchange, tradingsymbol) or 1)
        except KeyError:                      # not in our instrument list (someone else's MCX position)
            return 1

    def snapshot(self, now: datetime) -> Snapshot:
        orders = []
        for o in (self.kite.orders() or []):
            b = BrokerOrder.from_kite(o)
            u = self._units_of(b.exchange, b.tradingsymbol)
            if u != 1:
                b.quantity, b.filled_qty = b.quantity * u, b.filled_qty * u
            orders.append(b)
        pos: dict[tuple, int] = {}
        last_price: dict[tuple, float] = {}
        for p in (self.kite.positions() or {}).get("net", []):
            k = (p.get("exchange", ""), p.get("tradingsymbol", ""), p.get("product", ""))
            pos[k] = pos.get(k, 0) + int(p.get("quantity") or 0) * self._units_of(k[0], k[1])
            lp = p.get("last_price")           # absent on PaperExchange; present (free) on real Kite
            if lp not in (None, "", 0):
                last_price[k] = float(lp)
        return Snapshot(orders, pos, now, last_price=last_price)

    def ping(self) -> str:
        prof = self.kite.profile() or {}
        return str(prof.get("user_id", "?"))


def build_broker(cfg, *, cli_live: bool, zcfg=None, kite=None, paper_kite=None) -> KiteTraderBroker:
    """PAPER: a KiteTraderBroker over the paper exchange. LIVE: over a real, logged-in KiteConnect, and ONLY
    when TRADER_MODE=LIVE, ENABLE_LIVE_TRADING=true, KITE_DRY_RUN=0 and the --live flag are all set."""
    if cfg.mode != "LIVE":
        if cli_live:
            hint = ""
            if getattr(cfg, "engine_trading_mode", "") == "LIVE":
                hint = (" (.env has TRADING_MODE=LIVE: that is the live/ strategy engine's switch; the trader reads "
                        "TRADER_MODE=LIVE)")
            raise LiveTradingNotEnabled(f"--live given but TRADER_MODE is {cfg.mode}, not LIVE{hint}")
        if paper_kite is None:
            raise ValueError("PAPER mode needs the paper exchange")
        return KiteTraderBroker(paper_kite, live=False)
    from zerodha.config import ZerodhaConfig
    zcfg = zcfg or ZerodhaConfig.from_env()
    missing = [name for name, ok in (("TRADER_MODE=LIVE", cfg.mode == "LIVE"),
                                     ("ENABLE_LIVE_TRADING=true", cfg.enable_live_trading),
                                     ("KITE_DRY_RUN=0", not zcfg.dry_run),
                                     ("--live flag", cli_live)) if not ok]
    if missing:
        raise LiveTradingNotEnabled("live trading is not enabled; missing: " + ", ".join(missing))
    if kite is None:
        from zerodha.auth import connected_kite
        kite = connected_kite(zcfg)
    return KiteTraderBroker(kite, live=True)
