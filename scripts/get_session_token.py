"""Log in to ICICI Direct / Breeze (manual OTP) and store today's session token.

    python3 scripts/get_session_token.py            # browser opens, you type the OTP in the terminal
    python3 scripts/get_session_token.py --manual   # log in yourself, paste the redirected URL
    python3 scripts/get_session_token.py --check    # only verify the stored token
"""
import argparse
import sys

import _path  # noqa: F401

from trading_data.app import EXIT_ERROR, EXIT_OK, EXIT_SESSION, bootstrap
from trading_data.breeze.auth import LoginError, browser_login, manual_login
from trading_data.breeze.client import BreezeClient, BreezeError, SessionExpiredError
from trading_data.breeze.session_store import load_session, save_session
from trading_data.config import ConfigError
from trading_data.log import get_logger, register_secret


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manual", action="store_true", help="no browser automation: paste the redirected URL")
    ap.add_argument("--headless", action="store_true", help="run the automated browser without a window")
    ap.add_argument("--check", action="store_true", help="only verify the stored session token")
    ap.add_argument("--no-verify", action="store_true", help="save the token without opening a test session")
    args = ap.parse_args()

    settings, store = bootstrap("auth")
    log = get_logger("auth")
    creds = settings.credentials
    path = settings.paths.session_file

    if not args.check:
        try:
            token = manual_login(creds) if args.manual else browser_login(creds, headless=args.headless)
        except (LoginError, ConfigError) as exc:
            log.error("Login failed: %s", exc)
            return EXIT_ERROR
        register_secret(token)
        save_session(path, token, "manual" if args.manual else "browser")
        log.info("Session token saved to %s (valid for today only)", path.relative_to(settings.paths.log_dir.parent))
        if args.no_verify:
            return EXIT_OK

    session = load_session(path)
    if session is None:
        log.error("No stored session token. Run without --check first.")
        return EXIT_SESSION
    try:
        BreezeClient(settings, store).connect()
    except SessionExpiredError as exc:
        log.error("%s", exc)
        return EXIT_SESSION
    except (BreezeError, ConfigError) as exc:
        log.error("Session check failed: %s", exc)
        return EXIT_ERROR
    log.info("Breeze session verified (token from %s, source: %s)", session.created_on, session.source)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
