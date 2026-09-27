"""Command line: daily login and quick account checks.

    python3 -m zerodha login      # opens Kite login in the browser; the redirect is caught on KITE_REDIRECT_URL
    python3 -m zerodha login --manual       # old flow: paste the redirect URL into the terminal
    python3 -m zerodha status     # is today's token valid? available margin
"""
from __future__ import annotations

import argparse
import sys
import webbrowser

from .auth import (LoginRequired, access_token, connected_kite, exchange_request_token, extract_request_token,
                   load_session, login_url, save_session, start_callback_server, wait_for_request_token)
from .broker import KiteBroker
from .config import ZerodhaConfig


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m zerodha", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["login", "status"])
    ap.add_argument("--manual", action="store_true", help="login: paste the redirect URL instead of catching it")
    ap.add_argument("--no-browser", action="store_true", help="login: print the URL but do not open a browser")
    ap.add_argument("--timeout", type=float, default=180.0, help="login: seconds to wait for the redirect (180)")
    args = ap.parse_args(argv)
    cfg = ZerodhaConfig.from_env()

    if args.command == "login":
        url = login_url(cfg)
        try:
            token = None if args.manual else _catch_request_token(cfg, url, args.timeout, not args.no_browser)
        except ValueError as exc:          # Kite redirected with status != success
            print(f"Login failed: {exc}")
            return 1
        if token is None:
            token = _paste_request_token(url)
        session = exchange_request_token(cfg, token)
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


def _catch_request_token(cfg: ZerodhaConfig, url: str, timeout_s: float, open_browser: bool) -> str | None:
    """Catch the redirect with a local listener; None means fall back to pasting."""
    try:
        srv = start_callback_server(cfg.redirect_url)
    except (ValueError, OSError) as exc:
        print(f"Cannot listen on KITE_REDIRECT_URL={cfg.redirect_url} ({exc}); falling back to paste.")
        return None
    print(f"Listening on {cfg.redirect_url} (must match the Redirect URL on developers.kite.trade).")
    print("Log in to Kite at:\n  ", url)
    if open_browser and not webbrowser.open(url):
        print("   (could not open a browser; open the URL above yourself)")
    print(f"Waiting up to {timeout_s:.0f}s for the redirect...")
    token = wait_for_request_token(srv, timeout_s)
    if token is None:
        print("Timed out waiting for the redirect; falling back to paste.")
    return token


def _paste_request_token(url: str) -> str:
    print("1. Open this URL and log in to Kite:\n  ", url)
    print("2. After login you are redirected to your app's redirect URL.")
    return extract_request_token(input("   Paste that full URL (or just the request_token): "))


if __name__ == "__main__":
    sys.exit(main())
