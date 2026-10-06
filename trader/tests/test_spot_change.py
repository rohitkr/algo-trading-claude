"""Spot % change vs the previous close (2026-10-06): one lookup per underlying per day, failures retried later."""
from __future__ import annotations

from .test_service import Rig


def test_spot_shows_change_vs_previous_close_and_caches_it(tmp_path):
    r = Rig(tmp_path)
    r.quotes.set("SPOT:NIFTY", 22776.10)
    calls = []
    r.svc.prev_close_fn = lambda u: calls.append(u) or 22556.75
    s = r.svc.spot("NIFTY", with_ltp=True)
    assert s["prev_close"] == 22556.75 and s["change_pct"] == 0.97
    r.svc.spot("NIFTY", with_ltp=True)
    assert calls == ["NIFTY"]                              # cached for the day


def test_no_previous_close_means_no_change_and_a_later_retry(tmp_path):
    r = Rig(tmp_path)
    r.quotes.set("SPOT:NIFTY", 22776.10)
    calls = []

    def boom(u):
        calls.append(u)
        raise RuntimeError("kite down")
    r.svc.prev_close_fn = boom
    assert r.svc.spot("NIFTY", with_ltp=True)["change_pct"] is None
    r.svc.spot("NIFTY", with_ltp=True)
    assert len(calls) == 1                                 # not hammered: retried after a minute
    r.clock.advance(61)
    r.svc.spot("NIFTY", with_ltp=True)
    assert len(calls) == 2
