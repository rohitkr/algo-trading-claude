"""ICICI Breeze daily login from the web page (the Breeze light), like the Zerodha one: no ./login.sh, no restart.

ICICI redirects to the redirect URL registered for your Breeze app with ?apisession=<token>. Set that URL in the ICICI
API portal to BREEZE_REDIRECT_URL (default http://127.0.0.1:5679/breeze/callback); the trader listens there while a
login is in progress, saves the token to the Breeze session file (as ./login.sh does) and the running trader uses it.

Shared redirect (another app owns it, e.g. http://localhost:3000/): while that app is NOT running the trader borrows
its port just for the login (both 127.0.0.1 and ::1, since browsers try "localhost" on either). If the port is busy,
paste the ICICI tab's address (…?apisession=…) on the page instead (submit_url)."""
from __future__ import annotations

import html
import logging
import select
import socket
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("trader.breeze_login")
DEFAULT_REDIRECT = "http://localhost:3000/"
TIMEOUT_S = 300.0
_PAGE = ("<!doctype html><meta charset=utf-8><title>Breeze login</title><body style='font-family:sans-serif;margin:3em'>"
         "<h2>{title}</h2><p>{body}</p></body>")


class _Server(HTTPServer):
    path_expected = "/"
    token: str | None = None
    return_to: str | None = None


class _Server6(_Server):
    address_family = socket.AF_INET6


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def _handle(self, body: str = "") -> None:
        u = urlparse(self.path)
        if u.path.rstrip("/") != self.server.path_expected.rstrip("/"):
            return self._reply(404, "Not found", "This is the Breeze login callback listener.")
        q = {**parse_qs(u.query), **parse_qs(body)}                  # ICICI may GET or POST the apisession
        token = (q.get("apisession") or q.get("API_Session") or [""])[0].strip()
        if not token:
            return self._reply(400, "Login failed", "the redirect had no apisession")
        self.server.shared["token"] = token
        back = html.escape(self.server.return_to or "")
        self._reply(200, "Breeze login complete", (f'Back to <a href="{back}">the trader</a>; ' if back else "")
                    + "this tab closes itself.<script>setTimeout(function(){window.close()},800)</script>")

    def do_GET(self):  # noqa: N802
        self._handle()

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        self._handle(self.rfile.read(n).decode(errors="replace") if n else "")

    def _reply(self, code: int, title: str, body: str) -> None:
        data = _PAGE.format(title=title, body=body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # keep the session token out of logs
        pass


class BreezeLogin:
    def __init__(self, api_key: str, session_file, on_token, redirect_url: str = DEFAULT_REDIRECT, clock=datetime.now):
        self.api_key, self.session_file, self.on_token = api_key, session_file, on_token
        self.redirect_url, self.clock = redirect_url or DEFAULT_REDIRECT, clock
        self.status = {"state": "idle", "detail": "", "at": None}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def _set(self, state: str, detail: str = "") -> None:
        self.status = {"state": state, "detail": detail, "at": self.clock().isoformat(timespec="seconds")}

    def start(self, return_to: str | None = None) -> dict:
        """Open the ICICI login. Listens on the redirect address if it is free; otherwise manual=True (paste)."""
        from trading_data.breeze.auth import login_url
        with self._lock:
            url = login_url(self.api_key)
            if self._thread and self._thread.is_alive():
                return {"url": url, "status": self.status, "manual": False}
            u = urlparse(self.redirect_url)
            if u.scheme != "http" or u.hostname not in ("127.0.0.1", "localhost", "::1"):
                raise ValueError(f"BREEZE_REDIRECT_URL must be a local http:// address, got {self.redirect_url!r}")
            port, shared, servers = u.port or 80, {"token": None}, []
            hosts = [("127.0.0.1", _Server), ("::1", _Server6)] if u.hostname == "localhost" else \
                [(u.hostname, _Server6 if u.hostname == "::1" else _Server)]
            busy = None
            for host, cls in hosts:
                try:
                    srv = cls((host, port), _Handler)
                except OSError as exc:
                    busy = busy or exc
                    continue
                srv.path_expected, srv.return_to, srv.shared = u.path or "/", return_to, shared
                servers.append(srv)
            if busy is not None and getattr(busy, "errno", None) not in (None, 49, 99):   # in use (not "no IPv6")
                for srv in servers:
                    srv.server_close()
                servers = []
            if not servers:                       # e.g. the other app runs on that port: paste the URL instead
                self._set("manual", f"{self.redirect_url} is in use: after logging in, paste the ICICI tab's address here")
                return {"url": url, "status": self.status, "manual": True}
            self._set("waiting", "complete the ICICI login in the browser tab")
            self._thread = threading.Thread(target=self._run, args=(servers, shared), name="breeze-login", daemon=True)
            self._thread.start()
            return {"url": url, "status": self.status, "manual": False}

    def submit_url(self, text: str) -> dict:
        """The pasted redirect address (or the bare apisession value)."""
        from trading_data.breeze.auth import extract_session_token
        token = extract_session_token(text or "")
        if not token:
            raise ValueError("no apisession found in that address")
        self._accept(token)
        return {"status": self.status}

    def _accept(self, token: str) -> None:
        from trading_data.breeze.session_store import save_session
        save_session(self.session_file, token, "web")
        self.on_token(token)
        self._set("ok", "Breeze logged in for today")
        log.info("breeze login from the web page: ok")

    def _run(self, servers, shared) -> None:
        import time
        try:
            end = time.monotonic() + TIMEOUT_S
            while shared["token"] is None and time.monotonic() < end:
                ready, _, _ = select.select(servers, [], [], 0.5)
                for srv in ready:
                    srv.handle_request()
            if shared["token"] is None:
                raise TimeoutError(f"no redirect to {self.redirect_url} within {int(TIMEOUT_S)}s: paste the ICICI "
                                   "tab's address instead")
            if self.status["state"] != "ok":                         # not already done by a paste
                self._accept(shared["token"])
        except Exception as exc:                                      # noqa: BLE001 - shown on the page
            if self.status["state"] != "ok":                          # a paste already logged in: nothing to report
                self._set("error", f"{type(exc).__name__}: {exc}")
                log.warning("breeze login from the web page failed: %s", exc)
        finally:
            for srv in servers:
                srv.server_close()
