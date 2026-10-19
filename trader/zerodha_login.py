"""Zerodha daily login from the web page (the red Zerodha light): the same flow as `./login.sh`, run inside the trader.

start() opens the Kite login callback listener (KITE_REDIRECT_URL, e.g. 127.0.0.1:5678) and returns the Kite login URL
for the page to open. If you are already logged in to Kite in that browser, Kite redirects straight back with a
request_token; it is exchanged for today's access token, saved to the token file (as the CLI does) and handed to the
running trader (on_token), so no restart is needed. The browser tab then returns to the trader page."""
from __future__ import annotations

import logging
import threading
from datetime import datetime

log = logging.getLogger("trader.zerodha_login")
TIMEOUT_S = 180.0


class ZerodhaLogin:
    def __init__(self, zcfg, on_token, clock=datetime.now):
        self.zcfg, self.on_token, self.clock = zcfg, on_token, clock
        self.status = {"state": "idle", "detail": "", "at": None}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def _set(self, state: str, detail: str = "") -> None:
        self.status = {"state": state, "detail": detail, "at": self.clock().isoformat(timespec="seconds")}

    def start(self, return_to: str | None = None) -> dict:
        from zerodha.auth import login_url, start_callback_server
        with self._lock:
            url = login_url(self.zcfg)
            if self._thread and self._thread.is_alive():
                return {"url": url, "status": self.status, "manual": False}   # already waiting: just reopen Kite
            try:
                srv = start_callback_server(self.zcfg.redirect_url)
            except (OSError, ValueError) as exc:                      # port busy / not a local URL: paste instead
                self._set("manual", f"cannot listen on {self.zcfg.redirect_url} ({exc}): paste the redirect address")
                return {"url": url, "status": self.status, "manual": True}
            srv.return_to = return_to
            self._set("waiting", "complete the Kite login in the browser tab")
            self._thread = threading.Thread(target=self._run, args=(srv,), name="zerodha-login", daemon=True)
            self._thread.start()
            return {"url": url, "status": self.status, "manual": False}

    def submit_url(self, text: str) -> dict:
        """The pasted Kite redirect address (…?request_token=…) or the bare request_token."""
        from zerodha.auth import extract_request_token
        try:
            token = extract_request_token(text or "")
        except ValueError:
            raise ValueError("no request_token found in that text") from None
        self._accept(token)
        return {"status": self.status}

    def _accept(self, request_token: str) -> None:
        from zerodha.auth import exchange_request_token, save_session
        session = exchange_request_token(self.zcfg, request_token)
        save_session(self.zcfg.token_file, session)
        self.on_token(session.access_token)
        note = " (KITE_ACCESS_TOKEN in .env overrides it: remove that line)" if self.zcfg.access_token else ""
        self._set("ok", f"logged in as {session.user_id}{note}")
        log.info("zerodha login from the web page: ok (%s)", session.user_id)

    def _run(self, srv) -> None:
        from zerodha.auth import wait_for_request_token
        try:
            token = wait_for_request_token(srv, TIMEOUT_S)
            if self.status["state"] != "ok":                         # not already done by a paste
                self._accept(token)
        except Exception as exc:                                      # noqa: BLE001 - shown on the page
            if self.status["state"] != "ok":                          # a paste already logged in: nothing to report
                self._set("error", f"{type(exc).__name__}: {exc}")
                log.warning("zerodha login from the web page failed: %s", exc)
