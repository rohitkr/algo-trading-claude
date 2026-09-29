"""Background monitor: calls TradeService.tick() (then an optional after_tick hook) every TRADER_POLL_SECONDS."""
from __future__ import annotations

import logging
import threading

log = logging.getLogger("trader.monitor")


class Monitor(threading.Thread):
    def __init__(self, service, interval_s: float, after_tick=None):
        super().__init__(name="trader-monitor", daemon=True)
        self.service, self.interval_s, self.after_tick = service, interval_s, after_tick
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.service.tick()
            except Exception:                     # never let the loop die
                log.exception("monitor tick failed")
            if self.after_tick is not None:       # e.g. TickHub: push state changes to the pages
                try:
                    self.after_tick()
                except Exception:
                    log.exception("after-tick hook failed")
            self.stop_event.wait(self.interval_s)

    def stop(self) -> None:
        self.stop_event.set()
