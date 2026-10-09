"""venv/bin/python -m telegram_signals login            # once, in your terminal: phone + the code Telegram sends
   venv/bin/python -m telegram_signals fetch [--limit 500]
   venv/bin/python -m telegram_signals show  [--limit 30]
   venv/bin/python -m telegram_signals listen            # live (Ctrl-C stops)"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import TelegramConfig
from .store import MessageStore


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="telegram_signals", description="Read the tips channel (read-only).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login")
    f = sub.add_parser("fetch"); f.add_argument("--limit", type=int, default=500)
    s = sub.add_parser("show"); s.add_argument("--limit", type=int, default=30)
    sub.add_parser("listen")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = TelegramConfig.from_env()
    from . import reader
    if args.cmd == "login":
        print(asyncio.run(reader.login(cfg)))
    elif args.cmd == "fetch":
        r = asyncio.run(reader.fetch(cfg, args.limit))
        print(f"{r['channel']}: read {r['read']} messages, {r['new_or_changed']} new/changed, {r['stored']} stored")
    elif args.cmd == "show":
        for m in reversed(MessageStore(cfg.db).recent(args.limit)):
            text = (m["text"] or ("[media]" if m["has_media"] else "")).replace("\n", " | ")
            print(f"{m['date'][:16]}  #{m['msg_id']}{' ↩' + str(m['reply_to']) if m['reply_to'] else ''}  {text[:160]}")
    elif args.cmd == "listen":
        asyncio.run(reader.listen(cfg, lambda m: print(f"{m.date:%H:%M} #{m.id}  {(m.message or '')[:160]}")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
