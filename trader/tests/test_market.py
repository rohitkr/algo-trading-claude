"""BreezeQuotes: budget/cache bookkeeping is shared state and must stay correct under concurrent use, but
the actual network call must NOT serialize behind it - see market.py's _quote() docstring for why (a
"refresh all legs" click fires one Breeze call per leg at once; if they queued up behind each other for
the full network round-trip each, 4 legs at ~0.2s each would take ~0.8s instead of ~0.2s)."""
from __future__ import annotations

import threading
import time as _time
from datetime import date

from trader.market import BreezeQuotes


class SlowClient:
    """A fake Breeze client whose get_quote() takes a fixed, measurable amount of wall-clock time."""

    def __init__(self, delay=0.15, fail_for=()):
        self.delay, self.fail_for = delay, set(fail_for)
        self.calls = []
        self._lock = threading.Lock()

    def get_quote(self, req):
        _time.sleep(self.delay)
        with self._lock:
            self.calls.append(req)
        if req in self.fail_for:
            raise RuntimeError("simulated Breeze error")
        return {"ltp": 100.0 + (hash(req) % 10)}


class FakeStore:
    def __init__(self):
        self.n = 0

    def api_calls_on(self, day: date) -> int:
        return self.n


def _quotes(client=None, **kw):
    return BreezeQuotes(settings=None, usage_store=FakeStore(), daily_budget=1000, client=client, **kw)


def test_concurrent_fetches_for_different_symbols_run_in_parallel():
    client = SlowClient(delay=0.15)
    bq = _quotes(client)
    results: dict[str, float | None] = {}

    def fetch(sym):
        results[sym] = bq._quote(sym, lambda s=sym: s, max_age=0)

    symbols = [f"SYM{i}" for i in range(4)]
    threads = [threading.Thread(target=fetch, args=(s,)) for s in symbols]
    t0 = _time.monotonic()
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    elapsed = _time.monotonic() - t0

    assert all(results[s] is not None for s in symbols)
    assert bq.calls == 4
    # Serialized, 4 x 0.15s = 0.6s; run concurrently they should finish close to one call's delay.
    assert elapsed < 0.4, f"took {elapsed:.2f}s - looks like calls are serialized behind the lock"


def test_cache_and_budget_bookkeeping_still_correct_under_concurrency():
    client = SlowClient(delay=0.05)
    bq = _quotes(client)

    def fetch_twice(sym):
        bq._quote(sym, lambda s=sym: s, max_age=0)
        bq._quote(sym, lambda s=sym: s, max_age=999)   # cached: must NOT trigger another network call

    threads = [threading.Thread(target=fetch_twice, args=(f"SYM{i}",)) for i in range(3)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert bq.calls == 3            # one real Breeze call per distinct symbol, the second hit the cache
    assert set(client.calls) == {"SYM0", "SYM1", "SYM2"}


def test_error_in_one_fetch_does_not_corrupt_bookkeeping_for_others():
    client = SlowClient(delay=0.05, fail_for={"BAD"})
    bq = _quotes(client)
    results = {}

    def fetch(sym):
        results[sym] = bq._quote(sym, lambda s=sym: s, max_age=0)

    # Sequential, not concurrent: last_error is one shared field (a later success clears it, same as
    # before this change) - this test is only about one call's failure not corrupting or blocking a
    # later, unrelated call, not about concurrent-write ordering.
    fetch("BAD")
    assert results["BAD"] is None
    assert bq.errors == 1
    assert "BAD" in bq.last_error

    fetch("GOOD")
    assert results["GOOD"] is not None
