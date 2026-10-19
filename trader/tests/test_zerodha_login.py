"""Zerodha login from the web page: callback caught inside the trader, token saved and handed to the running app."""
from __future__ import annotations

import socket
import time
import urllib.request
from dataclasses import replace

import pytest

from trader.zerodha_login import ZerodhaLogin
from zerodha import auth
from zerodha.auth import KiteSession, load_session
from zerodha.config import ZerodhaConfig


def free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def wait(pred, s=5.0):
    end = time.time() + s
    while time.time() < end and not pred():
        time.sleep(0.05)
    return pred()


def test_login_from_the_page_saves_and_hands_over_the_token(tmp_path, monkeypatch):
    port = free_port()
    cfg = replace(ZerodhaConfig(), api_key="k", api_secret="s", token_file=tmp_path / "tok.json",
                  redirect_url=f"http://127.0.0.1:{port}/kite/callback")
    monkeypatch.setattr(auth, "exchange_request_token",
                        lambda c, rt: KiteSession(access_token=f"AT-{rt}", user_id="AB1234", created_at="2026-10-18T09:00:00+05:30"))
    got = []
    zl = ZerodhaLogin(cfg, got.append)
    r = zl.start("http://algo.local:8765/")
    assert "api_key=k" in r["url"] and zl.status["state"] == "waiting"
    assert zl.start()["url"] == r["url"]                          # a second click while waiting: same login
    page = urllib.request.urlopen(f"http://127.0.0.1:{port}/kite/callback?request_token=RT1&status=success").read().decode()
    assert "window.close" in page and "algo.local:8765" in page
    assert wait(lambda: zl.status["state"] == "ok")
    assert got == ["AT-RT1"] and load_session(cfg.token_file).access_token == "AT-RT1"
    assert "AB1234" in zl.status["detail"]




def test_breeze_login_from_the_page_get_or_post(tmp_path):
    from trader.breeze_login import BreezeLogin
    from trading_data.breeze.session_store import load_session as load_breeze
    for method in ("GET", "POST"):
        port = free_port()
        got = []
        bl = BreezeLogin("key 1", tmp_path / f"breeze-{method}.json", got.append,
                         redirect_url=f"http://127.0.0.1:{port}/breeze/callback")
        r = bl.start("http://algo.local:8765/")
        assert "api_key=key+1" in r["url"] and bl.status["state"] == "waiting"
        base = f"http://127.0.0.1:{port}/breeze/callback"
        req = (urllib.request.Request(base + "?apisession=S123") if method == "GET"
               else urllib.request.Request(base, data=b"apisession=S123", method="POST"))
        assert "window.close" in urllib.request.urlopen(req).read().decode()
        assert wait(lambda: bl.status["state"] == "ok"), bl.status
        assert got == ["S123"] and load_breeze(tmp_path / f"breeze-{method}.json", environ={}).token == "S123"


def test_breeze_redirect_must_be_local():
    from trader.breeze_login import BreezeLogin
    with pytest.raises(ValueError, match="local"):
        BreezeLogin("k", "x.json", lambda t: None, redirect_url="https://example.com/cb").start()


def test_breeze_shared_redirect_borrowed_when_free_else_paste(tmp_path):
    from trader.breeze_login import BreezeLogin
    from trading_data.breeze.session_store import load_session as load_breeze
    port = free_port()
    got = []
    bl = BreezeLogin("k", tmp_path / "b.json", got.append, redirect_url=f"http://localhost:{port}/")
    assert bl.start()["manual"] is False                     # the other app isn't running: listen on its port
    urllib.request.urlopen(f"http://127.0.0.1:{port}/?apisession=AB12")
    assert wait(lambda: bl.status["state"] == "ok") and got == ["AB12"]
    # the other app IS running on that port: no listener, paste the ICICI tab's address
    s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(1); busy = s.getsockname()[1]
    try:
        bl2 = BreezeLogin("k", tmp_path / "b2.json", got.append, redirect_url=f"http://localhost:{busy}/")
        r = bl2.start()
        assert r["manual"] is True and bl2.status["state"] == "manual"
        with pytest.raises(ValueError):
            bl2.submit_url("http://localhost:3000/?nothing=1")
        bl2.submit_url(f"http://localhost:{busy}/?apisession=XY99")
        assert bl2.status["state"] == "ok" and load_breeze(tmp_path / "b2.json", environ={}).token == "XY99"
    finally:
        s.close()


def test_zerodha_paste_url_or_bare_token(tmp_path, monkeypatch):
    cfg = replace(ZerodhaConfig(), api_key="k", api_secret="s", token_file=tmp_path / "tok.json",
                  redirect_url=f"http://127.0.0.1:{free_port()}/kite/callback")
    monkeypatch.setattr(auth, "exchange_request_token",
                        lambda c, rt: KiteSession(access_token=f"AT-{rt}", user_id="AB1234", created_at="2026-10-18T09:00:00+05:30"))
    got = []
    zl = ZerodhaLogin(cfg, got.append)
    zl.submit_url("http://127.0.0.1:5678/kite/callback?action=login&type=login&status=success&request_token=RQ7")
    zl.submit_url("  RQ8 ")
    assert got == ["AT-RQ7", "AT-RQ8"] and zl.status["state"] == "ok"
    with pytest.raises(ValueError):
        zl.submit_url("http://x/?status=success")


def test_zerodha_busy_port_means_paste(tmp_path):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(1); port = s.getsockname()[1]
    try:
        cfg = replace(ZerodhaConfig(), api_key="k", api_secret="s", token_file=tmp_path / "t.json",
                      redirect_url=f"http://127.0.0.1:{port}/kite/callback")
        r = ZerodhaLogin(cfg, lambda t: None).start()
        assert r["manual"] is True and "paste" in r["status"]["detail"]
    finally:
        s.close()
