"""Kite Connect login flow and daily access-token handling.

Kite Connect has no password/OTP API: a human logs in on kite.zerodha.com, Kite
redirects to the app's redirect URL with ?request_token=..., and that token is
exchanged (with the API secret) for an access token. Access tokens expire at
06:00 IST the next day, so this runs once per trading day:

    python3 -m zerodha login          # opens the browser, catches the redirect locally, saves the token

`login` listens on KITE_REDIRECT_URL (default http://127.0.0.1:5678/kite/callback, which
must also be the Redirect URL of the app on developers.kite.trade) with a one-shot stdlib
HTTP server. If it cannot bind, the URL is not a local http one, or nobody logs in before
the timeout, it falls back to pasting the redirect URL into the terminal.
"""
from __future__ import annotations

import html
import json
import os
import time as _time
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

from .config import ZerodhaConfig

IST = timezone(timedelta(hours=5, minutes=30))
TOKEN_RESET = time(6, 0)          # Kite invalidates access tokens around 06:00 IST
LOGIN_URL = "https://kite.zerodha.com/connect/login?v=3&api_key={api_key}"


class LoginRequired(RuntimeError):
    pass


@dataclass(frozen=True)
class KiteSession:
    access_token: str
    user_id: str
    created_at: str               # ISO timestamp, IST

    def expires_at(self) -> datetime:
        created = datetime.fromisoformat(self.created_at)
        reset = datetime.combine(created.date(), TOKEN_RESET, tzinfo=IST)
        return reset if created < reset else reset + timedelta(days=1)

    def is_valid(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(IST)) < self.expires_at()


def login_url(cfg: ZerodhaConfig) -> str:
    cfg.require_api()
    return LOGIN_URL.format(api_key=cfg.api_key)


def extract_request_token(redirect_url_or_token: str) -> str:
    """Accept the full redirect URL (…?request_token=abc&status=success) or the bare token."""
    s = redirect_url_or_token.strip()
    if "request_token=" not in s:
        if not s or "/" in s or "?" in s:
            raise ValueError("no request_token found")
        return s
    q = parse_qs(urlparse(s).query)
    if q.get("status", ["success"])[0] != "success":
        raise ValueError(f"login did not succeed: status={q.get('status')}")
    return q["request_token"][0]


LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def redirect_endpoint(redirect_url: str) -> tuple[str, int, str]:
    """(host, port, path) to listen on for KITE_REDIRECT_URL; ValueError unless it is a local http:// URL."""
    u = urlparse(redirect_url.strip())
    if u.scheme != "http":
        raise ValueError(f"KITE_REDIRECT_URL must be http:// to be caught locally, got {redirect_url!r}")
    if u.hostname not in LOOPBACK_HOSTS:
        raise ValueError(f"KITE_REDIRECT_URL host must be 127.0.0.1 (loopback), got {u.hostname!r}")
    return u.hostname, (80 if u.port is None else u.port), u.path or "/"


def parse_callback(request_path: str, expected_path: str) -> tuple[str | None, str | None]:
    """Parse one callback request. Returns (request_token, error); both None = not our path (ignore)."""
    u = urlparse(request_path)
    if (u.path.rstrip("/") or "/") != (expected_path.rstrip("/") or "/"):
        return None, None
    q = parse_qs(u.query)
    status = q.get("status", [""])[0]
    token = q.get("request_token", [""])[0]
    if status != "success":
        return None, f"login did not succeed: status={status or '(missing)'}"
    if not token:
        return None, "redirect had no request_token"
    return token, None


_PAGE = ("<!doctype html><meta charset=utf-8><title>Kite login</title>"
         "<body style='font-family:sans-serif;margin:3em'><h2>{title}</h2><p>{body}</p></body>")


class _CallbackServer(HTTPServer):
    expected_path = "/"
    request_token: str | None = None
    error: str | None = None


class _CallbackHandler(BaseHTTPRequestHandler):
    server: _CallbackServer

    def do_GET(self):  # noqa: N802 - http.server API
        token, error = parse_callback(self.path, self.server.expected_path)
        if token is None and error is None:
            self._reply(404, "Not found", "This is the Kite login callback listener.")
            return
        if token:
            self.server.request_token = token
            self._reply(200, "Login complete", "You can close this tab and return to the terminal.")
        else:
            self.server.error = error
            self._reply(400, "Login failed", html.escape(error or ""))

    def _reply(self, code: int, title: str, body: str) -> None:
        data = _PAGE.format(title=title, body=body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # keep request_token out of the terminal
        pass


def start_callback_server(redirect_url: str) -> _CallbackServer:
    """Bind the one-shot listener for KITE_REDIRECT_URL (ValueError / OSError when that is impossible)."""
    host, port, path = redirect_endpoint(redirect_url)
    srv = _CallbackServer((host, port), _CallbackHandler)
    srv.expected_path = path
    return srv


def wait_for_request_token(srv: _CallbackServer, timeout_s: float = 180.0,
                           clock: Callable[[], float] = _time.monotonic) -> str | None:
    """Serve until the callback arrives or timeout_s passes; returns the token (None on timeout).
    Raises ValueError if Kite redirected with a non-success status. Always closes the server."""
    deadline = clock() + timeout_s
    try:
        while srv.request_token is None and srv.error is None:
            left = deadline - clock()
            if left <= 0:
                return None
            srv.timeout = min(left, 1.0)
            srv.handle_request()
    finally:
        srv.server_close()
    if srv.error:
        raise ValueError(srv.error)
    return srv.request_token


def ipv4_adapter():
    """A requests adapter whose connections leave from an IPv4 address only (2026-10-05: an order was refused with
    "IP 2401:4900:... is not allowed to place orders for this app" when the Mac picked up a temporary IPv6 address).
    Binding the source to 0.0.0.0 makes every IPv6 candidate fail to bind, so urllib3 moves on to the IPv4 one."""
    from requests.adapters import HTTPAdapter

    class IPv4Adapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            kwargs["source_address"] = ("0.0.0.0", 0)
            super().init_poolmanager(*args, **kwargs)

    return IPv4Adapter()


def new_kite(cfg: ZerodhaConfig, access_token: str | None = None):
    """KiteConnect client. kiteconnect is imported lazily so the package imports without it."""
    try:
        from kiteconnect import KiteConnect
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError("pip install kiteconnect to talk to Zerodha") from exc
    cfg.require_api()
    kite = KiteConnect(api_key=cfg.api_key)
    if cfg.ipv4_only:
        kite.reqsession.mount("https://", ipv4_adapter())
    if access_token:
        kite.set_access_token(access_token)
    return kite


def exchange_request_token(cfg: ZerodhaConfig, request_token: str, kite=None,
                           now: datetime | None = None) -> KiteSession:
    cfg.require_api(secret=True)
    kite = kite or new_kite(cfg)
    data = kite.generate_session(request_token, api_secret=cfg.api_secret)
    return KiteSession(access_token=data["access_token"], user_id=str(data.get("user_id", "")),
                       created_at=(now or datetime.now(IST)).isoformat())


def save_session(path: str | Path, session: KiteSession) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(asdict(session), fh)
    os.chmod(p, 0o600)


def load_session(path: str | Path) -> KiteSession | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return KiteSession(**json.loads(p.read_text()))
    except (ValueError, TypeError):
        return None


def access_token(cfg: ZerodhaConfig, now: datetime | None = None) -> str:
    """KITE_ACCESS_TOKEN if set, else today's saved token; raises LoginRequired when stale/missing."""
    if cfg.access_token:
        return cfg.access_token
    s = load_session(cfg.token_file)
    if s is None or not s.is_valid(now):
        raise LoginRequired(f"no valid Kite session in {cfg.token_file}; run: python3 -m zerodha login")
    return s.access_token


def connected_kite(cfg: ZerodhaConfig):
    return new_kite(cfg, access_token(cfg))
