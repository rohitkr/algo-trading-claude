"""Shared wiring for the command-line scripts (settings -> logging -> store -> client)."""
from __future__ import annotations

import argparse
import sys
from datetime import date

from .breeze.client import BreezeClient, BreezeError, SessionExpiredError
from .config import ConfigError, Settings, load_settings
from .log import get_logger, setup_logging
from .storage import CandleStore

EXIT_OK, EXIT_ERROR, EXIT_API_LIMIT, EXIT_SESSION = 0, 1, 2, 3


def parse_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {s!r}") from None


def bootstrap(log_name: str) -> tuple[Settings, CandleStore]:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(EXIT_ERROR)
    setup_logging(settings.paths.log_dir, log_name)
    store = CandleStore(settings.paths.database)
    get_logger("app").debug("DuckDB at %s", settings.paths.database)
    return settings, store


def connect_client(settings: Settings, store: CandleStore, daily_limit: int | None = None,
                   delay_seconds: float | None = None) -> BreezeClient:
    log = get_logger("app")
    try:
        return BreezeClient(settings, store, daily_limit, delay_seconds).connect()
    except SessionExpiredError as exc:
        log.error(str(exc))
        sys.exit(EXIT_SESSION)
    except (BreezeError, ConfigError) as exc:
        log.error(str(exc))
        sys.exit(EXIT_ERROR)
