"""Central logging: INFO to the console, DEBUG to logs/<name>.log.

Keeps the legacy format (`time | LEVEL | message`). A redaction filter masks
registered secrets (password, API secret, session token, OTP) if they ever end
up in a log message.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"

_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Mask this value in every log record from now on."""
    if value and len(value) >= 4:
        _secrets.add(value)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if _secrets:
            msg = record.getMessage()
            redacted = msg
            for s in _secrets:
                redacted = redacted.replace(s, "***")
            if redacted != msg:
                record.msg, record.args = redacted, ()
        return True


def setup_logging(log_dir: Path, name: str = "pipeline", console_level: int = logging.INFO) -> logging.Logger:
    """Configure the root `trading_data` logger once per process."""
    root = logging.getLogger("trading_data")
    if getattr(root, "_configured", False):
        return root
    root.setLevel(logging.DEBUG)
    fmt = logging.Formatter(FORMAT, datefmt=DATEFMT)
    redact = RedactingFilter()

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(console_level)
    ch.setFormatter(fmt)
    ch.addFilter(redact)
    root.addHandler(ch)

    log_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_dir / f"{name}.log", mode="a", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    fh.addFilter(redact)
    root.addHandler(fh)

    root.propagate = False
    root._configured = True  # type: ignore[attr-defined]
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"trading_data.{name}")
