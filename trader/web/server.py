"""HTTP API + static UI on the standard library (no web framework needed). Bound to 127.0.0.1 only.

Safety:
  * GET never changes anything; every action is a POST.
  * Placing, cancelling and exiting need a single-use server-side token from a preview/prepare call,
    which a browser page from another site cannot read (no CORS), so it cannot trigger orders (CSRF).
  * POSTs must be JSON with the X-Trader header; the Host header must be our loopback address
    (blocks DNS-rebinding pages).
The UI holds no trading logic: it renders what the service returns and posts user intent.
"""
from __future__ import annotations

import json
import logging
import queue
import re
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..service import ActionError
from ..stream import sse

log = logging.getLogger("trader.web")
STATIC = Path(__file__).parent / "static"
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}
HOST = "127.0.0.1"


def make_server(app, port: int) -> ThreadingHTTPServer:
    # Host header allow-list (DNS-rebinding guard). Also answers to any *.localhost name (browsers never resolve
    # those outside this machine), e.g. http://algotrade.localhost:8765, and to names in TRADER_HOSTNAMES.
    names = ("127.0.0.1", "localhost", *getattr(app.cfg, "hostnames", ()))
    allowed_hosts = {f"{n}:{port}" for n in names}
    localhost_name = re.compile(rf"[a-z0-9-]+(\.[a-z0-9-]+)*\.localhost:{port}")

    class Handler(BaseHTTPRequestHandler):
        server_version = "trader"

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        # -- helpers ----------------------------------------------------------------------------
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, default=str).encode(), "application/json")

        def _host_ok(self) -> bool:
            host = self.headers.get("Host", "").lower()
            if host not in allowed_hosts and not localhost_name.fullmatch(host):
                self._json(403, {"error": "bad host"})
                return False
            return True

        # -- GET: read-only ----------------------------------------------------------------------
        def do_GET(self):  # noqa: N802
            if not self._host_ok():
                return
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            svc = app.service
            try:
                if u.path in ("/", "/index.html"):
                    return self._file("index.html")
                if u.path in ("/strategy", "/strategy.html"):
                    return self._file("strategy.html")
                if u.path.startswith("/static/"):
                    return self._file(u.path[len("/static/"):])
                if u.path == "/api/meta":
                    hub = getattr(app, "hub", None)
                    return self._json(200, {**svc.meta(), "streaming": bool(hub and hub.streaming)})
                if u.path == "/api/stream":
                    return self._stream()
                if u.path == "/api/dashboard":
                    return self._json(200, svc.dashboard())
                if u.path == "/api/strikes":
                    # No svc.lock: a strike list is a pure instrument-book lookup, and the first request for
                    # an exchange not yet cached today (e.g. switching to SENSEX/BFO) has to load and parse
                    # that exchange's whole instrument dump - see service.py's meta() docstring for why that
                    # must not block the live monitor tick.
                    strikes = svc.instruments.strikes(q["underlying"], date.fromisoformat(q["expiry"]))
                    return self._json(200, {"strikes": strikes})
                if u.path == "/api/contract":
                    return self._json(200, svc.contract(q["underlying"], q["expiry"], float(q["strike"]), q["option_type"],
                                                           with_ltp=q.get("ltp") == "1", force=q.get("force") == "1"))
                if u.path == "/api/spot":
                    return self._json(200, svc.spot(q["underlying"], with_ltp=q.get("ltp") == "1", force=q.get("force") == "1"))
                if u.path == "/api/strategies":
                    with svc.lock:
                        return self._json(200, {"strategies": app.strategies.dashboard()})
                m = re.fullmatch(r"/api/strategies/(\d+)", u.path)
                if m:
                    return self._json(200, {"strategy": app.strategies.view(int(m.group(1)))})
                m = re.fullmatch(r"/api/trades/(\d+)", u.path)
                if m:
                    tid = int(m.group(1))
                    with svc.lock:
                        t = svc.repo.trade(tid)
                        if not t:
                            return self._json(404, {"error": "no such trade"})
                        return self._json(200, {"trade": svc.trade_view(tid), "orders": svc.repo.orders(tid),
                                                "events": svc.repo.events(tid)})
                if u.path == "/api/events":
                    with svc.lock:
                        return self._json(200, {"events": svc.repo.events(limit=100)})
                return self._json(404, {"error": "not found"})
            except (KeyError, ValueError, ActionError) as exc:
                return self._json(400, {"error": str(exc)})
            except Exception as exc:
                log.exception("GET %s", self.path)
                return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

        def _stream(self) -> None:
            """Server-Sent Events (trader/stream.py): held open until the page goes away or the server stops."""
            hub = getattr(app, "hub", None)
            if hub is None:
                return self._json(404, {"error": "no stream"})
            client = hub.connect()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                self.wfile.write(b"retry: 2000\n\n")
                self.wfile.flush()
                while not client.closed:
                    try:
                        event, data = client.q.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": keep-alive\n\n")      # also detects a closed page
                        self.wfile.flush()
                        continue
                    if event == "bye":
                        break
                    self.wfile.write(sse(event, data))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                hub.disconnect(client)
            self.close_connection = True

        def _file(self, name: str) -> None:
            p = (STATIC / name).resolve()
            if STATIC.resolve() not in p.parents or not p.is_file():
                return self._json(404, {"error": "not found"})
            self._send(200, p.read_bytes(), TYPES.get(p.suffix, "application/octet-stream"))

        # -- POST: actions ------------------------------------------------------------------------
        def do_POST(self):  # noqa: N802
            if not self._host_ok():
                return
            if self.headers.get("X-Trader") != "1" or "application/json" not in self.headers.get("Content-Type", ""):
                return self._json(403, {"error": "actions need a JSON request from the trader UI"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except (ValueError, json.JSONDecodeError):
                return self._json(400, {"error": "bad JSON"})
            u = urlparse(self.path)
            svc = app.service
            try:
                if u.path == "/api/preview":
                    return self._json(200, svc.preview(body))
                m = re.fullmatch(r"/api/trades/(\d+)/edit/(prepare|apply)", u.path)
                if m:
                    tid = int(m.group(1))
                    if m.group(2) == "prepare":
                        return self._json(200, svc.prepare_edit(tid, dict(body.get("changes") or {})))
                    return self._json(200, svc.apply_edit(tid, str(body.get("token", ""))))
                m = re.fullmatch(r"/api/trades/(\d+)/partial/(prepare|confirm)", u.path)
                if m:
                    tid = int(m.group(1))
                    if m.group(2) == "prepare":
                        return self._json(200, svc.prepare_partial_exit(tid, body.get("qty")))
                    return self._json(200, svc.confirm_partial_exit(tid, str(body.get("token", ""))))
                m = re.fullmatch(r"/api/trades/(\d+)/refresh_ltp", u.path)
                if m:
                    return self._json(200, svc.refresh_ltp(int(m.group(1))))
                m = re.fullmatch(r"/api/trades/(\d+)/(confirm|prepare|cancel|exit)", u.path)
                if m:
                    tid, action = int(m.group(1)), m.group(2)
                    if action == "confirm":
                        return self._json(200, svc.confirm(tid, str(body.get("token", ""))))
                    if action == "prepare":
                        return self._json(200, svc.prepare(tid, str(body.get("action", "")).upper()))
                    if action == "cancel":
                        return self._json(200, svc.cancel_entry(tid, str(body.get("token", ""))))
                    return self._json(200, svc.request_exit(tid, str(body.get("token", ""))))
                if u.path == "/api/stream/watch":
                    hub = getattr(app, "hub", None)
                    if hub is None:
                        return self._json(404, {"error": "no stream"})
                    return self._json(200, hub.watch(int(body["client"]), list(body.get("keys") or [])))
                if u.path == "/api/resume":
                    svc.resume()
                    return self._json(200, {"ok": True})
                if u.path == "/api/strategies/trade_all":
                    return self._json(200, app.strategies.create_and_trade(body))
                m = re.fullmatch(r"/api/strategies/legs/(\d+)/add", u.path)
                if m:
                    return self._json(200, app.strategies.add_to_leg(int(m.group(1)), body))
                m = re.fullmatch(r"/api/strategies/(\d+)/exit", u.path)
                if m:
                    return self._json(200, app.strategies.exit_all(int(m.group(1))))
                if u.path == "/api/paper/price":
                    if app.cfg.mode != "PAPER" or not hasattr(app.quotes, "set"):
                        return self._json(403, {"error": "manual prices exist only in PAPER with TRADER_PAPER_QUOTES=manual"})
                    app.quotes.set(str(body["tradingsymbol"]), float(body["price"]))
                    svc.tick()
                    return self._json(200, {"ok": True})
                return self._json(404, {"error": "not found"})
            except ActionError as exc:
                return self._json(409, {"error": str(exc)})
            except (KeyError, ValueError) as exc:
                return self._json(400, {"error": str(exc)})
            except Exception as exc:
                log.exception("POST %s", self.path)
                return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    return ThreadingHTTPServer((HOST, port), Handler)
