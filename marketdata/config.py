"""Which market-data source to use, from the root .env (shell variables override it).

| Variable                  | Default | Meaning                                                                  |
|---------------------------|---------|--------------------------------------------------------------------------|
| MARKET_DATA_PROVIDER      | BREEZE  | KITE (KiteTicker WebSocket + Kite REST/historical) or BREEZE (polling)   |
| MARKET_DATA_FALLBACK      | NONE    | KITE only: BREEZE = ask Breeze when neither the ticker nor kite.ltp()     |
|                           |         | has a price (needs today's Breeze session), NONE = no Breeze at all      |
| KITE_TICKER_MODE          | ltp     | ltp / quote / full: KiteTicker packet mode (ltp is enough for prices)    |
| KITE_STALE_SECONDS        | 10      | no WebSocket message (Kite heartbeats every ~1s) for this long = the     |
|                           |         | stream is stale: prices come from REST and the connection is rebuilt    |
| KITE_REST_MIN_INTERVAL    | 1.0     | min seconds between kite.ltp() fallback calls (Kite allows ~1 req/s)     |
| KITE_HISTORICAL_MIN_INTERVAL | 0.35 | min seconds between kite.historical_data() calls (Kite allows 3 req/s)   |

Credentials (KITE_API_KEY, the daily token from `python3 -m zerodha login`) come from zerodha.config.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from zerodha.config import _env_value, read_env_file

PROVIDERS = ("KITE", "BREEZE")
FALLBACKS = ("BREEZE", "NONE")
TICKER_MODES = ("ltp", "quote", "full")


@dataclass(frozen=True)
class MarketDataConfig:
    provider: str = "BREEZE"
    fallback: str = "NONE"
    ticker_mode: str = "ltp"
    stale_s: float = 10.0
    rest_min_interval_s: float = 1.0
    historical_min_interval_s: float = 0.35

    def __post_init__(self):
        if self.provider not in PROVIDERS:
            raise ValueError(f"MARKET_DATA_PROVIDER must be one of {PROVIDERS}, not {self.provider!r}")
        if self.fallback not in FALLBACKS:
            raise ValueError(f"MARKET_DATA_FALLBACK must be one of {FALLBACKS}, not {self.fallback!r}")
        if self.ticker_mode not in TICKER_MODES:
            raise ValueError(f"KITE_TICKER_MODE must be one of {TICKER_MODES}, not {self.ticker_mode!r}")

    @property
    def kite(self) -> bool:
        return self.provider == "KITE"

    @property
    def breeze_fallback(self) -> bool:
        return self.kite and self.fallback == "BREEZE"

    @classmethod
    def from_env(cls, env_file: str | Path | None = ".env", environ: dict | None = None) -> "MarketDataConfig":
        env = dict(read_env_file(env_file)) if env_file else {}
        env.update({k: _env_value(str(v)) for k, v in (os.environ if environ is None else environ).items()})
        g = lambda k, default: (env.get(k) or "").strip() or default  # noqa: E731  (blank = default)
        d = cls()
        return cls(provider=g("MARKET_DATA_PROVIDER", d.provider).upper(),
                   fallback=g("MARKET_DATA_FALLBACK", d.fallback).upper(),
                   ticker_mode=g("KITE_TICKER_MODE", d.ticker_mode).lower(),
                   stale_s=float(g("KITE_STALE_SECONDS", d.stale_s)),
                   rest_min_interval_s=float(g("KITE_REST_MIN_INTERVAL", d.rest_min_interval_s)),
                   historical_min_interval_s=float(g("KITE_HISTORICAL_MIN_INTERVAL", d.historical_min_interval_s)))
