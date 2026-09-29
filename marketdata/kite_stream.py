"""One KiteTicker WebSocket per process, shared by everything that needs a live price.

    stream = KiteStream(kite_ticker_factory(cfg), lambda: access_token(cfg), rest_ltp=kite_rest_ltp(kite))
    stream.start()
    stream.hold("positions", {tok1, tok2})     # this owner now needs exactly these instruments
    stream.acquire([tok3], owner="ui:7")        # ... or add / release one at a time
    stream.ltp(tok1)                            # latest tick; REST kite.ltp() when the stream can't vouch
    stream.add_listener(fn)                     # fn({token: price}) on every tick batch (reactor thread)

Subscriptions are refcounted by owner: a token stays subscribed while any owner holds it, and is
unsubscribed when the last one lets go. The last tick is kept after that (a closed position's LTP).

Health: Kite sends a heartbeat about every second, so "no message for KITE_STALE_SECONDS" means the
connection is dead even if the socket looks open. A stale or unconnected stream never vouches for a
price: ltp() then asks kite.ltp() over REST (rate-limited, one batched call for every instrument that
needs it) and only returns the last tick if that fails too.

Reconnect: KiteTicker retries a dropped connection itself and resubscribes what it knew; on_connect
also resubscribes our own refcounted set (the authority - anything added while down is included). A
watchdog thread rebuilds the connection when KiteTicker gives up, the stream goes stale, or the
access token changes (a new `python3 -m zerodha login`). An expired session (Kite resets tokens at
06:00 IST, or a 403 on connect) stops the socket until a valid token appears in the token file.

The transport is injected (kite_ticker_factory for real Kite), so all of this is tested without Kite.
"""
from __future__ import annotations

import logging
import threading
import time as _time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable, Protocol

log = logging.getLogger("marketdata.kite")

# Kite instrument tokens of the underlying indices (stable; the smoke test checks them against kite.ltp()).
INDEX_SYMBOLS = {"NIFTY": "NSE:NIFTY 50", "BANKNIFTY": "NSE:NIFTY BANK", "FINNIFTY": "NSE:NIFTY FIN SERVICE",
                 "MIDCPNIFTY": "NSE:NIFTY MID SELECT", "SENSEX": "BSE:SENSEX", "BANKEX": "BSE:BANKEX"}
INDEX_TOKENS = {"NIFTY": 256265, "BANKNIFTY": 260105, "FINNIFTY": 257801, "MIDCPNIFTY": 288009,
                "SENSEX": 265, "BANKEX": 274441}


def index_token(underlying: str) -> int:
    try:
        return INDEX_TOKENS[underlying.upper()]
    except KeyError:
        raise KeyError(f"no Kite index token known for {underlying!r} (add it to marketdata.INDEX_TOKENS)") from None


class Transport(Protocol):
    def connect(self) -> None: ...
    def subscribe(self, tokens: list[int], mode: str) -> None: ...
    def unsubscribe(self, tokens: list[int]) -> None: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class Tick:
    price: float
    at: float            # monotonic seconds when received
    wall: datetime


class KiteStream:
    def __init__(self, transport_factory: Callable[[str, "KiteStream"], Transport], token_provider: Callable[[], str],
                 *, rest_ltp: Callable[[list[int]], dict[int, float]] | None = None, mode: str = "ltp",
                 stale_s: float = 10.0, rest_min_interval_s: float = 1.0, watchdog_s: float = 2.0,
                 monotonic=_time.monotonic, clock=datetime.now, sleep=_time.sleep):
        self.transport_factory, self.token_provider, self.rest_ltp = transport_factory, token_provider, rest_ltp
        self.mode, self.stale_s, self.rest_min_interval_s, self.watchdog_s = mode, stale_s, rest_min_interval_s, watchdog_s
        self.monotonic, self.clock, self.sleep = monotonic, clock, sleep
        self._lock = threading.RLock()
        self._rest_lock = threading.Lock()
        self._owners: dict[str, set[int]] = {}
        self._refs: dict[int, int] = {}
        self._ticks: dict[int, Tick] = {}
        self._rest: dict[int, Tick] = {}
        self._listeners: list[Callable[[dict[int, float]], None]] = []
        self._transport: Transport | None = None
        self._access_token: str | None = None
        self._rejected_token: str | None = None
        self._dead = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._rest_next = 0.0
        # health / status
        self.connected = False
        self.connected_at: float | None = None
        self.last_message_at: float | None = None
        self.last_tick_wall: datetime | None = None
        self.session_expired = False
        self.connects = self.reconnects = self.rebuilds = self.stale_rebuilds = 0
        self.errors = 0
        self.last_error: str | None = None
        self.rest_calls = self.rest_errors = 0

    # -- lifecycle ---------------------------------------------------------------------------------
    def start(self, watchdog: bool = True) -> "KiteStream":
        self.check()
        if watchdog and self._thread is None:
            self._thread = threading.Thread(target=self._watch, name="kite-stream-watchdog", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            t, self._transport = self._transport, None
            self.connected = False
        if t is not None:
            try:
                t.close()
            except Exception:
                pass

    def _watch(self) -> None:
        while not self._stop.wait(self.watchdog_s):
            try:
                self.check()
            except Exception:
                log.exception("kite stream watchdog")

    def check(self) -> None:
        """One watchdog pass: session expiry / token change / given-up / stale -> (re)build or stop."""
        try:
            token = self.token_provider()
        except Exception as exc:               # LoginRequired: no valid token until the next login
            with self._lock:
                if not self.session_expired:
                    log.warning("Kite session unavailable (%s); ticker stopped until a new login", exc)
                self.session_expired = True
                self.last_error = f"session: {exc}"
                old, self._transport, self.connected = self._transport, None, False
            if old is not None:
                self._close(old)
            return
        with self._lock:
            if token == self._rejected_token:          # Kite refused this token: wait for a new login
                return
            if self._transport is None or token != self._access_token or self._dead:
                why = ("new access token" if self._access_token and token != self._access_token else
                       "reconnect gave up" if self._dead else "start" if self._access_token is None else "restart")
                self._rebuild(token, why)
            elif self.connected and not self._fresh():
                self.stale_rebuilds += 1
                self._rebuild(token, f"no message for {self.monotonic() - (self.last_message_at or 0):.0f}s")

    def _rebuild(self, token: str, why: str) -> None:
        old = self._transport
        self._transport, self.connected, self._dead = None, False, False
        if old is not None:
            self.rebuilds += 1
            self._close(old)
        log.info("Kite ticker connecting (%s)", why)
        self._access_token, self.session_expired = token, False
        self.last_message_at = None
        t = self.transport_factory(token, self)
        self._transport = t
        try:
            t.connect()
        except Exception as exc:
            self._note_error(f"connect: {type(exc).__name__}: {exc}")
            self._dead = True

    @staticmethod
    def _close(t: Transport) -> None:
        try:
            t.close()
        except Exception as exc:
            log.debug("closing old ticker: %s", exc)

    # -- subscriptions (refcounted by owner) -----------------------------------------------------
    def hold(self, owner: str, tokens: Iterable[int]) -> None:
        """Owner now needs exactly `tokens` (anything it held before and not listed is released)."""
        new = {int(t) for t in tokens}
        with self._lock:
            old = self._owners.get(owner, set())
            self._change(owner, new - old, old - new)

    def acquire(self, tokens: Iterable[int], owner: str = "default") -> None:
        with self._lock:
            have = self._owners.get(owner, set())
            self._change(owner, {int(t) for t in tokens} - have, set())

    def release(self, tokens: Iterable[int], owner: str = "default") -> None:
        with self._lock:
            have = self._owners.get(owner, set())
            self._change(owner, set(), {int(t) for t in tokens} & have)

    def drop_owner(self, owner: str) -> None:
        self.hold(owner, ())
        with self._lock:
            self._owners.pop(owner, None)

    def _change(self, owner: str, add: set[int], remove: set[int]) -> None:
        held = self._owners.setdefault(owner, set())
        subscribe, unsubscribe = [], []
        for t in add:
            held.add(t)
            self._refs[t] = self._refs.get(t, 0) + 1
            if self._refs[t] == 1:
                subscribe.append(t)
        for t in remove:
            held.discard(t)
            self._refs[t] -= 1
            if self._refs[t] <= 0:
                del self._refs[t]
                unsubscribe.append(t)
        if self.connected and self._transport is not None:
            try:
                if subscribe:
                    self._transport.subscribe(sorted(subscribe), self.mode)
                if unsubscribe:
                    self._transport.unsubscribe(sorted(unsubscribe))
            except Exception as exc:          # resubscribed from _refs on the next connect
                self._note_error(f"subscribe: {type(exc).__name__}: {exc}")

    def subscribed(self) -> set[int]:
        with self._lock:
            return set(self._refs)

    def refcount(self, token: int) -> int:
        with self._lock:
            return self._refs.get(int(token), 0)

    # -- transport callbacks (reactor thread) ------------------------------------------------------
    def on_connect(self) -> None:
        with self._lock:
            self.connected, self._dead = True, False
            self.connects += 1
            self.connected_at = self.last_message_at = self.monotonic()
            tokens = sorted(self._refs)
            t = self._transport
        log.info("Kite ticker connected; subscribing %d instruments", len(tokens))
        if tokens and t is not None:
            try:
                t.subscribe(tokens, self.mode)
            except Exception as exc:
                self._note_error(f"resubscribe: {type(exc).__name__}: {exc}")

    def on_message(self) -> None:                 # every frame, heartbeats included
        self.last_message_at = self.monotonic()

    def on_ticks(self, ticks: Iterable[tuple[int, float]]) -> None:
        now_m, now_w = self.monotonic(), self.clock()
        batch: dict[int, float] = {}
        with self._lock:
            self.last_message_at = now_m
            for token, price in ticks:
                if price is None or price <= 0:
                    continue
                self._ticks[int(token)] = Tick(float(price), now_m, now_w)
                batch[int(token)] = float(price)
            if batch:
                self.last_tick_wall = now_w
            listeners = list(self._listeners)
        if batch:
            self._notify(listeners, batch)

    def on_close(self, code=None, reason=None) -> None:
        with self._lock:
            self.connected = False
        if _is_auth_failure(code, reason):
            self._auth_failed(code, reason)

    def on_error(self, code=None, reason=None) -> None:
        self._note_error(f"{code}: {reason}")
        if _is_auth_failure(code, reason):
            self._auth_failed(code, reason)

    def on_reconnect(self, attempts: int) -> None:
        with self._lock:
            self.connected = False
            self.reconnects += 1

    def on_noreconnect(self) -> None:
        with self._lock:
            self.connected, self._dead = False, True
        log.error("Kite ticker gave up reconnecting; the watchdog will rebuild it")

    def _auth_failed(self, code, reason) -> None:
        with self._lock:
            self.session_expired, self._dead, self.connected = True, True, False
            self._rejected_token = self._access_token
            t = self._transport
            self.last_error = f"session rejected by Kite ({code}: {reason}); run: python3 -m zerodha login"
        log.error("Kite ticker: %s", self.last_error)
        if t is not None:                               # stop KiteTicker retrying a token Kite refuses
            self._close(t)

    def _note_error(self, msg: str) -> None:
        with self._lock:
            self.errors += 1
            self.last_error = msg
        log.warning("Kite ticker error: %s", msg)

    # -- listeners ---------------------------------------------------------------------------------
    def add_listener(self, fn: Callable[[dict[int, float]], None]) -> None:
        with self._lock:
            self._listeners.append(fn)

    def remove_listener(self, fn) -> None:
        with self._lock:
            if fn in self._listeners:
                self._listeners.remove(fn)

    @staticmethod
    def _notify(listeners, batch: dict[int, float]) -> None:
        for fn in listeners:
            try:
                fn(batch)
            except Exception:
                log.exception("tick listener failed")

    # -- prices ------------------------------------------------------------------------------------
    def _fresh(self) -> bool:
        return self.last_message_at is not None and self.monotonic() - self.last_message_at <= self.stale_s

    def healthy(self) -> bool:
        with self._lock:
            return self.connected and self._fresh()

    def last(self, token: int) -> Tick | None:
        with self._lock:
            return self._ticks.get(int(token))

    def ltp(self, token: int, max_age: float | None = None) -> float | None:
        """The live price. From the stream when it is healthy and this token is subscribed and has ticked
        (Kite only sends a tick when something changes, so its age is not a staleness signal on a live
        stream). Otherwise kite.ltp() over REST (cached `max_age` seconds, default 1s), else the last
        tick or REST price we ever saw (None if none)."""
        token = int(token)
        with self._lock:
            tick = self._ticks.get(token)
            if tick is not None and token in self._refs and self.connected and self._fresh():
                return tick.price
        px = self._rest_price(token, 1.0 if max_age is None else max(max_age, 0.0))
        if px is not None:
            return px
        with self._lock:
            old = [t for t in (self._ticks.get(token), self._rest.get(token)) if t is not None]
        return max(old, key=lambda t: t.at).price if old else None

    def _rest_price(self, token: int, max_age: float) -> float | None:
        if self.rest_ltp is None:
            return None
        with self._rest_lock:                          # one REST call at a time (Kite: ~1 req/s)
            hit = self._rest.get(token)
            if hit is not None and self.monotonic() - hit.at <= max_age:
                return hit.price
            wait = self._rest_next - self.monotonic()
            if wait > 0:
                if wait > self.rest_min_interval_s:
                    return None
                self.sleep(wait)
            with self._lock:                            # batch: everything the stream cannot vouch for
                live = self.connected and self._fresh()
                need = {token} | ({t for t in self._refs if t not in self._ticks} if live else set(self._refs))
            batch = sorted(need)[:500]
            self._rest_next = self.monotonic() + self.rest_min_interval_s
            self.rest_calls += 1
            try:
                prices = self.rest_ltp(batch) or {}
            except Exception as exc:
                self.rest_errors += 1
                self._note_error(f"REST ltp: {type(exc).__name__}: {exc}")
                return None
            now_m, now_w = self.monotonic(), self.clock()
            for t, p in prices.items():
                if p is not None and p > 0:
                    self._rest[int(t)] = Tick(float(p), now_m, now_w)
            hit = self._rest.get(token)
            return hit.price if hit is not None and hit.at == now_m else None

    def status(self) -> dict:
        with self._lock:
            age = None if self.last_message_at is None else round(self.monotonic() - self.last_message_at, 1)
            return {"source": "kite", "ok": self.connected and self._fresh() and not self.session_expired,
                    "streaming": self.connected and self._fresh(), "connected": self.connected,
                    "session_expired": self.session_expired, "last_message_age_s": age,
                    "last_ok": self.last_tick_wall.isoformat(timespec="seconds") if self.last_tick_wall else None,
                    "subscribed": len(self._refs), "connects": self.connects, "reconnects": self.reconnects,
                    "rebuilds": self.rebuilds, "stale_rebuilds": self.stale_rebuilds, "errors": self.errors,
                    "last_error": self.last_error, "rest_calls": self.rest_calls, "rest_errors": self.rest_errors,
                    "api_budget_remaining": None}


def _is_auth_failure(code, reason) -> bool:
    text = f"{code} {reason}".lower()
    return code == 403 or "403" in text or "tokenexception" in text or "forbidden" in text


# -- real Kite -----------------------------------------------------------------------------------------
class KiteTickerTransport:
    """kiteconnect.KiteTicker behind the Transport protocol. KiteTicker runs on twisted's reactor, which can
    only be started once per process: the first connect starts it in a daemon thread, later ones (rebuilds)
    connect on the running reactor. Every call into the socket is marshalled onto the reactor thread."""

    def __init__(self, api_key: str, access_token: str, sink: KiteStream, reconnect_max_tries: int = 300,
                 reconnect_max_delay: int = 30):
        from kiteconnect import KiteTicker
        kt = KiteTicker(api_key, access_token, reconnect=True, reconnect_max_tries=reconnect_max_tries,
                        reconnect_max_delay=reconnect_max_delay)
        kt.on_connect = lambda ws, resp: sink.on_connect()
        kt.on_message = lambda ws, payload, is_binary: sink.on_message()
        kt.on_ticks = lambda ws, ticks: sink.on_ticks(
            (t["instrument_token"], t.get("last_price")) for t in ticks if "instrument_token" in t)
        kt.on_close = lambda ws, code, reason: sink.on_close(code, reason)
        kt.on_error = lambda ws, code, reason: sink.on_error(code, reason)
        kt.on_reconnect = lambda ws, n: sink.on_reconnect(n)
        kt.on_noreconnect = lambda ws: sink.on_noreconnect()
        self.kt = kt

    @staticmethod
    def _reactor():
        from twisted.internet import reactor
        return reactor

    def _call(self, fn) -> None:
        r = self._reactor()
        if r.running:
            r.callFromThread(fn)
        else:
            fn()

    def connect(self) -> None:
        if self._reactor().running:
            self._reactor().callFromThread(self.kt.connect)
        else:
            self.kt.connect(threaded=True)

    def subscribe(self, tokens: list[int], mode: str) -> None:
        def go():
            if self.kt.is_connected():
                self.kt.subscribe(list(tokens))
                self.kt.set_mode(mode, list(tokens))
        self._call(go)

    def unsubscribe(self, tokens: list[int]) -> None:
        def go():
            if self.kt.is_connected():
                self.kt.unsubscribe(list(tokens))
        self._call(go)

    def close(self) -> None:
        def go():
            try:
                self.kt.close(1000, "closing")
            except Exception:
                pass
        self._call(go)


def kite_ticker_factory(api_key: str):
    return lambda access_token, sink: KiteTickerTransport(api_key, access_token, sink)


def kite_rest_ltp(kite_factory: Callable[[], object]):
    """kite.ltp() by instrument token (Kite accepts tokens as instrument ids) -> {token: last_price}."""
    def fetch(tokens: list[int]) -> dict[int, float]:
        if not tokens:
            return {}
        data = kite_factory().ltp([str(t) for t in tokens])
        return {int(v.get("instrument_token") or k): v.get("last_price") for k, v in data.items()}
    return fetch


def build_kite_stream(mcfg, zcfg, kite_factory: Callable[[], object] | None = None) -> KiteStream:
    """The process-wide stream from MarketDataConfig + ZerodhaConfig (token from the saved daily login)."""
    from zerodha.auth import access_token, connected_kite
    zcfg.require_api()
    kf = kite_factory or _cached(lambda: connected_kite(zcfg), lambda: access_token(zcfg))
    stream = KiteStream(kite_ticker_factory(zcfg.api_key), lambda: access_token(zcfg), rest_ltp=kite_rest_ltp(kf),
                        mode=mcfg.ticker_mode, stale_s=mcfg.stale_s, rest_min_interval_s=mcfg.rest_min_interval_s)
    stream.kite_factory = kf                  # the REST client (historical data) for the same session
    return stream


def _cached(make, current_token):
    """A KiteConnect client reused until the access token changes (a new daily login)."""
    box: dict = {}
    lock = threading.Lock()

    def get():
        tok = current_token()
        with lock:
            if box.get("token") != tok:
                box["kite"], box["token"] = make(), tok
            return box["kite"]
    return get
