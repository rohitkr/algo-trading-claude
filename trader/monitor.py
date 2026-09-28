"""Background monitor: calls TradeService.tick() every TRADER_POLL_SECONDS until stopped."""
from __future__ import annotations

import logging
import threading

log = logging.getLogger("trader.monitor")


class Monitor(threading.Thread):
    def __init__(self, service, interval_s: float):
        super().__init__(name="trader-monitor", daemon=True)
        self.service, self.interval_s = service, interval_s
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.service.tick()
            except Exception:                     # never let the loop die
                log.exception("monitor tick failed")
            self.stop_event.wait(self.interval_s)

    def stop(self) -> None:
        self.stop_event.set()
