"""Kite Connect login flow and daily access-token handling.

Kite Connect has no password/OTP API: a human logs in on kite.zerodha.com, Kite
redirects to the app's redirect URL with ?request_token=..., and that token is
exchanged (with the API secret) for an access token. Access tokens expire at
06:00 IST the next day, so this runs once per trading day:

    python3 -m zerodha login          # prints the URL, asks for the redirect URL, saves the token
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
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


def new_kite(cfg: ZerodhaConfig, access_token: str | None = None):
    """KiteConnect client. kiteconnect is imported lazily so the package imports without it."""
    try:
        from kiteconnect import KiteConnect
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError("pip install kiteconnect to talk to Zerodha") from exc
    cfg.require_api()
    kite = KiteConnect(api_key=cfg.api_key)
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
