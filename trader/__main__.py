"""Command line.

    python3 -m trader serve            # web UI on http://127.0.0.1:8765 + monitor (PAPER unless configured)
    python3 -m trader serve --live     # LIVE: also needs TRADER_MODE=LIVE, ENABLE_LIVE_TRADING=true, KITE_DRY_RUN=0
    python3 -m trader status           # open trades + system status from the database (no broker calls)
    python3 -m trader reconcile        # one broker sync + reconciliation pass, then exit (no web server)
    python3 -m trader resume           # allow new trades again after a halt
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import TraderConfig


def _logging(cfg: TraderConfig) -> None:
    cfg.audit_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.StreamHandler(), logging.FileHandler(cfg.audit_dir / f"trader_{cfg.mode.lower()}.log")):
        h.setFormatter(fmt)
        root.addHandler(h)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m trader", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["serve", "status", "reconcile", "resume"])
    ap.add_argument("--live", action="store_true", help="serve: allow LIVE (with the env switches)")
    ap.add_argument("--port", type=int, help="serve: port (default TRADER_PORT)")
    args = ap.parse_args(argv)
    cfg = TraderConfig.from_env()

    if args.command == "status":
        from datetime import datetime

        from . import lifecycle as L
        from .repository import Repository
        repo = Repository(cfg.db_path, datetime.now)
        for t in repo.trades(L.OPEN_STATUSES):
            print(f"#{t['id']} {t['side']} {t['tradingsymbol']} {t['status']} filled {t['filled_qty']}/{t['quantity']} "
                  f"open {t['open_qty']} SL {t['current_sl']} target {t['target']} err {t['error'] or ''}")
        print(json.dumps(repo.status_values(), indent=1, default=str))
        return 0

    _logging(cfg)
    from .app import AlreadyRunning, build
    try:
        app = build(cfg, cli_live=args.live)
    except AlreadyRunning as exc:
        print(exc)
        return 1
    if args.command == "resume":
        app.service.resume()
        print("new trades allowed again")
        return 0
    if app.stream is not None:
        app.stream.start()                         # KiteTicker: connects in the background, REST until then
    res = app.service.startup()                    # recovery + reconciliation before anything else
    if args.command == "reconcile":
        if app.stream is not None:
            app.stream.stop()
        print(json.dumps(res), json.dumps(app.service.dashboard()["system"], default=str, indent=1))
        return 0

    from .monitor import Monitor
    from .web.server import make_server
    port = args.port or cfg.port
    srv = make_server(app, port)
    mon = Monitor(app.service, cfg.poll_seconds, after_tick=app.hub.after_tick)
    app.hub.start()
    mon.start()
    banner = "LIVE TRADING - REAL ORDERS" if cfg.live else "PAPER (simulated exchange)"
    names = "".join(f" or http://{n}:{port}" for n in cfg.hostnames)
    print(f"\n  {banner}\n  Open http://127.0.0.1:{port}{names}  (Ctrl-C stops; resting SL orders stay at Zerodha)\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        mon.stop()
        app.hub.stop()
        if app.stream is not None:
            app.stream.stop()
        srv.server_close()
        app.repo.set_status_value("process", {"state": "stopped", "stopped_at": app.repo.now(), "mode": cfg.mode})
    return 0


if __name__ == "__main__":
    sys.exit(main())
