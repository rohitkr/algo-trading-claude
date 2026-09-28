"""Manual option trading desk: a local web UI + trade execution service on top of zerodha/.

    web UI (trader/web) -> TradeService (trader/service.py) -> OrderPlacer (idempotent, trader/orders.py)
                                                            -> TraderBroker (Kite or the paper exchange)
    Monitor thread (trader/monitor.py) -> TradeService.tick(): broker sync, reconciliation, SL/target/
                                          trailing/partial/auto-exit
    Every trade, broker order and lifecycle event is persisted in SQLite (trader/repository.py).

See trader/README.md.
"""
