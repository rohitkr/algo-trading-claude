"""Market data sources shared by the trader UI (trader/) and the live engine (live/).

    MARKET_DATA_PROVIDER=KITE    one KiteTicker WebSocket per process (marketdata.kite_stream.KiteStream):
                                 refcounted subscriptions, reconnect + resubscribe, session-expiry and
                                 stale-stream detection, REST kite.ltp() when the stream cannot vouch for
                                 a price, optional Breeze as the last fallback (MARKET_DATA_FALLBACK=BREEZE)
    MARKET_DATA_PROVIDER=BREEZE  the existing Breeze polling (trader.market.BreezeQuotes,
                                 trading_data.breeze.live.BreezeMarketData) - the default

kiteconnect/twisted are imported lazily, so importing this package never needs them.
"""
from .config import MarketDataConfig
from .kite_stream import INDEX_TOKENS, KiteStream, index_token

__all__ = ["MarketDataConfig", "KiteStream", "INDEX_TOKENS", "index_token"]
