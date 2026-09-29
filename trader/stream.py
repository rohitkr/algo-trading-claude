"""Server -> browser push: one Server-Sent Events stream per open page (GET /api/stream).

Events (each `event: <name>` + one JSON `data:` line):
    hello      {"client": id, "streaming": bool}                        first message; the page then POSTs
                                                                          /api/stream/watch with the keys it shows
    ticks      {"prices": {key: ltp}, "trades": {id: {ltp, pnl, pnl_pct}}, "strategies": {id: combined_pnl}}
               coalesced every `flush_s` (0.25s): only what changed since the last flush
    dashboard  {"version": n}                                           trade/order state changed: refetch
                                                                          /api/dashboard (replaces the 2s polling)
    status     {"quotes": {...}}                                         every `status_s`, doubles as keep-alive

Keys: "NFO:NIFTY26SEP25000CE" (exchange:tradingsymbol) or "SPOT:NIFTY". Prices come from the process-wide
KiteStream (trader.market.KiteQuotes). Each page's keys are held in the stream under its own owner, and every
trade of today's book (open AND closed, contract not yet expired) under the owner "trades", so a closed
position keeps a live LTP. Per-trade P&L on a tick uses the same formula as TradeService._view():
realized + direction x open_qty x (ltp - entry_avg) - display only; every trading decision still happens
in TradeService.tick() exactly as before.

With the Breeze provider there are no ticks; after each monitor tick the hub pushes the trades' last_ltp /
pnl from the database instead, so the pages still need no polling.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import logging
import queue
import threading
import time as _time
from dataclasses import dataclass, field
from datetime import date

from . import lifecycle as L

log = logging.getLogger("trader.stream")

PRICE_FIELDS = {"last_ltp", "last_ltp_at", "unrealized_pnl", "kite_ltp", "updated_at", "best_price"}


@dataclass
class Client:
    id: int
    q: "queue.Queue[tuple[str, dict]]" = field(default_factory=lambda: queue.Queue(maxsize=500))
    keys: set = field(default_factory=set)
    closed: bool = False


@dataclass(frozen=True)
class TradeMark:
    id: int
    group_id: int | None
    token: int | None
    open: bool                 # open status with an open quantity: P&L moves with the price
    direction: int
    open_qty: int
    entry_avg: float | None
    filled: int
    realized: float


class TickHub:
    OWNER = "trades"

    def __init__(self, service, quotes, flush_s: float = 0.25, status_s: float = 5.0, monotonic=_time.monotonic):
        self.svc, self.quotes = service, quotes
        self.stream = getattr(quotes, "stream", None)
        self.flush_s, self.status_s, self.monotonic = flush_s, status_s, monotonic
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._clients: dict[int, Client] = {}
        self._key_token: dict[str, int] = {}
        self._token_keys: dict[int, set[str]] = {}
        self._marks: dict[int, TradeMark] = {}             # trade id -> mark
        self._by_token: dict[int, list[int]] = {}           # token -> trade ids
        self._groups: dict[int, list[int]] = {}             # strategy id -> leg trade ids
        self._pnl: dict[int, float] = {}                    # trade id -> latest P&L shown
        self._dirty: dict[int, float] = {}
        self._fingerprint: str | None = None
        self.version = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if self.stream is not None:
            self.stream.add_listener(self._on_ticks)

    @property
    def streaming(self) -> bool:
        return self.stream is not None

    def start(self) -> "TickHub":
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="trader-tickhub", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        for c in list(self._clients.values()):
            self._put(c, "bye", {})

    # -- clients -----------------------------------------------------------------------------------
    def connect(self) -> Client:
        c = Client(next(self._ids))
        with self._lock:
            self._clients[c.id] = c
        self._put(c, "hello", {"client": c.id, "streaming": self.streaming, "version": self.version})
        return c

    def disconnect(self, c: Client) -> None:
        c.closed = True
        with self._lock:
            self._clients.pop(c.id, None)
        if self.stream is not None:
            self.stream.drop_owner(f"ui:{c.id}")

    def watch(self, client_id: int, keys: list[str]) -> dict:
        """The page shows these keys: hold them in the stream and send their current prices at once."""
        with self._lock:
            c = self._clients.get(int(client_id))
        if c is None:
            raise KeyError(f"no stream client {client_id}")
        if self.stream is None:                   # Breeze: nothing streams; trades still come after each tick
            return {"keys": [], "errors": {}, "streaming": False}
        tokens, errors = {}, {}
        for k in list(dict.fromkeys(str(x) for x in keys))[:200]:
            try:
                tokens[k] = self._token_for(k)
            except Exception as exc:
                errors[k] = str(exc)
        c.keys = set(tokens)
        self.stream.hold(f"ui:{c.id}", tokens.values())
        prices = {}
        for k, tok in tokens.items():
            tick = self.stream.last(tok)
            px = tick.price if tick is not None else self.stream.ltp(tok)
            if px is not None:
                prices[k] = px
        if prices:
            self._put(c, "ticks", {"prices": prices, "trades": {}, "strategies": {}})
        return {"keys": sorted(tokens), "errors": errors, "streaming": True}

    def _token_for(self, key: str) -> int:
        with self._lock:
            if key in self._key_token:
                return self._key_token[key]
        exch, _, sym = key.partition(":")
        if not sym:
            raise ValueError(f"bad key {key!r}; use EXCHANGE:TRADINGSYMBOL or SPOT:UNDERLYING")
        if exch.upper() == "SPOT":
            tok = self.quotes.spot_token(sym)
        else:
            tok = int(self.svc.instruments.by_symbol(exch.upper(), sym).instrument_token)
        with self._lock:
            self._key_token[key] = tok
            self._token_keys.setdefault(tok, set()).add(key)
        return tok

    # -- after every monitor tick (monitor thread) ---------------------------------------------------
    def after_tick(self) -> None:
        svc = self.svc
        with svc.lock:
            trades = svc.repo.trades(L.OPEN_STATUSES) + svc.repo.trades(L.TERMINAL - {L.EXPIRED}, limit=100)
            seen = {t["id"] for t in trades}
            for gid in {t["group_id"] for t in trades if t.get("group_id")}:
                trades += [t for t in svc.repo.trades_by_group(gid) if t["id"] not in seen]
            halted = svc.halted()
            last_event = svc.repo.events(limit=1)
        today = svc.clock().date()
        marks, by_token, groups, pnl, fp = {}, {}, {}, {}, []
        for t in trades:
            tok = self._trade_token(t, today)
            is_open = t["status"] in L.OPEN_STATUSES and (t["open_qty"] or 0) > 0 and t["entry_avg_price"]
            m = TradeMark(t["id"], t.get("group_id"), tok, bool(is_open), L.direction(t["side"]), t["open_qty"] or 0,
                          t["entry_avg_price"], t["filled_qty"] or 0, t["realized_pnl"] or 0.0)
            marks[m.id] = m
            if tok is not None:
                by_token.setdefault(tok, []).append(m.id)
            if m.group_id:
                groups.setdefault(m.group_id, []).append(m.id)
            pnl[m.id] = (t["realized_pnl"] or 0) + ((t["unrealized_pnl"] or 0) if t["status"] in L.OPEN_STATUSES else 0)
            fp.append({k: v for k, v in t.items() if k not in PRICE_FIELDS})
        digest = hashlib.sha1(json.dumps([fp, halted, last_event[0]["id"] if last_event else None],
                                         sort_keys=True, default=str).encode()).hexdigest()
        with self._lock:
            self._marks, self._by_token, self._groups = marks, by_token, groups
            for tid, v in pnl.items():
                self._pnl.setdefault(tid, v)
                if not marks[tid].open:
                    self._pnl[tid] = v
            changed = digest != self._fingerprint
            self._fingerprint = digest
            if changed:
                self.version += 1
        if self.stream is not None:
            self.stream.hold(self.OWNER, by_token.keys())
        else:                                        # no ticks (Breeze): push the monitor's own marks
            rows = {t["id"]: t for t in trades}
            self._broadcast("ticks", {"prices": {}, "strategies": self._strategy_pnl(pnl),
                                      "trades": {tid: {"ltp": rows[tid]["kite_ltp"] or rows[tid]["last_ltp"],
                                                       "pnl": round(v, 2), "pnl_pct": self._pct(marks[tid], v)}
                                                 for tid, v in pnl.items()}})
        if changed:
            self._broadcast("dashboard", {"version": self.version})

    def _trade_token(self, t: dict, today: date) -> int | None:
        try:
            if t["expiry"] and date.fromisoformat(str(t["expiry"])[:10]) < today:
                return None                          # expired contract: no ticks will ever come
            if t.get("instrument_token"):
                return int(t["instrument_token"])
            return int(self.svc.instruments.by_symbol(t["exchange"], t["tradingsymbol"]).instrument_token)
        except Exception:
            return None

    # -- ticks ----------------------------------------------------------------------------------------
    def _on_ticks(self, batch: dict[int, float]) -> None:     # reactor thread: record only
        with self._lock:
            self._dirty.update(batch)

    def flush(self) -> None:
        with self._lock:
            dirty, self._dirty = self._dirty, {}
            if not dirty:
                return
            trades, touched_groups = {}, set()
            for tok, px in dirty.items():
                for tid in self._by_token.get(tok, ()):
                    m = self._marks[tid]
                    if m.open:
                        v = m.realized + m.direction * m.open_qty * (px - m.entry_avg)
                        self._pnl[tid] = v
                        if m.group_id:
                            touched_groups.add(m.group_id)
                    else:
                        v = self._pnl.get(tid, m.realized)
                    trades[tid] = {"ltp": px, "pnl": round(v, 2), "pnl_pct": self._pct(m, v)}
            strategies = {gid: round(sum(self._pnl.get(t, 0.0) for t in self._groups.get(gid, ())), 2)
                          for gid in touched_groups}
            key_prices = {k: px for tok, px in dirty.items() for k in self._token_keys.get(tok, ())}
            clients = list(self._clients.values())
        for c in clients:
            prices = {k: v for k, v in key_prices.items() if k in c.keys}
            if prices or trades:
                self._put(c, "ticks", {"prices": prices, "trades": trades, "strategies": strategies})

    def _strategy_pnl(self, pnl: dict[int, float]) -> dict:
        return {gid: round(sum(pnl.get(t, 0.0) for t in ids), 2) for gid, ids in self._groups.items()}

    @staticmethod
    def _pct(m: TradeMark, v: float) -> float | None:
        cost = (m.entry_avg or 0) * m.filled
        return round(v / cost * 100, 2) if cost else None

    # -- delivery ---------------------------------------------------------------------------------------
    def _broadcast(self, event: str, data: dict) -> None:
        with self._lock:
            clients = list(self._clients.values())
        for c in clients:
            self._put(c, event, data)

    def _put(self, c: Client, event: str, data: dict) -> None:
        try:
            c.q.put_nowait((event, data))
        except queue.Full:                              # a stalled page: drop its backlog, make it resync
            try:
                while True:
                    c.q.get_nowait()
            except queue.Empty:
                pass
            c.q.put_nowait(("dashboard", {"version": self.version, "resync": True}))

    def _run(self) -> None:
        next_status = 0.0
        while not self._stop.wait(self.flush_s):
            try:
                self.flush()
                if self.monotonic() >= next_status:
                    next_status = self.monotonic() + self.status_s
                    self._broadcast("status", {"quotes": self.quotes.status(), "version": self.version})
            except Exception:
                log.exception("tick hub flush")


def sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, default=str, separators=(',', ':'))}\n\n".encode()
