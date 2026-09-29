"""Option prices for monitoring: KiteQuotes (MARKET_DATA_PROVIDER=KITE, the paid Kite Connect plan's WebSocket)
or BreezeQuotes (MARKET_DATA_PROVIDER=BREEZE, the default; Zerodha's free Personal API has no quotes).

KiteQuotes reads the process-wide marketdata.KiteStream: a subscribed instrument's price is the latest tick
(no call, no budget); anything else, or a stale stream, is priced by kite.ltp() over REST, and only then by
the optional Breeze fallback (MARKET_DATA_FALLBACK=BREEZE).

BreezeQuotes reuses trading_data.breeze.client.BreezeClient (same session token file, throttle, retries):
get_quotes for the LTP, and the last completed 1-minute bar (historical_data_v2) when the quote is empty.
The Breeze daily call budget is counted in the trader's own SQLite file (Repository.api_calls_on /
add_api_calls), NOT in data/market_data.duckdb, because DuckDB allows one writer and the live engine /
downloaders hold it. TRADER_BREEZE_DAILY_BUDGET caps this process; keep it + the other scripts' usage
under Breeze's 5,000/day yourself.

ManualQuotes: prices set by hand (tests, and PAPER rehearsals from the UI without a Breeze session).
"""
from __future__ import annotations

import logging
import threading
import time as _time
from datetime import datetime, timedelta
from datetime import time as dtime
from typing import Protocol

from zerodha.instruments import Instrument

log = logging.getLogger("trader.market")


class QuoteSource(Protocol):
    name: str

    def ltp(self, inst: Instrument, max_age: float | None = None) -> float | None: ...
    def spot(self, underlying: str, max_age: float | None = None) -> float | None: ...
    def status(self) -> dict: ...


class ManualQuotes:
    name = "manual"

    def __init__(self):
        self.prices: dict[str, float] = {}
        self._lock = threading.Lock()

    def set(self, tradingsymbol: str, price: float) -> None:
        with self._lock:
            self.prices[tradingsymbol] = float(price)

    def ltp(self, inst: Instrument, max_age: float | None = None) -> float | None:
        return self.prices.get(inst.tradingsymbol)

    def spot(self, underlying: str, max_age: float | None = None) -> float | None:
        return self.prices.get(f"SPOT:{underlying.upper()}")

    def price_for(self, exchange: str, tradingsymbol: str) -> float | None:
        return self.prices.get(tradingsymbol)

    def status(self) -> dict:
        return {"source": self.name, "ok": True, "detail": "prices set by hand (PAPER rehearsal)"}


class BreezeQuotes:
    """Budget-paced Breeze prices. Nothing is fetched unless the service asks (i.e. only for open positions,
    plus one call per preview/confirm and the UI's explicit "Get LTP"). Each symbol is fetched at most once
    per max(requested max_age, pacing interval), where the pacing interval spreads the remaining daily
    budget (minus `reserve` kept for exits) over the rest of the session and the symbols being watched:
        interval = seconds_left_in_session x watched_symbols / (remaining_calls - reserve)
    so the budget cannot run out before the close however many positions are open."""
    name = "breeze"

    def __init__(self, settings, usage_store, daily_budget: int, ttl_s: float = 15.0, client=None,
                 monotonic=_time.monotonic, clock=datetime.now, reserve: int = 100,
                 session_end: dtime = dtime(15, 30)):
        self.settings, self.ttl_s, self.monotonic, self.clock = settings, ttl_s, monotonic, clock
        self._store, self._budget = usage_store, daily_budget
        self._client = client
        self.reserve, self.session_end = reserve, session_end
        self._cache: dict[str, tuple[float, float]] = {}
        self._watched: dict[str, float] = {}           # symbol -> last time a caller asked for it
        self._lock = threading.Lock()
        self.errors = 0
        self.calls = 0
        self.last_error: str | None = None
        self.last_ok: datetime | None = None
        self.last_interval: float | None = None

    def remaining(self) -> int:
        return max(0, self._budget - self._store.api_calls_on(self.clock().date()))

    def pacing_interval(self) -> float:
        now_m = self.monotonic()
        watched = sum(1 for t in self._watched.values() if now_m - t < 300) or 1
        end = datetime.combine(self.clock().date(), self.session_end)
        secs_left = max(0.0, (end - self.clock()).total_seconds())
        spare = self.remaining() - self.reserve
        if spare <= 0:
            return float("inf")                        # only the reserve is left: keep it for exits
        return secs_left * watched / spare

    def client(self):
        if self._client is None:
            from trading_data.breeze.client import BreezeClient
            self._client = BreezeClient(self.settings, self._store, daily_limit=self._budget).connect()
        return self._client

    def _request(self, inst: Instrument):
        from trading_data.breeze.client import HistoricalRequest
        prof = self.settings.options_profile(inst.name)     # stock code + Breeze exchange (NFO / BFO)
        return HistoricalRequest(prof.stock_code, prof.exchange, prof.product_type, expiry=inst.expiry,
                                 right=inst.right.lower(), strike=inst.strike)

    def _spot_request(self, underlying: str):
        from trading_data.breeze.client import HistoricalRequest
        inst = self.settings.instrument(underlying)         # the index's own stock code, e.g. NIFTY / CNXBAN
        return HistoricalRequest(inst.stock_code, inst.exchange, inst.product_type)

    def ltp(self, inst: Instrument, max_age: float | None = None) -> float | None:
        """Cached price if younger than max(max_age or ttl_s, pacing interval); else one Breeze call.
        max_age=0 forces a call when budget allows (previews, exits)."""
        return self._quote(inst.tradingsymbol, lambda: self._request(inst), max_age)

    def spot(self, underlying: str, max_age: float | None = None) -> float | None:
        """The underlying index's own LTP (NIFTY, BANKNIFTY, ...), for display only - never used for any
        trading decision. Same cache/budget/pacing as an option's ltp(), keyed separately so it doesn't
        compete with option quotes for the pacing interval's per-symbol slot."""
        return self._quote(f"SPOT:{underlying.upper()}", lambda: self._spot_request(underlying), max_age)

    def _quote(self, key: str, build_request, max_age: float | None) -> float | None:
        # Bookkeeping (cache/budget/pacing) is shared state and stays under the lock, but the Breeze network
        # call itself does NOT - it used to run inside this lock, which meant every symbol's fetch (e.g. 4
        # legs' worth of "refresh all" clicks) queued up behind one another for the full round-trip each,
        # turning a ~1-2s Breeze call into an 8s wait for 4 legs. Different symbols' calls now overlap; the
        # lock only ever guards the in-memory dict/counter updates, which are effectively instant.
        with self._lock:
            now_m = self.monotonic()
            self._watched[key] = now_m
            hit = self._cache.get(key)
            wanted = self.ttl_s if max_age is None else max_age
            pace = self.pacing_interval() if wanted > 0 else (0.0 if self.remaining() > 0 else float("inf"))
            self.last_interval = max(wanted, pace)
            if hit and now_m - hit[1] < self.last_interval:
                return hit[0]
            if pace == float("inf") and hit:
                return hit[0]
            self.calls += 1
        try:
            req = build_request()
            q = self.client().get_quote(req)
            px = float(q["ltp"]) if q and q.get("ltp") not in (None, "") else None
            if not px or px <= 0:
                with self._lock:
                    self.calls += 1
                px = self._last_bar_close(req)
        except Exception as exc:
            with self._lock:
                self.errors += 1
                self.last_error = f"{key}: {type(exc).__name__}: {exc}"
            log.warning("Breeze price error %s", self.last_error)
            return hit[0] if hit else None     # the stale cached price, if any (caller sees last_ltp_at)
        with self._lock:
            if px and px > 0:
                self._cache[key] = (px, now_m)
                self.last_ok = self.clock()
                self.last_error = None
                return px
            return hit[0] if hit else None

    def _last_bar_close(self, req) -> float | None:
        now = self.clock().replace(second=0, microsecond=0)
        rows = self.client().fetch_candles(req, now - timedelta(minutes=5), now)
        closes = [float(r["close"]) for r in rows if r.get("close") not in (None, "")]
        return closes[-1] if closes else None

    def status(self) -> dict:
        try:
            remaining = self.remaining()
        except Exception:
            remaining = None
        return {"source": self.name, "ok": self.last_error is None,
                "last_ok": self.last_ok.isoformat(timespec="seconds") if self.last_ok else None,
                "errors": self.errors, "last_error": self.last_error, "api_budget_remaining": remaining,
                "calls_this_run": self.calls,
                "interval_s": None if self.last_interval in (None, float("inf")) else round(self.last_interval, 1),
                "budget_exhausted": self.last_interval == float("inf")}


class KiteQuotes:
    """QuoteSource over marketdata.KiteStream. Every instrument asked for is kept subscribed while it keeps
    being asked for (released `idle_s` after the last request), so the monitor's open trades and a leg
    being watched in the UI stream, while a strike browsed once in the form does not stay subscribed.
    max_age is irrelevant for a live stream (a tick is always the current price); it bounds the REST cache."""
    name = "kite"
    OWNER = "quotes"

    def __init__(self, stream, fallback=None, idle_s: float = 300.0, monotonic=_time.monotonic, spot_resolver=None):
        self.stream, self.fallback, self.idle_s, self.monotonic = stream, fallback, idle_s, monotonic
        self.spot_resolver = spot_resolver
        self._asked: dict[int, float] = {}
        self._lock = threading.Lock()
        self.fallback_used = 0

    def _keep(self, token: int) -> None:
        now = self.monotonic()
        with self._lock:
            new = token not in self._asked
            self._asked[token] = now
            idle = [t for t, at in self._asked.items() if now - at > self.idle_s]
            for t in idle:
                del self._asked[t]
        if new:
            self.stream.acquire([token], owner=self.OWNER)
        if idle:
            self.stream.release(idle, owner=self.OWNER)

    def token(self, inst: Instrument) -> int:
        return int(inst.instrument_token)

    def spot_token(self, underlying: str) -> int:
        if self.spot_resolver is not None:        # app wiring: index token, or the MCX future
            return int(self.spot_resolver(underlying))
        from marketdata import index_token
        return index_token(underlying)

    def ltp(self, inst: Instrument, max_age: float | None = None) -> float | None:
        tok = self.token(inst)
        self._keep(tok)
        px = self.stream.ltp(tok, max_age)
        if px is None and self.fallback is not None:
            self.fallback_used += 1
            px = self.fallback.ltp(inst, max_age)
        return px

    def spot(self, underlying: str, max_age: float | None = None) -> float | None:
        tok = self.spot_token(underlying)
        self._keep(tok)
        px = self.stream.ltp(tok, max_age)
        if px is None and self.fallback is not None:
            self.fallback_used += 1
            px = self.fallback.spot(underlying, max_age)
        return px

    def status(self) -> dict:
        st = dict(self.stream.status())
        if self.fallback is not None:
            fb = self.fallback.status()
            st["fallback"] = {"source": fb.get("source"), "ok": fb.get("ok"), "used": self.fallback_used,
                              "api_budget_remaining": fb.get("api_budget_remaining")}
        return st
