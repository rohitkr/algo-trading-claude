"""Trading engine command line.

    python3 -m live run                         # PAPER (default): Breeze data, simulated fills, full audit
    python3 -m live replay --start 2026-07-27 --end 2026-09-25 [--parity]
                                                # BACKTEST: replay DuckDB through the live engine, compare with backtest
    python3 -m live status                      # saved positions, halt reason, today's counters
    python3 -m live squareoff                   # emergency: tell the running engine to flatten and stop
    python3 -m live squareoff --now             # ... or flatten directly (engine not running)
    python3 -m live resume                      # clear a halt after you have checked why it happened

Modes come from TRADING_MODE (default PAPER). LIVE order execution is not wired in yet.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import date, datetime, time, timedelta

from .config import EngineConfig


def _date(s: str) -> date:
    return date.fromisoformat(s)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m live", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the engine for today (PAPER)")
    rp = sub.add_parser("replay", help="BACKTEST mode over stored DuckDB data")
    rp.add_argument("--start", type=_date, required=True)
    rp.add_argument("--end", type=_date, required=True)
    rp.add_argument("--parity", action="store_true",
                    help="lift capital/risk limits and slippage so only strategy logic is compared")
    rp.add_argument("--intraday", action="store_true",
                    help="with --parity: keep INTRADAY_ONLY (measures its effect vs the overnight backtest)")
    sub.add_parser("status")
    sq = sub.add_parser("squareoff", help="flatten every engine position and stop")
    sq.add_argument("--now", action="store_true", help="flatten directly instead of signalling a running engine")
    sub.add_parser("resume", help="clear the halt flag")
    args = ap.parse_args(argv)

    cfg = EngineConfig.from_env()
    if args.cmd == "replay":
        return replay(cfg, args.start, args.end, args.parity, args.intraday)
    if cfg.mode == "LIVE":
        print("TRADING_MODE=LIVE: live order execution is not wired into this engine yet. "
              "Use TRADING_MODE=PAPER (see README 'LIVE mode setup').", file=sys.stderr)
        return 2
    if args.cmd == "status":
        return status(cfg)
    if args.cmd == "resume":
        return resume(cfg)
    if args.cmd == "squareoff":
        return squareoff(cfg, args.now)
    return run(cfg)


def run(cfg: EngineConfig) -> int:
    from trading_data.breeze.client import BreezeError, SessionExpiredError
    from trading_data.config import load_settings

    from .app import build, setup_live_logging

    if cfg.kill_file.exists():
        print(f"Kill switch {cfg.kill_file} is present. Remove it once positions are checked, then start again.")
        return 2
    settings = load_settings()
    setup_live_logging(settings, f"live_{cfg.mode.lower()}")
    try:
        built = build(cfg, settings=settings)
    except SessionExpiredError as exc:
        print(f"Breeze: {exc}")
        return 3
    except BreezeError as exc:
        print(f"Breeze: {exc}")
        return 1
    print(f"{cfg.mode} engine: strategies={','.join(cfg.strategies)} position_mode={cfg.position_mode} "
          f"broker={built.engine.broker.name} (real orders: {built.engine.broker.live}). Ctrl-C stops "
          f"(open positions are kept); `python3 -m live squareoff` flattens.")
    try:
        built.engine.run(built.clock.tick)
    finally:
        built.store.close()
    return 0


def replay(cfg: EngineConfig, start: date, end: date, parity: bool, intraday: bool = False) -> int:
    import tempfile
    from pathlib import Path

    from trading_data.config import load_settings
    from trading_data.storage import CandleStore

    from .app import Clock, build, setup_live_logging
    from .replay import backtest_trades, compare

    over = dict(mode="BACKTEST")
    if parity:
        over.update(capital=1e12, max_risk_per_trade=1e12, max_daily_loss=1e12, max_trades_per_day=10_000,
                    max_open_positions=10_000, max_lots_per_trade=10_000, max_qty_per_trade=10_000_000,
                    paper_slippage_points=0.0, zerodte_quote_stops=False, max_data_lag_s=1e9,
                    intraday_only=intraday, max_daily_loss_enabled=False, max_daily_profit_enabled=False)
    cfg = replace(cfg, **over)
    settings = load_settings()
    setup_live_logging(settings, "live_backtest")
    tmp = Path(tempfile.mkdtemp(prefix="live_replay_"))
    cfg = replace(cfg, audit_dir=tmp / "audit", state_dir=tmp, kill_file=tmp / "KILL")
    store = CandleStore(settings.paths.database, read_only=True)
    sim = {"now": datetime.combine(start, time(9, 0))}
    clock = Clock(lambda: sim["now"])
    built = build(cfg, clock=clock, settings=settings, store=store, state_path=tmp / "state.json")
    built.feed.load_spot(start - timedelta(days=90), end)
    eng, cal = built.engine, built.feed.cal
    eng.startup(sim["now"])
    for d in cal.trading_days(start, end):
        open_t, close_t = cal.session(d)
        t = datetime.combine(d, open_t) + timedelta(minutes=1)
        while t <= datetime.combine(d, close_t) + timedelta(minutes=2):
            sim["now"] = t
            clock.tick()
            eng.step(t)
            t += timedelta(minutes=1)
    live = eng.closed
    bt = backtest_trades(built.feed, cfg, start, end)
    report = compare(live, bt)
    print(json.dumps(report["summary"], indent=2, default=str))
    for row in report["rows"]:
        print(row)
    print(f"\nAudit trail: {cfg.audit_dir}\nOpen at end: {list(eng.state.positions)}")
    store.close()
    return 0


def status(cfg: EngineConfig) -> int:
    from .state import EngineState
    st = EngineState.load(cfg.state_dir / f"state_{cfg.mode.lower()}.json")
    print(json.dumps({"mode": cfg.mode, "day": st.day, "halted": st.halted, "trades_today": st.trades_today,
                      "realized_today": st.realized_today, "kill_switch": cfg.kill_file.exists(),
                      "positions": st.positions}, indent=2, default=str))
    return 0


def resume(cfg: EngineConfig) -> int:
    from .state import EngineState
    st = EngineState.load(cfg.state_dir / f"state_{cfg.mode.lower()}.json")
    print(f"Clearing halt: {st.halted!r}")
    st.halted = None
    st.save()
    return 0


def squareoff(cfg: EngineConfig, now: bool) -> int:
    cfg.kill_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.kill_file.write_text(f"square-off requested {datetime.now().isoformat()}\n")
    print(f"Kill switch written: {cfg.kill_file}. A running engine flattens its positions and stops at its next poll.")
    if not now:
        return 0
    from trading_data.breeze.session_store import now_ist
    from trading_data.config import load_settings

    from .app import build, setup_live_logging
    settings = load_settings()
    setup_live_logging(settings, f"live_{cfg.mode.lower()}")
    built = build(cfg, settings=settings)
    built.clock.tick()
    built.engine.startup(now_ist())
    built.engine.square_off_all(now_ist(), "manual squareoff --now")
    built.engine.save()
    print(f"Remaining positions: {list(built.engine.state.positions) or 'none'}")
    built.store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
