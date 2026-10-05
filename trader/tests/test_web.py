"""Increments 5-6: HTTP API safety and an end-to-end PAPER trade through the web API."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from trader import lifecycle as L
from trader.app import App, AlreadyRunning, single_instance_lock
from trader.broker import LiveTradingNotEnabled, build_broker
from trader.config import TraderConfig
from trader.strategy import StrategyService
from trader.web.server import make_server

from .fakes import EXPIRY
from .test_service import SYM, Rig


@pytest.fixture
def web(tmp_path):
    r = Rig(tmp_path, paper_quotes="manual")
    app = App(r.cfg, r.svc, r.repo, r.ex, r.quotes, None, StrategyService(r.svc, r.repo))
    srv = make_server(app, _free_port())
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield r, f"http://127.0.0.1:{port}"
    srv.shutdown()
    srv.server_close()


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def call(base, path, body=None, headers=None):
    h = {} if body is None else {"Content-Type": "application/json", "X-Trader": "1"}
    h.update(headers or {})
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                 headers=h, method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if "json" in resp.headers.get("Content-Type", "") else raw)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


TRADE = dict(underlying="NIFTY", expiry=EXPIRY.isoformat(), strike=25000, option_type="CE", side="BUY", lots=2,
             entry_price=100, stop_loss=90, target=120, partial_enabled=True, partial_lots=1, partial_price=110)


def test_server_binds_loopback_only(web):
    r, base = web
    assert base.startswith("http://127.0.0.1:")


def test_get_never_places_orders(web):
    r, base = web
    for p in ("/", "/api/meta", "/api/dashboard", f"/api/strikes?underlying=NIFTY&expiry={EXPIRY}",
              "/api/preview", "/api/trades/1/confirm"):
        call(base, p)
    assert r.places() == []


def test_post_needs_ui_header_and_json(web):
    r, base = web
    req = urllib.request.Request(base + "/api/preview", data=json.dumps(TRADE).encode(),
                                 headers={"Content-Type": "text/plain"}, method="POST")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403


def test_bad_host_header_refused(web):
    r, base = web
    code, _ = call(base, "/api/dashboard", headers={"Host": "evil.example:80"})
    assert code == 403


def test_confirm_needs_token(web):
    r, base = web
    _, p = call(base, "/api/preview", TRADE)
    assert p["ok"], p
    code, _ = call(base, f"/api/trades/{p['trade_id']}/confirm", {"token": "guess"})
    assert code == 409 and r.places() == []


def test_end_to_end_paper_trade(web):
    """Preview -> confirm -> fill -> SL order -> partial booking -> target, all through HTTP."""
    r, base = web
    _, meta = call(base, "/api/meta")
    assert meta["underlyings"]["NIFTY"]["lot_size"] == 65
    _, c = call(base, f"/api/contract?underlying=NIFTY&expiry={EXPIRY}&strike=25000&option_type=CE")
    assert c["tradingsymbol"] == SYM and c["lot_size"] == 65
    _, p = call(base, "/api/preview", TRADE)
    assert p["ok"] and p["summary"]["quantity"] == 130 and p["summary"]["order_value"] == 13000
    code, res = call(base, f"/api/trades/{p['trade_id']}/confirm", {"token": p["token"]})
    assert code == 200
    tid = p["trade_id"]
    for price in (100, 110, 120):
        assert call(base, "/api/paper/price", {"tradingsymbol": SYM, "price": price})[0] == 200
        r.tick()
    _, d = call(base, "/api/dashboard")
    done = {t["id"]: t for t in d["completed"]}
    assert done[tid]["status"] == L.EXITED and done[tid]["exit_reason"] == L.TARGET_HIT
    assert done[tid]["pnl"] == pytest.approx(65 * 10 + 65 * 20)
    _, detail = call(base, f"/api/trades/{tid}")
    kinds = [o["kind"] for o in detail["orders"]]
    assert kinds.count("ENTRY") == 1 and kinds.count("PARTIAL") == 1 and kinds.count("EXIT") == 1
    assert d["system"]["broker"]["value"]["ok"]


def test_exit_via_api_needs_prepare_token(web):
    r, base = web
    r.price(100)
    _, p = call(base, "/api/preview", {**TRADE, "partial_enabled": False})
    call(base, f"/api/trades/{p['trade_id']}/confirm", {"token": p["token"]})
    r.tick()
    tid = p["trade_id"]
    code, _ = call(base, f"/api/trades/{tid}/exit", {"token": "nope"})
    assert code == 409 and r.trade(tid)["status"] == L.POSITION_ACTIVE
    _, prep = call(base, f"/api/trades/{tid}/prepare", {"action": "EXIT"})
    code, _ = call(base, f"/api/trades/{tid}/exit", {"token": prep["token"]})
    assert code == 200 and r.trade(tid)["status"] == L.EXITED
    code, _ = call(base, f"/api/trades/{tid}/exit", {"token": prep["token"]})   # replay
    assert code == 409


def test_live_needs_every_switch(tmp_path):
    cfg = TraderConfig(mode="LIVE", enable_live_trading=True, db_path=tmp_path / "x.sqlite")
    from zerodha.config import ZerodhaConfig
    with pytest.raises(LiveTradingNotEnabled, match="--live"):
        build_broker(cfg, cli_live=False, zcfg=ZerodhaConfig(dry_run=False), kite=object())
    with pytest.raises(LiveTradingNotEnabled, match="KITE_DRY_RUN"):
        build_broker(cfg, cli_live=True, zcfg=ZerodhaConfig(dry_run=True), kite=object())
    with pytest.raises(LiveTradingNotEnabled, match="ENABLE_LIVE"):
        build_broker(TraderConfig(mode="LIVE"), cli_live=True, zcfg=ZerodhaConfig(dry_run=False), kite=object())
    with pytest.raises(LiveTradingNotEnabled):
        build_broker(TraderConfig(), cli_live=True, paper_kite=object())
    b = build_broker(cfg, cli_live=True, zcfg=ZerodhaConfig(dry_run=False), kite=object())
    assert b.live


def test_config_defaults_to_paper(tmp_path):
    cfg = TraderConfig.from_env(env_file=None, environ={})
    assert cfg.mode == "PAPER" and not cfg.live
    cfg = TraderConfig.from_env(env_file=None, environ={"TRADER_TRADING_END": "", "TRADER_FREEZE_QTY_NIFTY": "900",
                                                        "TRADER_MAX_DAILY_LOSS": "5000"})
    assert cfg.trading_end is None and cfg.freeze_for("NIFTY") == 900 and cfg.max_daily_loss == 5000


def test_single_instance_lock(tmp_path):
    fh = single_instance_lock(tmp_path / "db.sqlite")
    with pytest.raises(AlreadyRunning):
        single_instance_lock(tmp_path / "db.sqlite")
    fh.close()


def test_trading_mode_mixup_is_explained(tmp_path):
    cfg = TraderConfig.from_env(env_file=None, environ={"TRADING_MODE": "LIVE"})
    assert cfg.mode == "PAPER"                         # the engine's switch never turns the trader LIVE
    with pytest.raises(LiveTradingNotEnabled, match="TRADER_MODE=LIVE"):
        build_broker(cfg, cli_live=True, paper_kite=object())
    assert TraderConfig.from_env(env_file=None, environ={"TRADER_MODE": "live"}).mode == "LIVE"


def test_ui_has_no_nested_form_in_dialog_and_no_typed_live():
    """Regression: the edit form was a <form> inside the dialog's <form>; browsers drop it and Edit did nothing."""
    from pathlib import Path
    js = (Path(__file__).parents[1] / "web" / "static" / "app.js").read_text()
    html = (Path(__file__).parents[1] / "web" / "static" / "index.html").read_text()
    assert '<form id="edit-form"' not in js and '<div id="edit-form"' in js
    assert "dlg-live-input" not in js and "dlg-live-input" not in html


def test_ui_answers_to_a_localhost_name_but_not_other_hosts(web):
    r, base = web
    port = base.rsplit(":", 1)[1]
    assert call(base, "/api/dashboard", headers={"Host": f"algotrade.localhost:{port}"})[0] == 200
    assert call(base, "/api/dashboard", headers={"Host": f"algo.local:{port}"})[0] == 200       # default name
    assert call(base, "/api/dashboard", headers={"Host": f"algotrade.com:{port}"})[0] == 403   # not configured
    assert call(base, "/api/dashboard", headers={"Host": f"evil.localhost.example:{port}"})[0] == 403
