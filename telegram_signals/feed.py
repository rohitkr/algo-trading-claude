"""The live signal feed inside the trader process: a background thread (its own asyncio loop for Telethon) that
catches up on recent channel messages at start, then stores every new/edited one as it arrives. Read-only. It is
fenced off from trading: any failure (no keys, not logged in, network) only shows on the /telegram page, is retried,
and never reaches the trader. Phase 2: nothing here places, changes or cancels orders."""
from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timezone

from .config import TelegramConfig
from .parser import TICK, build_signals
from .store import MessageStore

log = logging.getLogger("telegram_signals")
RETRY_S = 30
CATCH_UP = 200                 # messages re-read at start, to fill anything missed while the trader was down


class SignalFeed:
    def __init__(self, cfg: TelegramConfig):
        self.cfg = cfg
        self.store = MessageStore(cfg.db)
        self.status = {"state": "off", "detail": "not started", "channel": None, "last_message_at": None}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = None

    # -- lifecycle -------------------------------------------------------------------------------------
    def start(self) -> None:
        why = self._not_ready()
        if why:
            self.status.update(state="off", detail=why)
            log.info("telegram feed off: %s", why)
            return
        self._thread = threading.Thread(target=self._run, name="telegram-feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._loop and self._client:
            try:
                asyncio.run_coroutine_threadsafe(self._client.disconnect(), self._loop)
            except Exception:
                pass

    def _not_ready(self) -> str | None:
        try:
            self.cfg.require_api()
        except ValueError as exc:
            return str(exc)
        session = self.cfg.session if self.cfg.session.suffix == ".session" else self.cfg.session.with_suffix(".session")
        if not session.exists():
            return "not logged in to Telegram: run  venv/bin/python -m telegram_signals login  once"
        try:
            import telethon  # noqa: F401
        except ImportError:
            return "telethon is not installed: venv/bin/pip install -r requirements.txt"
        return None

    def _run(self) -> None:
        while not self._stop.is_set():
            self._loop = asyncio.new_event_loop()
            try:
                self._loop.run_until_complete(self._listen())
            except Exception as exc:                      # never let it die silently; never touch the trader
                log.warning("telegram feed: %s", exc)
                self.status.update(state="error", detail=f"{type(exc).__name__}: {exc} (retrying in {RETRY_S}s)")
            finally:
                self._loop.close()
                self._client = None
            if self._stop.wait(RETRY_S):
                break

    async def _listen(self) -> None:
        from telethon import events

        from .reader import _find, _save, make_client
        self.status.update(state="connecting", detail="")
        client = self._client = make_client(self.cfg)
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session expired: run  venv/bin/python -m telegram_signals login  again")
        entity, cid, title, username = await _find(client, self.cfg.channel)
        self.store.save_channel(cid, title, username)
        self.status.update(channel=title)
        async for m in client.iter_messages(entity, limit=CATCH_UP):
            _save(self.store, cid, m)

        async def on_message(event):
            _save(self.store, cid, event.message)
            self.status["last_message_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

        client.add_event_handler(on_message, events.NewMessage(chats=entity))
        client.add_event_handler(on_message, events.MessageEdited(chats=entity))
        self.status.update(state="listening", detail="")
        log.info("telegram feed: listening to %s", title)
        await client.run_until_disconnected()

    # -- what the /telegram page shows ---------------------------------------------------------------------
    def view(self, limit: int = 1500, show_ticks: bool = False) -> dict:
        rows = self.store.recent(limit)
        signals, messages = build_signals(rows)
        msgs = [m for m in reversed(messages) if show_ticks or m["kind"] != TICK]
        return {"status": dict(self.status),
                "signals": sorted(signals.values(), key=lambda s: s["date"], reverse=True),
                "messages": [{k: m[k] for k in ("msg_id", "date", "text", "reply_to", "has_media", "kind",
                                                "parsed", "signal_id")} for m in msgs[:400]],
                "stored": self.store.count()}
