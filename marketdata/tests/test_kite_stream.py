"""KiteStream without Kite: refcounted subscriptions, reconnect + resubscribe, stale detection, session
expiry / token rotation, REST fallback; KiteQuotes' Breeze fallback; KiteMarketDataProvider bars."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from marketdata import KiteStream
from marketdata.config import MarketDataConfig
from marketdata.kite_provider import KiteMarketDataProvider, candles_frame
from zerodha.auth import LoginRequired


class FakeTransport:
    def __init__(self, token, sink, log):
        self.token, self.sink, self.log = token, sink, log
        self.subs: list[tuple[list[int], str]] = []
        self.unsubs: list[list[int]] = []
        self.closed = False
        self.connected_calls = 0
        log.append(self)

    def connect(self):
        self.connected_calls += 1

    def subscribe(self, tokens, mode):
        self.subs.append((list(tokens), mode))

    def unsubscribe(self, tokens):
        self.unsubs.append(list(tokens))

    def close(self):
        self.closed = True

    def subscribed(self) -> set[int]:
        return {t for toks, _ in self.subs for t in toks}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Env:
    """A stream on a fake transport, fake clock and a settable access token."""

    def __init__(self, rest=None, stale_s=10.0):
        self.transports: list[FakeTransport] = []
        self.clock = Clock()
        self.token = "tok-1"
        self.rest_calls: list[list[int]] = []
        self.rest_prices = dict(rest or {})
        self.rest_fail = False
        self.slept: list[float] = []

        def provider():
            if self.token is None:
                raise LoginRequired("no valid Kite session")
            return self.token

        def rest(tokens):
            self.rest_calls.append(list(tokens))
            if self.rest_fail:
                raise RuntimeError("kite.ltp down")
            return {t: self.rest_prices[t] for t in tokens if t in self.rest_prices}

        def sleep(s):
            self.slept.append(s)
            self.clock.t += s

        self.stream = KiteStream(lambda tok, sink: FakeTransport(tok, sink, self.transports), provider,
                                 rest_ltp=rest, stale_s=stale_s, rest_min_interval_s=1.0, monotonic=self.clock,
                                 sleep=sleep)

    @property
    def t(self) -> FakeTransport:
        return self.transports[-1]

    def up(self):
        self.stream.start(watchdog=False)
        self.stream.on_connect()
        return self


# -- refcount -------------------------------------------------------------------------------------
def test_refcounted_subscriptions_subscribe_once_and_unsubscribe_on_last_release():
    e = Env().up()
    s = e.stream
    s.acquire([101], owner="a")
    s.acquire([101], owner="b")
    assert e.t.subs == [([101], "ltp")] and s.refcount(101) == 2
    s.acquire([101], owner="a")                   # same owner twice counts once
    assert s.refcount(101) == 2
    s.release([101], owner="a")
    assert e.t.unsubs == [] and s.refcount(101) == 1
    s.release([101], owner="b")
    assert e.t.unsubs == [[101]] and s.subscribed() == set()
    s.release([101], owner="b")                   # releasing again is harmless
    assert s.refcount(101) == 0


def test_hold_replaces_an_owners_set():
    e = Env().up()
    s = e.stream
    s.hold("trades", {1, 2, 3})
    s.hold("ui:1", {3, 4})
    s.hold("trades", {2, 3, 5})                   # drops 1, adds 5; 3 still held by ui:1 too
    assert s.subscribed() == {2, 3, 4, 5}
    assert [1] in e.t.unsubs
    s.drop_owner("ui:1")
    assert s.subscribed() == {2, 3, 5} and s.refcount(3) == 1


def test_ticks_after_last_release_are_kept_as_last_price():
    e = Env().up()
    s = e.stream
    s.acquire([7])
    s.on_ticks([(7, 101.5)])
    s.release([7])
    assert s.last(7).price == 101.5


# -- reconnect ----------------------------------------------------------------------------------------
def test_subscriptions_made_while_down_are_sent_on_connect():
    e = Env()
    e.stream.start(watchdog=False)
    e.stream.acquire([11, 12])
    assert e.t.subs == []                          # not connected yet: nothing sent
    e.stream.on_connect()
    assert e.t.subscribed() == {11, 12}


def test_reconnect_resubscribes_the_whole_refcounted_set():
    e = Env().up()
    s = e.stream
    s.acquire([1, 2])
    s.on_close(1006, "connection lost")
    s.on_reconnect(1)
    assert not s.healthy()
    s.acquire([3])                                  # added during the outage
    s.release([1])
    e.t.subs.clear()
    s.on_connect()                                  # KiteTicker reconnected on the same transport
    assert e.t.subs == [([2, 3], "ltp")]
    assert s.reconnects == 1 and s.connects == 2


def test_watchdog_rebuilds_when_kiteticker_gives_up():
    e = Env().up()
    e.stream.acquire([5])
    first = e.t
    e.stream.on_noreconnect()
    e.stream.check()
    assert first.closed and len(e.transports) == 2 and e.t.connected_calls == 1
    e.stream.on_connect()
    assert e.t.subscribed() == {5}


def test_stale_stream_falls_back_to_rest_and_is_rebuilt():
    e = Env(rest={9: 55.0}).up()
    s = e.stream
    s.acquire([9])
    s.on_ticks([(9, 50.0)])
    assert s.ltp(9) == 50.0 and e.rest_calls == []
    e.clock.t += 5
    s.on_message()                                  # heartbeat keeps it healthy
    e.clock.t += 9
    assert s.ltp(9) == 50.0
    e.clock.t += 2                                  # 11s without a message > stale_s
    assert not s.healthy()
    assert s.ltp(9) == 55.0 and e.rest_calls == [[9]]
    s.check()
    assert s.stale_rebuilds == 1 and len(e.transports) == 2 and e.transports[0].closed


# -- session -----------------------------------------------------------------------------------------------
def test_new_login_token_rebuilds_the_connection():
    e = Env().up()
    e.token = "tok-2"
    e.stream.check()
    assert len(e.transports) == 2 and e.t.token == "tok-2" and e.transports[0].closed


def test_expired_session_stops_ticker_until_a_new_login():
    e = Env(rest={4: 12.0}).up()
    s = e.stream
    s.acquire([4])
    s.on_ticks([(4, 10.0)])
    e.token = None                                   # 06:00 IST: saved token no longer valid
    s.check()
    assert s.session_expired and e.t.closed and not s.healthy()
    e.clock.t += 1
    assert s.ltp(4) == 12.0                          # REST (it will also fail in reality, then...)
    e.rest_fail = True
    e.clock.t += 5
    assert s.ltp(4) == 12.0                          # ... the newest price we ever saw
    e.token = "tok-new"
    s.check()
    assert not s.session_expired and len(e.transports) == 2 and e.t.token == "tok-new"


def test_token_rejected_by_kite_is_not_retried_until_it_changes():
    e = Env().up()
    s = e.stream
    s.on_error(0, "WebSocket connection upgrade failed (403 - Forbidden)")
    assert s.session_expired and e.t.closed
    s.check()
    assert len(e.transports) == 1                   # same token: no reconnect storm
    e.token = "tok-2"
    s.check()
    assert len(e.transports) == 2 and not s.session_expired


# -- fallback / REST ---------------------------------------------------------------------------------------------
def test_unsubscribed_or_untickled_token_uses_rest_and_caches_it():
    e = Env(rest={21: 30.0, 22: 31.0}).up()
    s = e.stream
    assert s.ltp(21) == 30.0 and e.rest_calls == [[21]]
    assert s.ltp(21, max_age=5) == 30.0 and len(e.rest_calls) == 1          # REST cache
    s.acquire([22])                                                            # subscribed, no tick yet
    e.clock.t += 2
    assert s.ltp(22) == 31.0


def test_rest_is_rate_limited_and_batches_everything_the_stream_cannot_vouch_for():
    e = Env(rest={1: 1.0, 2: 2.0, 3: 3.0})
    s = e.stream
    s.start(watchdog=False)                         # never connects
    s.acquire([1, 2, 3])
    assert s.ltp(1) == 1.0
    assert e.rest_calls == [[1, 2, 3]]
    e.clock.t += 0.4
    assert s.ltp(2, max_age=0) == 2.0               # 2nd call inside 1s waits for the slot (fake sleep)
    assert e.slept and len(e.rest_calls) == 2


def test_rest_failure_returns_last_tick():
    e = Env().up()
    s = e.stream
    s.acquire([8])
    s.on_ticks([(8, 70.0)])
    e.rest_fail = True
    s.on_close(1006, "gone")
    assert s.ltp(8) == 70.0 and s.rest_errors == 1


def test_listeners_get_batches_and_bad_prices_are_ignored():
    e = Env().up()
    got = []
    e.stream.add_listener(got.append)
    e.stream.add_listener(lambda b: 1 / 0)          # a broken listener must not break the others
    e.stream.on_ticks([(1, 10.0), (2, 0.0), (3, None)])
    assert got == [{1: 10.0}]


# -- KiteQuotes -----------------------------------------------------------------------------------------------
class Inst:
    def __init__(self, sym, token):
        self.tradingsymbol, self.instrument_token = sym, token


class FakeFallback:
    name = "breeze"

    def __init__(self):
        self.calls = []

    def ltp(self, inst, max_age=None):
        self.calls.append(inst.tradingsymbol)
        return 99.0

    def spot(self, underlying, max_age=None):
        return 25000.0

    def status(self):
        return {"source": "breeze", "ok": True, "api_budget_remaining": 1000}


def test_kite_quotes_streams_asked_instruments_and_releases_idle_ones():
    from trader.market import KiteQuotes
    e = Env(rest={}).up()
    q = KiteQuotes(e.stream, idle_s=60, monotonic=e.clock)
    e.stream.on_ticks([(501, 42.0)])
    q.ltp(Inst("A", 501))
    e.stream.on_ticks([(501, 42.5)])
    assert q.ltp(Inst("A", 501)) == 42.5 and e.stream.refcount(501) == 1
    e.clock.t += 61
    e.stream.on_message()
    q.ltp(Inst("B", 502))                            # any request prunes idle ones
    assert e.stream.refcount(501) == 0 and e.stream.refcount(502) == 1


def test_kite_quotes_breeze_fallback_only_when_kite_has_nothing():
    from trader.market import KiteQuotes
    e = Env(rest={}).up()
    fb = FakeFallback()
    q = KiteQuotes(e.stream, fallback=fb, monotonic=e.clock)
    assert q.ltp(Inst("X", 900)) == 99.0 and fb.calls == ["X"]
    e.stream.on_ticks([(900, 12.0)])
    assert q.ltp(Inst("X", 900)) == 12.0 and fb.calls == ["X"]
    assert q.spot("NIFTY") == 25000.0                   # no NIFTY tick/REST price -> fallback
    st = q.status()
    assert st["source"] == "kite" and st["fallback"]["used"] == 2


def test_market_data_config_from_env():
    assert MarketDataConfig.from_env(env_file=None, environ={}).provider == "BREEZE"
    c = MarketDataConfig.from_env(env_file=None, environ={"MARKET_DATA_PROVIDER": "kite",
                                                           "MARKET_DATA_FALLBACK": "breeze", "KITE_STALE_SECONDS": "5"})
    assert c.kite and c.breeze_fallback and c.stale_s == 5.0
    with pytest.raises(ValueError):
        MarketDataConfig.from_env(env_file=None, environ={"MARKET_DATA_PROVIDER": "yahoo"})


# -- KiteMarketDataProvider -------------------------------------------------------------------------------------------
class FakeKite:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def historical_data(self, token, start, end, interval):
        self.calls.append((token, start, end, interval))
        return [r for r in self.rows if start <= r["date"].replace(tzinfo=None) <= end]


class FakeBook:
    def option(self, underlying, expiry, strike, right):
        class I:
            instrument_token = 7001
        return I()


def _bar(ts: datetime, px: float) -> dict:
    from datetime import timezone
    ist = timezone(timedelta(hours=5, minutes=30))
    return {"date": ts.replace(tzinfo=ist), "open": px, "high": px + 1, "low": px - 1, "close": px, "volume": 10}


def test_kite_provider_serves_completed_bars_incrementally_and_ticks_for_fresh_prices():
    from trading_data.config import load_settings
    from trading_data.storage import OptionContract
    settings = load_settings()
    d = date(2026, 9, 29)
    rows = [_bar(datetime(2026, 9, 29, 9, 15) + timedelta(minutes=i), 100 + i) for i in range(10)]
    kite = FakeKite(rows)
    e = Env(rest={}).up()
    clock = Clock()
    p = KiteMarketDataProvider(lambda: kite, e.stream, settings, "NIFTY", book_factory=lambda today: FakeBook(),
                               min_refetch_s=5, historical_min_interval_s=0, monotonic=clock, sleep=lambda s: None)
    assert e.stream.refcount(256265) == 1                    # NIFTY 50 index streamed for the engine
    now = datetime(2026, 9, 29, 9, 20, 30)
    bars = p.spot_bars(d, now)
    assert list(bars.index) == [datetime(2026, 9, 29, 9, 15) + timedelta(minutes=i) for i in range(5)]
    assert kite.calls[-1][0] == 256265
    clock.t += 1
    p.spot_bars(d, datetime(2026, 9, 29, 9, 21, 5))         # new bar due but refetched <5s ago: cache
    assert len(kite.calls) == 1
    clock.t += 10
    bars = p.spot_bars(d, datetime(2026, 9, 29, 9, 22, 5))
    assert bars.index[-1] == datetime(2026, 9, 29, 9, 21) and kite.calls[-1][1] == datetime(2026, 9, 29, 9, 20)
    c = OptionContract("NIFTY", "NFO", date(2026, 9, 30), 25000, "CALL")
    e.stream.on_ticks([(7001, 88.0)])
    assert p.option_price(c, now, fresh=True) == 88.0 and e.stream.refcount(7001) == 1
    assert p.option_price(c, now) == 104.0                    # not fresh: last completed bar close (7001 rows)
    assert p.api_budget_remaining() is None


def test_candles_frame_drops_out_of_session_and_insane_rows():
    d = date(2026, 9, 29)
    rows = [_bar(datetime(2026, 9, 29, 9, 14), 1), _bar(datetime(2026, 9, 29, 9, 15), 2),
            _bar(datetime(2026, 9, 29, 15, 29), 3), _bar(datetime(2026, 9, 29, 15, 30), 4),
            {**_bar(datetime(2026, 9, 29, 9, 16), 5), "low": 9}]
    df = candles_frame(rows, d, time(9, 15), time(15, 29))
    assert list(df["close"]) == [2.0, 3.0]
