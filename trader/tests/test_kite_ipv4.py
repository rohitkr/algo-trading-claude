"""Kite calls leave over IPv4 only (2026-10-05: an order was refused because the Mac sent it from a temporary
IPv6 address that is not the static IP registered with Kite)."""
from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

from zerodha.auth import ipv4_adapter, new_kite
from zerodha.config import ZerodhaConfig


def test_kite_session_uses_the_ipv4_adapter():
    kite = new_kite(ZerodhaConfig(api_key="k"))
    assert type(kite.reqsession.get_adapter("https://api.kite.trade")).__name__ == "IPv4Adapter"
    kite = new_kite(ZerodhaConfig(api_key="k", ipv4_only=False))
    assert type(kite.reqsession.get_adapter("https://api.kite.trade")).__name__ == "HTTPAdapter"


def test_ipv4_only_session_skips_ipv6_and_connects_over_ipv4():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(self.client_address[0].encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        s = requests.Session()
        s.mount("http://", ipv4_adapter())
        # "localhost" resolves to ::1 first on macOS; the IPv4 adapter must still reach the IPv4 server
        r = s.get(f"http://localhost:{srv.server_port}/", timeout=5)
        assert r.status_code == 200 and socket.inet_aton(r.text)          # the server saw an IPv4 client
    finally:
        srv.shutdown()
