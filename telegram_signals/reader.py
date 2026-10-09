"""Read-only Telegram access as YOUR user (a bot cannot read a channel it does not administer). Nothing here sends,
forwards, reacts or marks anything: it only lists your chats, finds the channel and reads its messages."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from .config import TelegramConfig
from .store import MessageStore

log = logging.getLogger("telegram_signals")


def pick_channel(dialogs: list[tuple[int, str, str | None]], wanted: str) -> tuple[int, str, str | None]:
    """dialogs: (id, title, username). wanted: "@username" or a title (exact match, any case/spacing).
    Exactly one match, else ValueError naming the near misses - never a guess."""
    w = wanted.strip()
    if w.startswith("@"):
        hits = [d for d in dialogs if (d[2] or "").lower() == w[1:].lower()]
    else:
        norm = lambda s: " ".join((s or "").lower().split())            # noqa: E731
        hits = [d for d in dialogs if norm(d[1]) == norm(w)]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise ValueError(f"{len(hits)} chats are called {w!r}: set TELEGRAM_CHANNEL to its @username instead")
    words = set(w.lower().lstrip("@").split())
    near = [d[1] for d in dialogs if words & set((d[1] or "").lower().split())][:8]
    raise ValueError(f"no chat called {w!r} in your Telegram" + (f"; similar: {near}" if near else ""))


def make_client(cfg: TelegramConfig):
    from telethon import TelegramClient                     # imported lazily: tests/other tools don't need it
    cfg.require_api()
    cfg.session.parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(str(cfg.session.with_suffix("")), cfg.api_id, cfg.api_hash)


def _private(session: Path) -> None:
    p = session if session.suffix == ".session" else session.with_suffix(".session")
    if p.exists():
        os.chmod(p, 0o600)


async def login(cfg: TelegramConfig) -> str:
    """Interactive, in YOUR terminal: Telethon asks for the phone number, the code Telegram sends, and the 2-step
    password if you have one. Saves the session file (chmod 600)."""
    client = make_client(cfg)
    await client.start()
    me = await client.get_me()
    await client.disconnect()
    _private(cfg.session)
    return f"logged in as {me.first_name or ''} {('@' + me.username) if me.username else ''}".strip()


async def _find(client, wanted: str):
    dialogs = []
    async for d in client.iter_dialogs():
        if d.is_channel or d.is_group:
            dialogs.append((d.id, d.name, getattr(d.entity, "username", None)))
    cid, title, username = pick_channel(dialogs, wanted)
    return await client.get_entity(cid), cid, title, username


def _save(store: MessageStore, cid: int, m) -> bool:
    reply = getattr(getattr(m, "reply_to", None), "reply_to_msg_id", None)
    return store.upsert(cid, m.id, m.date, m.message or "", edit_date=m.edit_date, reply_to=reply,
                        has_media=m.media is not None)


async def fetch(cfg: TelegramConfig, limit: int = 500) -> dict:
    """The last `limit` messages of the channel into the store (re-running only adds/updates)."""
    store = MessageStore(cfg.db)
    client = make_client(cfg)
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("not logged in: run  venv/bin/python -m telegram_signals login  first")
    try:
        entity, cid, title, username = await _find(client, cfg.channel)
        store.save_channel(cid, title, username)
        n = new = 0
        async for m in client.iter_messages(entity, limit=limit):
            n += 1
            new += int(_save(store, cid, m))
        return {"channel": title, "username": username, "read": n, "new_or_changed": new, "stored": store.count()}
    finally:
        await client.disconnect()


async def listen(cfg: TelegramConfig, on_message=None) -> None:
    """Live: every new or edited message of the channel into the store as it arrives (Ctrl-C stops)."""
    from telethon import events
    store = MessageStore(cfg.db)
    client = make_client(cfg)
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("not logged in: run  venv/bin/python -m telegram_signals login  first")
    entity, cid, title, username = await _find(client, cfg.channel)
    store.save_channel(cid, title, username)

    async def handler(event):
        _save(store, cid, event.message)
        if on_message:
            on_message(event.message)

    client.add_event_handler(handler, events.NewMessage(chats=entity))
    client.add_event_handler(handler, events.MessageEdited(chats=entity))
    log.info("listening to %s", title)
    await client.run_until_disconnected()
