"""Zerodha (Kite Connect) execution adapter.

Depends only on the neutral `strategy_signals` package and the standard library;
`kiteconnect` is imported lazily (zerodha.auth.new_kite) so everything else,
including paper trading and the tests, works without it installed.
"""
from .broker import KiteBroker, OrderRequest, OrderStatus, PaperBroker
from .config import ZerodhaConfig
from .executor import ExecutionReport, Executor
from .instruments import Instrument, InstrumentBook
from .margin import MarginCheck, check_margin, estimate_basket_margin
from .orders import OrderFailed, OrderManager

__all__ = ["ZerodhaConfig", "KiteBroker", "PaperBroker", "OrderRequest", "OrderStatus", "Executor",
           "ExecutionReport", "Instrument", "InstrumentBook", "MarginCheck", "check_margin",
           "estimate_basket_margin", "OrderFailed", "OrderManager"]
