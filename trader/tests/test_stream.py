"""TickHub (trader/stream.py): per-tick trade P&L, per-page price subscriptions, dashboard-change events, and
the /api/stream SSE endpoint."""
from __future__ import annotations

import http.client
import json
import threading

import pytest

from marketdata.tests.test_kite_stream import Env
from trader import lifecycle as L
from trader.app import App
from trader.strategy import StrategyService
from trader.stream import TickHub
from trader.web.server import make_server

from .test_service import SYM, Rig
from .test_web import _free_port, call

TOKEN = 1003                                          # fakes.rows(): NIFTY 25000 CE


class StreamQuotes:
    """The parts of KiteQuotes the hub uses, over a fake-transport KiteStream."""
    name = "kite"

    def __init__(self, stream):
        self.stream = stream

    def spot_token(self, underlying):
        return {"NIFTY": 256265}[underlying]

    def status(self):
        return self.stream.status()


def drain(client) -> list[tuple[str, dict]]:
    out = []
    while not client.q.empty():
        out.append(client.q.get_nowait())
    return out


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    r.price(100)
    tid = r.open_trade()                                # BUY 1 lot (65) @ 100
    r.tick()
    assert r.trade(tid)["status"] == L.POSITION_ACTIVE
    e = Env().up()
    hub = TickHub(r.svc, StreamQuotes(e.stream))
    return r, e, hub, tid


def test_ticks_push_live_pnl_for_open_trades(rig):
    r, e, hub, tid = rig
    c = hub.connect()
    hub.after_tick()
    assert e.stream.refcount(TOKEN) == 1               # every trade of the day is streamed
    evs = drain(c)
    assert evs[0][0] == "hello" and ("dashboard", {"version": 1}) in evs
    e.stream.on_ticks([(TOKEN, 110.0)])
    hub.flush()
    [(ev, d)] = drain(c)
    assert ev == "ticks"
    t = d["trades"][tid]
    assert (t["ltp"], t["pnl"], t["pnl_pct"]) == (110.0, 650.0, 10.0)
    e.stream.on_ticks([(TOKEN, 95.0)])
    e.stream.on_ticks([(TOKEN, 96.0)])                 # coalesced: one message, newest price
    hub.flush()
    [(_, d)] = drain(c)
    assert d["trades"][tid]["pnl"] == pytest.approx(-4 * 65)
    hub.flush()
    assert drain(c) == []                               # nothing new, nothing sent


def test_price_only_changes_do_not_signal_dashboard_but_state_changes_do(rig):
    r, e, hub, tid = rig
    c = hub.connect()
    r.tick()                                            # let the SL order's own status event settle
    hub.after_tick()
    drain(c)
    r.price(105)
    r.tick()                                            # monitor marks a new LTP / unrealised P&L
    hub.after_tick()
    assert [ev for ev, _ in drain(c)] == []
    r.price(130)                                        # target: the trade exits
    r.tick()
    hub.after_tick()
    assert [ev for ev, _ in drain(c)] == ["dashboard"]
    assert r.trade(tid)["status"] == L.EXITED
    e.stream.on_ticks([(TOKEN, 140.0)])                 # closed trade: LTP still live, P&L stays realised
    hub.flush()
    [(_, d)] = drain(c)
    assert d["trades"][tid]["ltp"] == 140.0 and d["trades"][tid]["pnl"] == pytest.approx(30 * 65)


def test_page_watch_holds_its_keys_and_releases_them_on_disconnect(rig):
    r, e, hub, tid = rig
    c = hub.connect()
    e.stream.on_ticks([(256265, 25010.0)])
    res = hub.watch(c.id, [f"NFO:{SYM}", "SPOT:NIFTY", "NFO:NOPE"])
    assert res["keys"] == [f"NFO:{SYM}", "SPOT:NIFTY"] and "NFO:NOPE" in res["errors"]
    assert e.stream.refcount(256265) == 1
    assert ("ticks", {"prices": {"SPOT:NIFTY": 25010.0}, "trades": {}, "strategies": {}}) in drain(c)
    other = hub.connect()
    e.stream.on_ticks([(256265, 25020.0)])
    hub.flush()
    assert drain(c)[-1][1]["prices"] == {"SPOT:NIFTY": 25020.0}
    assert all(not d.get("prices") for _, d in drain(other))     # other page did not ask for the spot
    hub.disconnect(c)
    assert e.stream.refcount(256265) == 0


def test_stalled_page_is_told_to_resync_instead_of_growing_a_backlog(rig):
    r, e, hub, tid = rig
    c = hub.connect()
    for i in range(600):
        hub._put(c, "ticks", {"i": i})
    evs = drain(c)
    assert evs[0][0] == "dashboard" and evs[0][1]["resync"] and len(evs) < 500


def test_breeze_provider_pushes_monitor_marks_after_each_tick(tmp_path):
    r = Rig(tmp_path)
    r.price(100)
    tid = r.open_trade()
    r.tick()
    r.price(104)
    r.tick()
    hub = TickHub(r.svc, r.quotes)                      # ManualQuotes: no stream
    c = hub.connect()
    assert hub.watch(c.id, ["SPOT:NIFTY"])["streaming"] is False
    hub.after_tick()
    ticks = [d for ev, d in drain(c) if ev == "ticks"]
    assert ticks[-1]["trades"][tid]["ltp"] == 104.0 and ticks[-1]["trades"][tid]["pnl"] == pytest.approx(4 * 65)


def test_sse_endpoint_streams_hello_and_ticks(rig):
    r, e, hub, tid = rig
    app = App(r.cfg, r.svc, r.repo, r.ex, StreamQuotes(e.stream), None, StrategyService(r.svc, r.repo), e.stream, hub)
    port = _free_port()
    srv = make_server(app, port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/stream", headers={"Host": f"127.0.0.1:{port}"})
        resp = conn.getresponse()
        assert resp.status == 200 and resp.getheader("Content-Type").startswith("text/event-stream")

        def next_event():
            ev, data = None, None
            while True:
                line = resp.fp.readline().decode().rstrip("\n")
                if line.startswith("event: "):
                    ev = line[7:]
                elif line.startswith("data: "):
                    data = json.loads(line[6:])
                elif line == "" and ev:
                    return ev, data

        ev, hello = next_event()
        assert ev == "hello" and hello["streaming"] is True
        status, res = call(f"http://127.0.0.1:{port}", "/api/stream/watch", {"client": hello["client"], "keys": ["SPOT:NIFTY"]})
        assert status == 200 and res["keys"] == ["SPOT:NIFTY"]
        e.stream.on_ticks([(256265, 25100.5)])
        hub.flush()
        ev, d = next_event()
        assert ev == "ticks" and d["prices"] == {"SPOT:NIFTY": 25100.5}
        status, meta = call(f"http://127.0.0.1:{port}", "/api/meta")
        assert meta["streaming"] is True
        conn.close()
    finally:
        hub.stop()
        srv.shutdown()
        srv.server_close()
