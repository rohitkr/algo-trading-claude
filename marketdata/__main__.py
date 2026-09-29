"""Read-only Kite market-data smoke test (never places, modifies or cancels orders).

    python3 -m marketdata smoke [--seconds 15]

Checks, with today's saved Kite session: REST kite.ltp() by symbol and by instrument token, the index
tokens in marketdata.INDEX_TOKENS, KiteTicker connect + NIFTY ticks, an option's ticks, and 1-minute
historical data for the index and a live option. Exit code 0 only if every check passed.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timedelta


def smoke(seconds: float) -> int:
    from zerodha.config import ZerodhaConfig
    from zerodha.instruments import InstrumentBook

    from .config import MarketDataConfig
    from .kite_stream import INDEX_SYMBOLS, INDEX_TOKENS, build_kite_stream

    results: list[tuple[str, bool, str]] = []

    def check(name, fn):
        try:
            ok, detail = fn()
        except Exception as exc:
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        results.append((name, ok, detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
        return ok

    stream = build_kite_stream(MarketDataConfig.from_env(environ={"MARKET_DATA_PROVIDER": "KITE"}), ZerodhaConfig.from_env())
    kite = stream.kite_factory()
    check("session", lambda: (True, f"user {kite.profile().get('user_id')}"))

    def index_tokens():
        r = kite.ltp(list(INDEX_SYMBOLS.values()))
        bad = {u: r.get(s, {}).get("instrument_token") for u, s in INDEX_SYMBOLS.items()
               if r.get(s, {}).get("instrument_token") != INDEX_TOKENS[u]}
        return not bad, "all match" if not bad else f"mismatch {bad}"
    check("REST ltp + index tokens", index_tokens)
    check("REST ltp by token", lambda: (lambda p: (bool(p.get(256265)), f"NIFTY {p.get(256265)}"))(stream.rest_ltp([256265])))

    book = InstrumentBook.from_kite(kite, "data", "NFO", date.today())
    exp = book.expiries("NIFTY")[0]
    strikes = book.strikes("NIFTY", exp)
    try:
        spot = kite.ltp(["NSE:NIFTY 50"])["NSE:NIFTY 50"]["last_price"]
    except Exception:
        spot = strikes[len(strikes) // 2]
    opt = book.option("NIFTY", exp, min(strikes, key=lambda k: abs(k - spot)), "CALL")

    got: list[dict] = []
    stream.add_listener(got.append)
    stream.start()
    stream.acquire([256265, opt.instrument_token], owner="smoke")
    deadline = time.time() + seconds
    while time.time() < deadline and not (stream.healthy() and any(opt.instrument_token in b for b in got)):
        time.sleep(0.5)
    st = stream.status()
    check("ticker connects + heartbeats", lambda: (st["streaming"], f"connected={st['connected']} last message "
                                                                     f"{st['last_message_age_s']}s ago, error={st['last_error']}"))
    check("NIFTY ticks", lambda: (any(256265 in b for b in got), f"last {stream.last(256265)}"))
    check(f"option ticks {opt.tradingsymbol}", lambda: (any(opt.instrument_token in b for b in got),
                                                        f"last {stream.last(opt.instrument_token)}"))
    now = datetime.now()
    check("historical NIFTY 1-minute", lambda: (lambda rows: (bool(rows), f"{len(rows)} rows, last {rows[-1] if rows else None}"))(
        kite.historical_data(256265, now - timedelta(days=5), now, "minute")))
    check(f"historical {opt.tradingsymbol} 1-minute", lambda: (lambda rows: (bool(rows), f"{len(rows)} rows"))(
        kite.historical_data(opt.instrument_token, now - timedelta(days=5), now, "minute")))
    stream.stop()
    failed = [n for n, ok, _ in results if not ok]
    print("\nALL PASSED" if not failed else f"\nFAILED: {', '.join(failed)}")
    return 0 if not failed else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m marketdata", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["smoke"])
    ap.add_argument("--seconds", type=float, default=15.0, help="how long to wait for ticks")
    args = ap.parse_args(argv)
    return smoke(args.seconds)


if __name__ == "__main__":
    sys.exit(main())
