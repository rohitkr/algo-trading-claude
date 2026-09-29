"""Trading engine command line. Every command works on all configured instances, or on one with --instance.

    python3 -m live run [--instance ID ...]      # PAPER (default): Breeze data, simulated fills, full audit
    python3 -m live replay --start 2026-07-27 --end 2026-09-25 [--parity] [--instance ID ...]
                                                 # BACKTEST: replay DuckDB through the live engine, compare with backtest
    python3 -m live status [--instance ID]       # saved positions, halt reason, today's counters
    python3 -m live squareoff [--instance ID]    # emergency: a running engine flattens (that instance / everything)
    python3 -m live squareoff --now [--instance ID]   # ... or flatten directly (engine not running)
    python3 -m live resume [--instance ID]       # clear a halt (that instance's / the account-wide one)

Instances come from INSTANCES=... in .env (see live/config.py). Modes come from TRADING_MODE (default PAPER).
LIVE order execution is not wired in yet.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import date, datetime, time, timedelta

from .config import EngineConfig, load_instances


def _date(s: str) -> date:
    return date.fromisoformat(s)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m live", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    parsers = {}
    parsers["run"] = sub.add_parser("run", help="run the engine for today (PAPER)")
    rp = parsers["replay"] = sub.add_parser("replay", help="BACKTEST mode over stored DuckDB data")
    rp.add_argument("--start", type=_date, required=True)
    rp.add_argument("--end", type=_date, required=True)
    rp.add_argument("--parity", action="store_true",
                    help="lift capital/risk limits and slippage so only strategy logic is compared "
                         "(positional instances hold to expiry, like the backtest)")
    rp.add_argument("--intraday", action="store_true",
                    help="with --parity: make positional instances intraday-only (measures that vs the backtest)")
    parsers["status"] = sub.add_parser("status")
    sq = parsers["squareoff"] = sub.add_parser("squareoff", help="flatten positions and stop")
    sq.add_argument("--now", action="store_true", help="flatten directly instead of signalling a running engine")
    parsers["resume"] = sub.add_parser("resume", help="clear a halt flag")
    for p in parsers.values():
        p.add_argument("--instance", action="append", help="only this instance (repeatable)")
    args = ap.parse_args(argv)

    cfgs, warnings = load_instances(only=args.instance)
    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr)
    if args.cmd == "replay":
        return replay(cfgs, args.start, args.end, args.parity, args.intraday)
    if cfgs[0].mode == "LIVE":
        print("TRADING_MODE=LIVE: live order execution is not wired into this engine yet. "
              "Use TRADING_MODE=PAPER (see README 'LIVE mode setup').", file=sys.stderr)
        return 2
    if args.cmd == "status":
        return status(cfgs)
    if args.cmd == "resume":
        return resume(cfgs, bool(args.instance))
    if args.cmd == "squareoff":
        return squareoff(cfgs, args.now, bool(args.instance))
    return run(cfgs, warnings)


def run(cfgs: list[EngineConfig], warnings: list[str]) -> int:
    from trading_data.breeze.client import BreezeError, SessionExpiredError
    from trading_data.config import load_settings
    from zerodha.auth import LoginRequired

    from .app import build, setup_live_logging

    g = cfgs[0]
    if g.kill_file.exists():
        print(f"Kill switch {g.kill_file} is present. Remove it once positions are checked, then start again.")
        return 2
    settings = load_settings()
    setup_live_logging(settings, f"live_{g.mode.lower()}")
    try:
        built = build(cfgs, settings=settings)
    except SessionExpiredError as exc:
        print(f"Breeze: {exc}")
        return 3
    except BreezeError as exc:
        print(f"Breeze: {exc}")
        return 1
    except LoginRequired as exc:
        print(f"Kite: {exc}")
        return 3
    for iid, inst in built.instances.items():
        print(f"{g.mode} {iid}: {json.dumps(inst.summary())}")
    print(f"broker={built.account.broker.name} (real orders: {built.account.broker.live}). Ctrl-C stops "
          f"(open positions are kept); `python3 -m live squareoff [--instance ID]` flattens.")
    try:
        built.account.run(built.clock.tick, warnings=warnings)
    finally:
        built.store.close()
    return 0


def replay(cfgs: list[EngineConfig], start: date, end: date, parity: bool, intraday: bool = False) -> int:
    import tempfile
    from pathlib import Path

    from trading_data.config import load_settings
    from trading_data.storage import CandleStore

    from .app import Clock, build, setup_live_logging
    from .replay import backtest_trades, compare

    tmp = Path(tempfile.mkdtemp(prefix="live_replay_"))
    out = []
    for cfg in cfgs:
        over = dict(mode="BACKTEST", audit_dir=tmp / "audit", state_dir=tmp, kill_file=tmp / "KILL")
        if parity:
            over.update(capital=1e12, max_risk_per_trade=1e12, max_trades_per_day=10_000,
                        max_open_positions=10_000, max_lots_per_trade=10_000, max_qty_per_trade=10_000_000,
                        paper_slippage_points=0.0, zerodte_quote_stops=False, max_data_lag_s=1e9,
                        max_daily_loss_enabled=False, max_daily_profit_enabled=False, shared_contracts=True,
                        halt_on_unknown_positions=False)
            if cfg.strategy == "positional":
                over["intraday_only"] = intraday
        out.append(replace(cfg, **over))
    cfgs = out
    settings = load_settings()
    setup_live_logging(settings, "live_backtest")
    store = CandleStore(settings.paths.database, read_only=True)
    sim = {"now": datetime.combine(start, time(9, 0))}
    clock = Clock(lambda: sim["now"])
    built = build(cfgs, clock=clock, settings=settings, store=store, state_root=tmp)
    built.feed.load_spot(start - timedelta(days=90), end)
    acct, cal = built.account, built.feed.cal
    acct.startup(sim["now"])
    for d in cal.trading_days(start, end):
        open_t, close_t = cal.session(d)
        t = datetime.combine(d, open_t) + timedelta(minutes=1)
        while t <= datetime.combine(d, close_t) + timedelta(minutes=2):
            sim["now"] = t
            clock.tick()
            acct.step(t)
            t += timedelta(minutes=1)
    for cfg in cfgs:
        inst = built.instances[cfg.instance_id]
        report = compare(inst.closed, backtest_trades(built.feed, cfg, start, end))
        print(f"\n=== {cfg.instance_id}: {json.dumps(inst.summary())}")
        print(json.dumps(report["summary"], indent=2, default=str))
        for row in report["rows"]:
            print(row)
        print(f"Open at end: {list(inst.state.positions)}")
    print(f"\nAudit trail: {tmp / 'audit'}")
    store.close()
    return 0


def _states(cfgs: list[EngineConfig]):
    from .app import state_dir
    from .state import EngineState
    root = state_dir(cfgs[0])
    return root, {c.instance_id: EngineState.load(root / f"{c.instance_id}.json") for c in cfgs}, \
        EngineState.load(root / "_account.json")


def status(cfgs: list[EngineConfig]) -> int:
    root, states, acct = _states(cfgs)
    g = cfgs[0]
    print(json.dumps({"mode": g.mode, "state_dir": str(root), "kill_switch": g.kill_file.exists(),
                      "account": {"halted": acct.halted, "unmanaged": acct.unmanaged},
                      "instances": {iid: {"day": st.day, "halted": st.halted, "session_status": st.session_status,
                                          "trades_today": st.trades_today, "realized_today": st.realized_today,
                                          "kill_switch": g.kill_file.with_name(f"{g.kill_file.name}_{iid}").exists(),
                                          "blocked": st.blocked, "unmanaged": st.unmanaged,
                                          "positions": st.positions} for iid, st in states.items()}},
                     indent=2, default=str))
    return 0


def resume(cfgs: list[EngineConfig], one: bool) -> int:
    _, states, acct = _states(cfgs)
    for iid, st in states.items():
        print(f"{iid}: clearing halt {st.halted!r}")
        st.halted = None
        st.save()
    if not one:
        print(f"account: clearing halt {acct.halted!r}")
        acct.halted = None
        acct.save()
    return 0


def squareoff(cfgs: list[EngineConfig], now: bool, one: bool) -> int:
    g = cfgs[0]
    files = [g.kill_file.with_name(f"{g.kill_file.name}_{c.instance_id}") for c in cfgs] if one else [g.kill_file]
    for f in files:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"square-off requested {datetime.now().isoformat()}\n")
        print(f"Kill switch written: {f}")
    print("A running engine flattens " + ("those instances (the others keep running)" if one else
                                         "every instance and stops") + " at its next poll.")
    if not now:
        return 0
    from trading_data.breeze.session_store import now_ist
    from trading_data.config import load_settings

    from .app import build, setup_live_logging
    settings = load_settings()
    setup_live_logging(settings, f"live_{g.mode.lower()}")
    built = build(cfgs, settings=settings)
    built.clock.tick()
    built.account.startup(now_ist())
    for inst in built.instances.values():
        inst.square_off_all(now_ist(), "manual squareoff --now")
    built.account.save()
    print(f"Remaining positions: { {i: list(x.state.positions) for i, x in built.instances.items()} }")
    built.store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
