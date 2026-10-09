"""Settings from .env (values never printed):

| Key                    | Default                                 | Meaning                                         |
|------------------------|-----------------------------------------|-------------------------------------------------|
| TELEGRAM_API_ID        | (required)                              | from my.telegram.org -> API development tools   |
| TELEGRAM_API_HASH      | (required)                              | same place                                      |
| TELEGRAM_CHANNEL       | Nifty Sensex VIP setups                 | channel title (exact, any case) or @username    |
| TELEGRAM_SESSION       | data/telegram/telegram.session          | login session (a login to YOUR account: private)|
| TELEGRAM_DB            | data/telegram/messages.sqlite           | every message read, as received                 |
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from zerodha.config import read_env_file

DEFAULT_CHANNEL = "Nifty Sensex VIP setups"


@dataclass(frozen=True)
class TelegramConfig:
    api_id: int = 0
    api_hash: str = field(default="", repr=False)
    channel: str = DEFAULT_CHANNEL
    session: Path = Path("data/telegram/telegram.session")
    db: Path = Path("data/telegram/messages.sqlite")

    @classmethod
    def from_env(cls, env_file: str | Path | None = ".env", environ: dict | None = None) -> "TelegramConfig":
        env = {**(read_env_file(env_file) if env_file else {}), **(os.environ if environ is None else environ)}
        g = lambda k, d="": (env.get(k) or "").strip() or d          # noqa: E731 (blank = default)
        raw_id = g("TELEGRAM_API_ID", "0")
        try:
            api_id = int(raw_id)
        except ValueError:
            raise ValueError("TELEGRAM_API_ID must be the number shown on my.telegram.org") from None
        return cls(api_id=api_id, api_hash=g("TELEGRAM_API_HASH"), channel=g("TELEGRAM_CHANNEL", DEFAULT_CHANNEL),
                   session=Path(g("TELEGRAM_SESSION", "data/telegram/telegram.session")),
                   db=Path(g("TELEGRAM_DB", "data/telegram/messages.sqlite")))

    def require_api(self) -> None:
        if not self.api_id or not self.api_hash:
            raise ValueError("set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env (my.telegram.org -> API development tools)")
