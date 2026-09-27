"""Zerodha settings from environment variables (secrets are never stored in code or git).

| Variable              | Default                   | Meaning                                              |
|-----------------------|---------------------------|------------------------------------------------------|
| KITE_API_KEY          | (required for live/login) | Kite Connect app API key                             |
| KITE_API_SECRET       | (required for login)      | Kite Connect app secret                              |
| KITE_ACCESS_TOKEN     |                           | Use this token instead of the saved session file     |
| KITE_TOKEN_FILE       | data/.kite_session.json   | Where the daily access token is saved (chmod 600)    |
| KITE_REDIRECT_URL     | http://127.0.0.1:5678/kite/callback | Must equal the app's Redirect URL on developers.kite.trade; `login` listens here |
| KITE_DRY_RUN          | 1                         | 1 = log orders only; set 0 to send real orders       |
| KITE_PRODUCT          | NRML                      | NRML (carry overnight) or MIS (intraday)             |
| KITE_ORDER_TYPE       | LIMIT                     | LIMIT (marketable limit around LTP) or MARKET        |
| KITE_LIMIT_BUFFER_PCT | 2.0                       | LIMIT price = LTP +/- this % (BUY above, SELL below)  |
| KITE_FREEZE_QTY       | 1800                      | Max units per order (NSE freeze limit; verify!)      |
| KITE_FILL_TIMEOUT_S   | 20                        | Seconds to wait for a fill before re-pricing         |
| KITE_MAX_REPRICES     | 3                         | Re-price attempts before giving up on a leg          |
| KITE_MARGIN_BUFFER_PCT| 10                        | Need available >= required x (1 + buffer)            |
| KITE_TAG              | algo                      | Order tag (max 20 chars)                             |
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_REDIRECT_URL = "http://127.0.0.1:5678/kite/callback"


def _bool(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


def read_env_file(path: str | Path) -> dict[str, str]:
    """Minimal KEY=VALUE reader (no python-dotenv dependency). Missing file -> {}."""
    out: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = _env_value(v)
    return out


def _env_value(raw: str) -> str:
    """Quoted values are taken verbatim up to the closing quote (may contain #);
    unquoted values drop a trailing ` # comment` and surrounding whitespace."""
    v = raw.strip()
    if v[:1] in ('"', "'"):
        end = v.find(v[0], 1)
        if end != -1:
            return v[1:end]
    for i, ch in enumerate(v):
        if ch == "#" and (i == 0 or v[i - 1] in " \t"):
            return v[:i].rstrip()
    return v


@dataclass(frozen=True)
class ZerodhaConfig:
    api_key: str = ""
    api_secret: str = field(default="", repr=False)
    access_token: str = field(default="", repr=False)
    token_file: Path = Path("data/.kite_session.json")
    redirect_url: str = DEFAULT_REDIRECT_URL
    dry_run: bool = True
    exchange: str = "NFO"
    product: str = "NRML"
    order_type: str = "LIMIT"
    limit_buffer_pct: float = 2.0
    freeze_qty: int = 1800
    fill_timeout_s: float = 20.0
    poll_interval_s: float = 0.5
    max_reprices: int = 3
    margin_buffer_pct: float = 10.0
    tag: str = "algo"

    def __post_init__(self):
        if self.product not in ("NRML", "MIS"):
            raise ValueError(f"KITE_PRODUCT must be NRML or MIS, not {self.product!r}")
        if self.order_type not in ("LIMIT", "MARKET"):
            raise ValueError(f"KITE_ORDER_TYPE must be LIMIT or MARKET, not {self.order_type!r}")
        if len(self.tag) > 20:
            raise ValueError("KITE_TAG must be at most 20 characters")

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None, env_file: str | Path | None = ".env") -> "ZerodhaConfig":
        env = dict(read_env_file(env_file)) if env_file else {}
        # Shell variables win over .env; clean them too, since `export $(cat .env | xargs)`
        # style loading leaves inline "# comments" in the value.
        env.update({k: _env_value(v) for k, v in (os.environ if environ is None else environ).items()
                    if k.startswith("KITE_")})
        g = env.get
        return cls(
            api_key=g("KITE_API_KEY", ""),
            api_secret=g("KITE_API_SECRET", ""),
            access_token=g("KITE_ACCESS_TOKEN", ""),
            token_file=Path(g("KITE_TOKEN_FILE", "data/.kite_session.json")),
            redirect_url=g("KITE_REDIRECT_URL", "") or DEFAULT_REDIRECT_URL,
            dry_run=_bool(g("KITE_DRY_RUN", "1")),
            product=g("KITE_PRODUCT", "NRML").upper(),
            order_type=g("KITE_ORDER_TYPE", "LIMIT").upper(),
            limit_buffer_pct=float(g("KITE_LIMIT_BUFFER_PCT", "2.0")),
            freeze_qty=int(g("KITE_FREEZE_QTY", "1800")),
            fill_timeout_s=float(g("KITE_FILL_TIMEOUT_S", "20")),
            max_reprices=int(g("KITE_MAX_REPRICES", "3")),
            margin_buffer_pct=float(g("KITE_MARGIN_BUFFER_PCT", "10")),
            tag=g("KITE_TAG", "algo"),
        )

    def require_api(self, secret: bool = False) -> None:
        missing = [n for n, v in (("KITE_API_KEY", self.api_key), ("KITE_API_SECRET", self.api_secret))
                   if not v and (n == "KITE_API_KEY" or secret)]
        if missing:
            raise RuntimeError(f"set {', '.join(missing)} in the environment or .env")
