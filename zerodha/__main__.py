"""Command line: daily login and quick account checks.

    python3 -m zerodha login      # open the printed URL, log in, paste the redirect URL back
    python3 -m zerodha status     # is today's token valid? available margin
"""
from __future__ import annotations

import argparse
import sys

from .auth import (LoginRequired, access_token, connected_kite, exchange_request_token, extract_request_token,
                   load_session, login_url, save_session)
from .broker import KiteBroker
from .config import ZerodhaConfig


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m zerodha", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["login", "status"])
    args = ap.parse_args(argv)
    cfg = ZerodhaConfig.from_env()

    if args.command == "login":
        print("1. Open this URL and log in to Kite:\n  ", login_url(cfg))
        print("2. After login you are redirected to your app's redirect URL.")
        raw = input("   Paste that full URL (or just the request_token): ")
        session = exchange_request_token(cfg, extract_request_token(raw))
        save_session(cfg.token_file, session)
        print(f"Saved session for {session.user_id} to {cfg.token_file} (valid until {session.expires_at():%Y-%m-%d %H:%M} IST)")
        return 0

    s = load_session(cfg.token_file)
    try:
        access_token(cfg)
    except LoginRequired as exc:
        print(exc)
        return 1
    print(f"Token OK for {s.user_id if s else '(KITE_ACCESS_TOKEN)'}; dry_run={cfg.dry_run}, product={cfg.product}")
    print(f"Available equity margin: ₹{KiteBroker(connected_kite(cfg)).available_margin():,.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
