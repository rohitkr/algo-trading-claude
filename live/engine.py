"""The trading engine: market data -> strategies -> risk -> execution, with state, audit and reconciliation.

Startup: the saved state is cross-checked against the broker's actual positions before anything else
(`startup`); the broker's state wins (see `reconcile`).

One `step(now)` per poll:
  1. kill switch?          -> square off everything the engine holds, stop
  2. new trading day?      -> reset daily counters and caps, re-base day P&L
  3. market hours?         -> otherwise idle
  4. strategies.on_poll    -> signals; each ENTRY goes through the RiskManager (sized, checked,
                              audited) before the ExecutionBroker; EXITs always go out
  5. intraday              -> INTRADAY_ONLY: everything still open is squared off at FORCE_EXIT_TIME
  6. failed exits          -> retried every poll; after ENGINE_EXIT_RETRY_LIMIT: CRITICAL + halt
  7. mark to market        -> daily P&L (realised + unrealised) drives BOTH caps:
                              max loss halts new entries; max profit squares off and ends the day
  8. data health           -> stale feed blocks entries; optional square-off on long outages
  9. reconciliation        -> broker positions are the truth (MANUAL_EXIT, adopt, or stop managing + halt)
 10. save state
The engine never imports Breeze or Kite: it only sees the interfaces in live/interfaces.py.
"""
from __future__ import annotations

import logging
import time as _time
from datetime import date, datetime, time, timedelta
from typing import Callable

from strategy_signals import Action, LegRole, OptionLeg, OrderIntent, Right, Side

from .audit import AuditLog
from .config import EngineConfig
from .interfaces import (ExecutionBroker, ExecutionResult, MarketDataProvider, Signal, StrategyContext,
                         contract_from_dict, contract_to_dict, contract_key)
from .risk import RiskContext, RiskManager, estimate_risk_per_unit
from .selection import ContractSelector
from .state import EngineState

log = logging.getLogger("live.engine")
MINUTE = timedelta(minutes=1)
MANUAL_EXIT = "MANUAL_EXIT"
MAX_PROFIT_REACHED = "MAX_PROFIT_REACHED"
MAX_LOSS_REACHED = "MAX_LOSS_REACHED"


def key_to_json(k: tuple) -> list:
    return [k[0], k[1].isoformat() if isinstance(k[1], date) else k[1], k[2], k[3]]


def key_from_json(k: list) -> tuple:
    return (k[0], date.fromisoformat(k[1]), float(k[2]), k[3])


def key_label(k: tuple) -> str:
    return f"{k[0]} {k[1]} {k[2]:g} {k[3]}" if len(k) == 4 else " ".join(map(str, k))


def root_of(pid: str) -> str:
    return pid.rstrip("R")


class TradingEngine:
    def __init__(self, cfg: EngineConfig, market: MarketDataProvider, broker: ExecutionBroker,
                 selector: ContractSelector, strategies: list, risk: RiskManager, state: EngineState,
                 audit: AuditLog):
        self.cfg, self.market, self.broker, self.selector = cfg, market, broker, selector
        self.strategies, self.risk, self.state, self.audit = strategies, risk, state, audit
        self.by_name = {s.name: s for s in strategies}
        for s in strategies:
            s.set_state(state.strategies.get(s.name, {}))
        self.stopped = False
        self.save_hooks: list[Callable[[], None]] = []
        self._last_reconcile: datetime | None = None
        self._last_outage_alert: datetime | None = None
        self._data_errors_seen = 0
        self._mismatch_seen: dict[str, int] = {}
        self.last_mtm: dict = {}
        self.closed: list[dict] = []                   # positions closed by this process (replay comparison)

    # =====================================================================================
    # main loop
    # =====================================================================================
    def run(self, now_fn: Callable[[], datetime], sleep: Callable[[float], None] = _time.sleep) -> None:
        now = now_fn()
        self.audit.write("engine_start", now, mode=self.cfg.mode, broker=self.broker.name,
                         live_orders=self.broker.live, position_mode=self.cfg.position_mode,
                         hedge_width=self.cfg.hedge_width if self.cfg.hedged else None,
                         strategies=[s.name for s in self.strategies], config=self._config_summary())
        self.startup(now)
        try:
            while not self.stopped:
                now = now_fn()
                if not self.selector.is_trading_day(now.date()) or now.time() > self.cfg.stop_time:
                    self.audit.write("engine_stop", now, reason="not a trading day" if not
                                     self.selector.is_trading_day(now.date()) else f"after {self.cfg.stop_time:%H:%M}",
                                     open_positions=list(self.state.positions))
                    break
                self.step(now)
                sleep(self.cfg.poll_seconds)
        except KeyboardInterrupt:
            self.audit.write("engine_stop", now_fn(), level=logging.WARNING, reason="Ctrl-C (positions NOT closed)",
                             open_positions=list(self.state.positions))
        finally:
            self.save()

    def startup(self, now: datetime) -> dict:
        """Cross-check the saved state against the broker before trading; the broker's positions win."""
        res = self.reconcile(now, startup=True)
        self.audit.write("startup_reconcile", now, level=logging.INFO if res.get("ok") else logging.WARNING,
                         positions=list(self.state.positions), unmanaged=list(self.state.unmanaged),
                         halted=self.state.halted, **{k: v for k, v in res.items() if k not in ("ok", "unmanaged")})
        self.save()
        return res

    def step(self, now: datetime) -> None:
        if self.stopped:
            return
        if self.cfg.kill_file.exists():
            self.square_off_all(now, f"kill switch {self.cfg.kill_file}")
            self.stopped = True
            self.save()
            return
        today = now.date()
        if not self.selector.is_trading_day(today):
            return
        self._roll_day(today, now)
        open_t, close_t = self.selector.cal.session(today)
        if not (datetime.combine(today, open_t) + MINUTE <= now <= datetime.combine(today, close_t) + 2 * MINUTE):
            return

        ctx = StrategyContext(now, self.market, self.selector)
        for strat in self.strategies:
            try:
                signals = strat.on_poll(ctx)
            except Exception as exc:
                self.audit.write("strategy_error", now, level=logging.ERROR, strategy=strat.name,
                                 error=f"{type(exc).__name__}: {exc}")
                log.exception("strategy %s failed", strat.name)
                signals = []
            self._drain_notes(strat, now)
            for sig in signals:
                if sig.action is Action.ENTRY:
                    self._entry(sig, now, strat)
                else:
                    self._exit_position(sig.position_id, now, sig.reason, sig)
            self._drain_notes(strat, now)
            self.state.strategies[strat.name] = strat.get_state()

        self._intraday_exits(now)
        self._retry_pending_exits(now)
        self._mark_to_market(now)
        self._data_health(now)
        if self._last_reconcile is None or (now - self._last_reconcile).total_seconds() >= self.cfg.reconcile_seconds:
            self.reconcile(now)
        self.save()

    def save(self) -> None:
        for s in self.strategies:
            self.state.strategies[s.name] = s.get_state()
        self.state.save()
        for hook in self.save_hooks:
            hook()

    # =====================================================================================
    # entries
    # =====================================================================================
    def _entry(self, sig: Signal, now: datetime, strat) -> None:
        cfg, lot = self.cfg, self.selector.lot_size
        main = sig.contract
        wing = self.selector.wing(main, cfg.hedge_width) if cfg.hedged else None
        main_ref = sig.ref_price if sig.ref_price is not None else self.market.option_price(main, now, fresh=True)
        wing_ref = self.market.option_price(wing, now, fresh=True) if wing else None
        sig.ref_price = main_ref
        risk_unit, hedge_info = estimate_risk_per_unit(sig, cfg, wing_ref)
        if wing:
            hedge_info.update(hedge_contract=wing.label, hedge_strike=wing.strike, hedge_ref_price=wing_ref,
                              hedge_width=cfg.hedge_width)
        self.audit.write("signal", sig.ts, strategy=sig.strategy, action="ENTRY", position_id=sig.position_id,
                         underlying_price=sig.spot, contract=main.label, strike=main.strike, expiry=main.expiry,
                         right=main.right, side=sig.side, ref_price=main_ref, stop=sig.stop, reason=sig.reason,
                         is_reentry=sig.is_reentry, position_mode=cfg.position_mode, hedge=hedge_info or None,
                         meta=sig.meta)

        margin_per_lot = available = None
        try:
            margin_per_lot = self.broker.required_margin(self._intent(sig, 1, wing, "margin-probe"))
            available = self.broker.available_margin()
        except Exception as exc:
            self.audit.write("margin_error", now, level=logging.WARNING, position_id=sig.position_id,
                             error=f"{type(exc).__name__}: {exc}")
        root = root_of(sig.position_id)
        held = {tuple(l["key"]) for p in self.state.positions.values() for l in p["legs"]} | \
               {tuple(u["key"]) for u in self.state.unmanaged.values() if u.get("key")}
        rc = RiskContext(
            now=now, session=self.selector.cal.session(now.date()) if self.selector.is_trading_day(now.date()) else None,
            halted=self.state.halted, data_lag_s=self._data_lag(now), api_budget=self.market.api_budget_remaining(),
            open_positions=len(self.state.positions), trades_today=self.state.trades_today,
            daily_pnl=self.daily_pnl(), reentries_for_root=self.state.counters.get("reentries", {}).get(root, 0),
            signal_blocked=self.state.blocked.get(root),
            already_sent=self.state.was_sent(f"{sig.position_id}:entry"),
            contract_already_open=tuple(key_to_json(contract_key(main))) in held,
            lot_size=lot, risk_per_unit=risk_unit, margin_per_lot=margin_per_lot, available_margin=available,
            hedge=hedge_info)
        decision = self.risk.evaluate_entry(sig, rc)
        self.audit.write("risk_check", now, position_id=sig.position_id, ok=decision.ok, lots=decision.lots,
                         checks=[{"check": c.name, "passed": c.passed, "detail": c.detail} for c in decision.checks],
                         metrics=decision.metrics, level=logging.INFO if decision.ok else logging.WARNING)
        if not decision.ok:
            strat.on_entry_result(sig, False)
            return

        intent = self._intent(sig, decision.lots, wing, "entry")
        self.state.mark_sent(intent.intent_id)
        self.state.pending_entries[sig.position_id] = {       # lets a restart adopt it if we crash mid-order
            "pid": sig.position_id, "strategy": sig.strategy, "ts": sig.ts.isoformat(), "wall": now.isoformat(),
            "spot": sig.spot, "reason": sig.reason, "stop": sig.stop, "is_reentry": sig.is_reentry,
            "ref_price": main_ref, "wing_ref": wing_ref,
            "legs": [{"key": key_to_json(l.key), "side": l.side.value, "role": l.role.value, "qty": l.quantity,
                      "contract": contract_to_dict(self._contract(l))} for l in intent.legs]}
        self.save()                                    # duplicate guard + pending entry must survive a crash
        res = self.broker.execute(intent)
        self._audit_order(now, intent, res)
        if not res.ok:
            strat.on_entry_result(sig, False)
            if res.uncertain:                          # keep the pending entry: reconciliation decides
                self._halt(now, f"broker error on {intent.intent_id}: {res.message}")
                self.reconcile(now)
            else:
                self.state.pending_entries.pop(sig.position_id, None)
            return
        self.state.pending_entries.pop(sig.position_id, None)

        legs = []
        for l in intent.legs:
            f = res.fill_for(l.key)
            legs.append(self._leg(l.key, l.side.value, l.role.value, f.quantity if f else l.quantity,
                                  self._contract(l), f.average_price if f else None,
                                  f.tradingsymbol if f else "", f.order_ids if f else []))
        pos = self._position(sig.position_id, sig.strategy, sig.is_reentry, sig.ts, now, sig.spot, sig.reason,
                             sig.stop, legs, res.margin_required)
        self._open(pos, now, sig.is_reentry, signal_ref_price=main_ref)
        strat.on_entry_result(sig, True)

    def _open(self, pos: dict, now: datetime, is_reentry: bool, **extra) -> None:
        root = pos["root"]
        self.state.positions[pos["pid"]] = pos
        self.state.trades_today += 1
        if is_reentry:
            r = self.state.counters.setdefault("reentries", {})
            r[root] = r.get(root, 0) + 1
        keys = self.state.counters.setdefault("traded_keys", [])
        for l in pos["legs"]:
            if l["key"] not in keys:
                keys.append(l["key"])
        main = next(l for l in pos["legs"] if l["role"] == "MAIN")
        c = main["contract"]
        self.audit.write("position_opened", now, position_id=pos["pid"], strategy=pos["strategy"],
                         underlying_price=pos["entry_spot"], contract=f"{c['underlying']} {c['expiry']} "
                         f"{c['strike']:g} {c['right']}", strike=c["strike"], expiry=c["expiry"], quantity=main["qty"],
                         entry_price=main["entry_price"], stop=pos["stop"], hedge=pos["hedge"], margin=pos["margin"],
                         is_reentry=is_reentry, order_ids=[o for l in pos["legs"] for o in l["order_ids"]], **extra)
        self._last_reconcile = None                   # reconcile right after every order

    def _leg(self, key, side, role, qty, contract, price, symbol="", order_ids=None) -> dict:
        return {"key": key_to_json(key), "contract": contract_to_dict(contract), "role": role, "side": side,
                "qty": qty, "symbol": symbol, "entry_price": price, "day_ref_price": price, "last_price": price,
                "closed_qty": 0, "exit_value": 0.0, "order_ids": order_ids or []}

    def _position(self, pid, strategy, is_reentry, ts, now, spot, reason, stop, legs, margin) -> dict:
        main = next(x for x in legs if x["role"] == "MAIN")
        wing = next((x for x in legs if x["role"] == "HEDGE"), None)
        hedge = None
        if wing and main["entry_price"] is not None and wing["entry_price"] is not None:
            credit = main["entry_price"] - wing["entry_price"]
            width = abs(wing["contract"]["strike"] - main["contract"]["strike"])
            hedge = {"strike": wing["contract"]["strike"], "entry_price": wing["entry_price"],
                     "net_credit": round(credit, 2), "max_loss": round((width - credit) * main["qty"], 2),
                     "width": width}
        return {"pid": pid, "root": root_of(pid), "strategy": strategy, "status": "open", "is_reentry": is_reentry,
                "entry_ts": ts.isoformat() if isinstance(ts, datetime) else ts, "entry_wall": now.isoformat(),
                "entry_spot": spot, "reason": reason, "stop": stop, "legs": legs, "hedge": hedge, "margin": margin,
                "exit_attempts": 0, "entry_credit": main["entry_price"]}

    def _contract(self, l: OptionLeg):
        return contract_from_dict({"underlying": l.underlying, "exchange": self.selector.profile.exchange,
                                   "expiry": l.expiry.isoformat(), "strike": l.strike, "right": l.right.value})

    def _intent(self, sig: Signal, lots: int, wing, tag: str) -> OrderIntent:
        qty = lots * self.selector.lot_size
        c = sig.contract
        legs = [OptionLeg(c.underlying, c.expiry, c.strike, Right(c.right), Side(sig.side), qty, LegRole.MAIN,
                          sig.ref_price)]
        if wing is not None:
            legs.append(OptionLeg(wing.underlying, wing.expiry, wing.strike, Right(wing.right), Side.BUY, qty,
                                  LegRole.HEDGE))
        return OrderIntent(f"{sig.position_id}:{tag}", sig.position_id, Action.ENTRY, tuple(legs), sig.ts,
                           sig.strategy, sig.reason)

    # =====================================================================================
    # exits
    # =====================================================================================
    def _exit_position(self, pid: str, now: datetime, reason: str, sig: Signal | None = None,
                       external: bool = False) -> bool:
        """Close one position. `external` = decided by the engine (force exit, square-off, cap), not by its
        strategy: the strategy is told to stop managing it (no stop, no re-entry) before the order goes out."""
        pos = self.state.positions.get(pid)
        if sig is not None:
            self.audit.write("signal", sig.ts, strategy=sig.strategy, action="EXIT", position_id=pid,
                             underlying_price=sig.spot, contract=sig.contract.label, reason=reason,
                             ref_price=sig.ref_price, stop=sig.stop, is_reentry=sig.is_reentry)
        if pos is None:
            self.audit.write("exit_ignored", now, position_id=pid,
                             reason="no open position (entry not executed, or already closed/unmanaged)")
            return True
        if external:
            self._stop_managing(pos, reason)
        remaining = [l for l in pos["legs"] if l["qty"] - l["closed_qty"] > 0]
        legs = tuple(OptionLeg(l["contract"]["underlying"], date.fromisoformat(l["contract"]["expiry"]),
                               l["contract"]["strike"], Right(l["contract"]["right"]), Side(l["side"]),
                               l["qty"] - l["closed_qty"], LegRole(l["role"])) for l in remaining)
        attempt = pos["exit_attempts"]
        intent = OrderIntent(f"{pid}:exit{attempt or ''}", pid, Action.EXIT, legs, sig.ts if sig else now,
                             pos["strategy"], reason)
        self.state.mark_sent(intent.intent_id)
        res = self.broker.execute(intent)
        self._audit_order(now, intent, res)
        for f in res.fills:
            for l in remaining:
                if tuple(key_from_json(l["key"])) == f.key:
                    l["closed_qty"] += f.quantity
                    l["exit_value"] += f.average_price * f.quantity
        if res.ok and all(l["qty"] == l["closed_qty"] for l in pos["legs"]):
            self._close(pos, now, reason, sig)
            return True
        pos.update(status="exit_pending", exit_attempts=attempt + 1, exit_reason=reason, last_error=res.message)
        self.audit.write("exit_failed", now, level=logging.CRITICAL, position_id=pid, attempt=attempt + 1,
                         message=res.message, uncertain=res.uncertain,
                         action="retrying every poll" if attempt + 1 < self.cfg.exit_retry_limit else
                         "RETRY LIMIT REACHED - check Zerodha positions manually")
        if attempt + 1 >= self.cfg.exit_retry_limit:
            self._halt(now, f"exit of {pid} failed {attempt + 1} times")
        if res.uncertain:
            self.reconcile(now)
        return False

    def _retry_pending_exits(self, now: datetime) -> None:
        for pid, pos in list(self.state.positions.items()):
            if pos["status"] == "exit_pending" and pos["exit_attempts"] < self.cfg.exit_retry_limit * 3:
                self._exit_position(pid, now, pos.get("exit_reason", "retry"))

    def _close(self, pos: dict, now: datetime, reason: str, sig: Signal | None, status: str = "CLOSED",
               estimated: bool = False) -> dict:
        gross = day = 0.0
        for l in pos["legs"]:
            exit_px = l["exit_value"] / l["closed_qty"] if l["closed_qty"] else None
            l["exit_price"] = round(exit_px, 4) if exit_px is not None else None
            sign = 1 if l["side"] == "SELL" else -1
            if exit_px is not None and l["entry_price"] is not None:
                gross += sign * (l["entry_price"] - exit_px) * l["qty"]
                day += sign * ((l["day_ref_price"] or l["entry_price"]) - exit_px) * l["qty"]
        self.state.realized_today += day
        self.state.positions.pop(pos["pid"], None)
        main = next(l for l in pos["legs"] if l["role"] == "MAIN")
        wing = next((l for l in pos["legs"] if l["role"] == "HEDGE"), None)
        rec = dict(position_id=pos["pid"], strategy=pos["strategy"], status=status,
                   underlying=main["contract"]["underlying"], expiry=main["contract"]["expiry"],
                   right=main["contract"]["right"], strike=main["contract"]["strike"], side=main["side"],
                   quantity=main["qty"], entry_ts=pos["entry_ts"], entry_spot=pos["entry_spot"],
                   entry_price=main["entry_price"], stop=pos["stop"], exit_ts=(sig.ts if sig else now).isoformat(),
                   exit_spot=sig.spot if sig else None, exit_price=main.get("exit_price"), exit_reason=reason,
                   gross_pnl=round(gross, 2), pnl_today=round(day, 2), pnl_estimated=estimated,
                   is_reentry=pos["is_reentry"],
                   hedge_strike=wing["contract"]["strike"] if wing else None,
                   hedge_entry_price=wing["entry_price"] if wing else None,
                   hedge_exit_price=wing.get("exit_price") if wing else None,
                   net_credit=(pos["hedge"] or {}).get("net_credit"), max_loss=(pos["hedge"] or {}).get("max_loss"),
                   margin=pos["margin"], mode=self.cfg.mode, position_mode=self.cfg.position_mode,
                   order_ids=[o for l in pos["legs"] for o in l["order_ids"]])
        self.audit.write("position_closed", now, pnl=round(gross, 2),
                         level=logging.WARNING if status != "CLOSED" else logging.INFO, **rec)
        self.audit.trade(rec)
        self.closed.append(rec)
        self._last_reconcile = None
        return rec

    def _intraday_exits(self, now: datetime) -> None:
        """INTRADAY_ONLY: nothing survives FORCE_EXIT_TIME, and nothing is carried into a new day."""
        if not self.cfg.intraday_only:
            return
        for pid, pos in list(self.state.positions.items()):
            if pos["status"] == "exit_pending":
                continue
            entry_day = datetime.fromisoformat(pos["entry_ts"]).date()
            if entry_day < now.date():
                self._exit_position(pid, now, f"intraday: carried over from {entry_day} (should not happen)",
                                    external=True)
            elif now.time() >= self.cfg.force_exit_time:
                self._exit_position(pid, now, f"intraday force exit {self.cfg.force_exit_time:%H:%M}", external=True)

    def square_off_all(self, now: datetime, reason: str, halt_reason: str | None = None) -> None:
        """Emergency square-off: close every position the engine holds (retrying failures), then halt."""
        self.audit.write("square_off", now, level=logging.CRITICAL, reason=reason,
                         positions=list(self.state.positions))
        for pid in list(self.state.positions):
            for _ in range(max(1, self.cfg.exit_retry_limit)):
                if self._exit_position(pid, now, f"square-off: {reason}", external=True):
                    break
        self._halt(now, halt_reason or f"square-off: {reason}")
        self.reconcile(now)

    def _stop_managing(self, pos: dict, reason: str) -> None:
        strat = self.by_name.get(pos["strategy"])
        if strat is not None and hasattr(strat, "on_external_close"):
            strat.on_external_close(pos["pid"], reason)

    # =====================================================================================
    # P&L: one measure for both daily caps
    # =====================================================================================
    def _mark_to_market(self, now: datetime) -> None:
        unreal = 0.0
        for pos in self.state.positions.values():
            for l in pos["legs"]:
                open_qty = l["qty"] - l["closed_qty"]
                if not open_qty or l["entry_price"] is None:
                    continue
                px = self.market.option_price(contract_from_dict(l["contract"]), now)
                if px is None:
                    px = l["last_price"]
                l["last_price"] = px
                if l["day_ref_price"] is None:
                    l["day_ref_price"] = px
                sign = 1 if l["side"] == "SELL" else -1
                unreal += sign * (l["day_ref_price"] - px) * open_qty
        self.last_mtm = {"ts": now.isoformat(), "unrealized_today": round(unreal, 2),
                         "realized_today": round(self.state.realized_today, 2)}
        self._check_daily_limits(now)

    def daily_pnl(self) -> float:
        """Realised today + unrealised mark-to-market of open positions (vs today's reference price)."""
        return self.state.realized_today + self.last_mtm.get("unrealized_today", 0.0)

    def _check_daily_limits(self, now: datetime) -> None:
        c, pnl = self.cfg, self.daily_pnl()
        if c.max_daily_profit_enabled and pnl >= c.max_daily_profit and self.state.session_status != MAX_PROFIT_REACHED:
            self.state.session_status = MAX_PROFIT_REACHED
            self.audit.write("max_daily_profit", now, level=logging.WARNING, pnl=round(pnl, 2),
                             cap=c.max_daily_profit, action="square off all, no more trades today")
            self.square_off_all(now, f"{MAX_PROFIT_REACHED} ({pnl:,.0f} >= {c.max_daily_profit:,.0f})",
                                halt_reason=MAX_PROFIT_REACHED)
        elif c.max_daily_loss_enabled and pnl <= -c.max_daily_loss and self.state.session_status is None:
            self.state.session_status = MAX_LOSS_REACHED
            self._halt(now, f"max daily loss reached ({pnl:,.0f})")

    # =====================================================================================
    # data health
    # =====================================================================================
    def _data_lag(self, now: datetime) -> float | None:
        bars = self.market.spot_bars(now.date(), now)
        if bars is None or bars.empty:
            open_t = self.selector.cal.session(now.date())[0]
            return max(0.0, (now - datetime.combine(now.date(), open_t)).total_seconds() - 60)
        return (now - (bars.index.max().to_pydatetime() + MINUTE)).total_seconds()

    def _data_health(self, now: datetime) -> None:
        errors = getattr(self.market, "errors", 0)
        if errors > self._data_errors_seen:
            self.audit.write("data_error", now, level=logging.WARNING, errors=errors - self._data_errors_seen,
                             last_error=getattr(self.market, "last_error", None))
            self._data_errors_seen = errors
        lag = self._data_lag(now)
        close_t = self.selector.cal.session(now.date())[1]
        if lag is None or lag < self.cfg.data_outage_s or now.time() > close_t:
            return
        if self._last_outage_alert and (now - self._last_outage_alert).total_seconds() < 300:
            return
        self._last_outage_alert = now
        self.audit.write("data_outage", now, level=logging.CRITICAL, lag_seconds=round(lag),
                         open_positions=list(self.state.positions),
                         action="square off" if self.cfg.square_off_on_data_outage else "alert only (stops blind)")
        if self.cfg.square_off_on_data_outage and self.state.positions:
            self.square_off_all(now, f"market data down {lag:.0f}s")

    # =====================================================================================
    # reconciliation: the broker's positions are the truth
    # =====================================================================================
    def expected_positions(self) -> dict[tuple, int]:
        out: dict[tuple, int] = {}
        for pos in self.state.positions.values():
            for k, q in self._pos_expected(pos).items():
                out[k] = out.get(k, 0) + q
        return out

    @staticmethod
    def _pos_expected(pos: dict) -> dict[tuple, int]:
        out: dict[tuple, int] = {}
        for l in pos["legs"]:
            k = key_from_json(l["key"])
            sign = -1 if l["side"] == "SELL" else 1
            out[k] = out.get(k, 0) + sign * (l["qty"] - l["closed_qty"])
        return out

    def _confirmed(self, tag: str, startup: bool) -> bool:
        """A mismatch is acted on only after ENGINE_RECONCILE_CONFIRMATIONS consecutive checks (broker
        position reports can lag a fill); at startup there is no race, so immediately."""
        self._mismatch_seen[tag] = self._mismatch_seen.get(tag, 0) + 1
        return startup or self._mismatch_seen[tag] >= self.cfg.reconcile_confirmations

    def reconcile(self, now: datetime, startup: bool = False) -> dict:
        """Compare the engine's book with the broker's net positions and adopt the broker's state.

        Per engine position (all its legs):
          * broker flat on every leg        -> MANUAL_EXIT: closed outside the engine; no order, no re-entry
          * same side, consistently smaller -> adopt the broker quantity and keep managing
          * anything else (more, flipped, one leg only) -> stop managing it (no stop/re-entry/orders),
                                               keep it read-only as UNMANAGED, halt new entries
        Pending entries (sent, crash before the result was recorded):
          * broker holds exactly those legs -> adopt as the engine's position with the saved signal data
          * broker flat                     -> the entry never filled; drop it
          * anything else                   -> UNMANAGED + halt
        Broker positions nobody claims (UNDERLYING options) -> UNMANAGED read-only, alert, halt (config).
        Open orders found at startup -> halt: the engine cannot know what they belong to.
        Every mismatch must repeat ENGINE_RECONCILE_CONFIRMATIONS times before it is acted on (not at startup).
        """
        self._last_reconcile = now
        try:
            actual = {k: q for k, q in self.broker.positions().items()}
            open_orders = self.broker.open_orders()
        except Exception as exc:
            self.audit.write("reconcile_error", now, level=logging.ERROR, error=f"{type(exc).__name__}: {exc}")
            return {"ok": False, "error": str(exc)}
        claimed: set = set()
        events: list[dict] = []
        pending_confirmation: list[str] = []

        for pid, pos in list(self.state.positions.items()):
            exp = self._pos_expected(pos)
            act = {k: actual.get(k, 0) for k in exp}
            claimed |= set(exp)
            if act == exp:
                self._mismatch_seen.pop(pid, None)
                continue
            if not self._confirmed(pid, startup):
                pending_confirmation.append(pid)
                continue
            self._mismatch_seen.pop(pid, None)
            detail = {key_label(k): {"engine": exp[k], "broker": act[k]} for k in exp}
            if all(q == 0 for q in act.values()):
                events.append(self._manual_exit(pos, now, detail, startup))
            elif self._consistent_reduction(exp, act):
                events.append(self._adopt_quantity(pos, now, exp, act, detail))
            else:
                events.append(self._unmanage(pid, pos, now, exp, act, "position changed outside the engine"))

        for pid, pend in list(self.state.pending_entries.items()):
            exp = {key_from_json(l["key"]): (-1 if l["side"] == "SELL" else 1) * l["qty"] for l in pend["legs"]}
            act = {k: actual.get(k, 0) for k in exp}
            claimed |= set(exp)
            if not self._confirmed("pending:" + pid, startup):
                pending_confirmation.append("pending:" + pid)
                continue
            self._mismatch_seen.pop("pending:" + pid, None)
            del self.state.pending_entries[pid]
            if all(q == 0 for q in act.values()):
                self._stop_managing({"pid": pid, "strategy": pend["strategy"]}, "entry not filled (broker flat)")
                events.append({"event": "pending_entry_dropped", "position_id": pid})
                self.audit.write("pending_entry_dropped", now, level=logging.WARNING, position_id=pid,
                                 reason="entry was sent but the broker holds nothing")
            elif act == exp or self._consistent_reduction(exp, act):
                events.append(self._adopt_pending(pend, act, now))
            else:
                events.append(self._unmanage(pid, {"pid": pid, "strategy": pend["strategy"]}, now, exp, act,
                                             "pending entry does not match broker"))

        # broker positions nobody in the engine claims
        for key, u in list(self.state.unmanaged.items()):
            k = tuple(key_from_json(u["key"])) if u.get("key") else None
            if k is not None:
                claimed.add(k)
                if actual.get(k, 0) == 0:
                    del self.state.unmanaged[key]
                    self.audit.write("unmanaged_gone", now, position_id=key, contract=key_label(k),
                                     reason="broker no longer holds it")
                else:
                    u["broker_qty"] = actual[k]
        for k, q in actual.items():
            if k in claimed or not q:
                continue
            if len(k) != 4 or k[0] != self.cfg.underlying:
                continue                                  # not an option of the traded underlying: not ours
            if not self._confirmed(f"unknown:{key_label(k)}", startup):
                pending_confirmation.append(f"unknown:{key_label(k)}")
                continue
            uid = f"BROKER {key_label(k)}"
            self.state.unmanaged[uid] = {"key": key_to_json(k), "broker_qty": q, "first_seen": now.isoformat(),
                                         "reason": "open at the broker, not opened by this engine"}
            events.append({"event": "unknown_position", "contract": key_label(k), "broker_qty": q})
            self.audit.write("unknown_position", now, level=logging.CRITICAL, contract=key_label(k), broker_qty=q,
                             action="adopted read-only: the engine will not trade or close it")
            if self.cfg.halt_on_unknown_positions:
                self._halt(now, f"reconciliation: unknown broker position {key_label(k)} x{q}")

        if startup and open_orders:
            self._halt(now, f"reconciliation: {len(open_orders)} open order(s) at the broker at startup")
        result = {"ok": not events and not pending_confirmation and not (startup and open_orders),
                  "events": events, "pending_confirmation": pending_confirmation,
                  "open_orders": len(open_orders), "engine_positions": len(self.state.positions),
                  "unmanaged": list(self.state.unmanaged)}
        level = logging.DEBUG if result["ok"] and not open_orders else logging.WARNING
        self.audit.write("reconcile", now, level=level, startup=startup, **result)
        return result

    @staticmethod
    def _consistent_reduction(exp: dict, act: dict) -> bool:
        ratios = set()
        for k, e in exp.items():
            a = act[k]
            if e == 0 or a == 0 or (a > 0) != (e > 0) or abs(a) > abs(e):
                return False
            ratios.add(round(a / e, 6))
        return len(ratios) == 1 and ratios != {1.0}

    def _manual_exit(self, pos: dict, now: datetime, detail: dict, startup: bool) -> dict:
        """Closed outside the engine: record MANUAL_EXIT (never a stop), no order, no re-entry for this signal."""
        self._stop_managing(pos, MANUAL_EXIT)
        self.state.blocked[pos["root"]] = MANUAL_EXIT
        for l in pos["legs"]:                         # estimated exit at the last known price
            open_qty = l["qty"] - l["closed_qty"]
            if open_qty:
                px = self.market.option_price(contract_from_dict(l["contract"]), now) or l["last_price"] or \
                    l["entry_price"] or 0.0
                l["closed_qty"] += open_qty
                l["exit_value"] += px * open_qty
        rec = self._close(pos, now, MANUAL_EXIT + (" (found at startup)" if startup else ""), None,
                          status=MANUAL_EXIT, estimated=True)
        self.audit.write("manual_exit", now, level=logging.WARNING, position_id=pos["pid"], detail=detail,
                         action="no orders; re-entry disabled for this signal", estimated_pnl=rec["gross_pnl"])
        return {"event": MANUAL_EXIT, "position_id": pos["pid"]}

    def _adopt_quantity(self, pos: dict, now: datetime, exp: dict, act: dict, detail: dict) -> dict:
        for l in pos["legs"]:
            k = key_from_json(l["key"])
            l["qty"] = l["closed_qty"] + abs(act[k])
        self.audit.write("adopted_broker_quantity", now, level=logging.WARNING, position_id=pos["pid"],
                         detail=detail, action="engine quantity set to the broker's; still managed")
        return {"event": "adopted_quantity", "position_id": pos["pid"]}

    def _unmanage(self, pid: str, pos: dict, now: datetime, exp: dict, act: dict, reason: str) -> dict:
        """Cannot make sense of it: stop all management of this signal (no stop, re-entry or orders),
        keep what the broker holds read-only, halt new entries."""
        self._stop_managing(pos, "UNMANAGED: " + reason)
        self.state.blocked[root_of(pid)] = "UNMANAGED"
        self.state.positions.pop(pid, None)
        detail = {key_label(k): {"engine": exp[k], "broker": act[k]} for k in exp}
        for k in exp:
            if act[k]:
                self.state.unmanaged[f"{pid} {key_label(k)}"] = {
                    "key": key_to_json(k), "broker_qty": act[k], "engine_qty": exp[k], "position_id": pid,
                    "first_seen": now.isoformat(), "reason": reason}
        self.audit.write("position_unmanaged", now, level=logging.CRITICAL, position_id=pid, detail=detail,
                         reason=reason, action="no further orders for this signal; check it in Kite")
        self._halt(now, f"reconciliation: {pid} {reason}")
        return {"event": "UNMANAGED", "position_id": pid}

    def _adopt_pending(self, pend: dict, act: dict, now: datetime) -> dict:
        """Crash between sending an entry and recording it: rebuild the position from the broker + saved signal."""
        legs = []
        for l in pend["legs"]:
            k = key_from_json(l["key"])
            price = pend["ref_price"] if l["role"] == "MAIN" else pend.get("wing_ref")
            legs.append(self._leg(k, l["side"], l["role"], abs(act[k]), contract_from_dict(l["contract"]), price))
        pos = self._position(pend["pid"], pend["strategy"], pend["is_reentry"], pend["ts"], now, pend["spot"],
                             pend["reason"], pend["stop"], legs, None)
        pos["entry_price_estimated"] = True
        self._open(pos, now, pend["is_reentry"], adopted_from_broker=True,
                   note="entry prices are the signal's reference prices (actual fills unknown)")
        self.audit.write("adopted_pending_entry", now, level=logging.WARNING, position_id=pend["pid"],
                         action="position rebuilt from broker quantities + saved signal; managed normally")
        return {"event": "adopted_pending_entry", "position_id": pend["pid"]}

    # =====================================================================================
    # helpers
    # =====================================================================================
    def _roll_day(self, today: date, now: datetime) -> None:
        if self.state.day == today.isoformat():
            return
        prev = self.state.day
        self.state.day = today.isoformat()
        self.state.trades_today = 0
        self.state.realized_today = 0.0
        self.state.session_status = None
        self.last_mtm = {}
        if self.state.halted and (self.state.halted.startswith("max daily loss") or self.state.halted == MAX_PROFIT_REACHED):
            self.state.halted = None
        for pos in self.state.positions.values():          # carried positions: today's P&L starts from yesterday's mark
            for l in pos["legs"]:
                l["day_ref_price"] = l.get("last_price") or l["entry_price"]
        self.audit.write("day_start", now, previous_day=prev, carried_positions=list(self.state.positions),
                         halted=self.state.halted,
                         level=logging.CRITICAL if self.cfg.intraday_only and self.state.positions else logging.INFO)

    def _halt(self, now: datetime, reason: str) -> None:
        self.state.halted = reason
        self.audit.write("halt_new_entries", now, level=logging.CRITICAL, reason=reason)

    def _drain_notes(self, strat, now: datetime) -> None:
        for n in strat.notes:
            self.audit.write("decision", now, **n)
        strat.notes.clear()

    def _audit_order(self, now: datetime, intent: OrderIntent, res: ExecutionResult) -> None:
        self.audit.write("order", now, level=logging.INFO if res.ok else logging.ERROR,
                         intent_id=intent.intent_id, action=intent.action.value, position_id=intent.position_id,
                         broker=self.broker.name, live_orders=self.broker.live, ok=res.ok, message=res.message,
                         uncertain=res.uncertain, plan=res.plan,
                         legs=[{"contract": f"{l.underlying} {l.expiry} {l.strike:g} {l.right.value}",
                                "role": l.role.value, "entry_side": l.side.value, "qty": l.quantity} for l in intent.legs],
                         fills=[{"symbol": f.tradingsymbol, "side": f.side, "qty": f.quantity, "avg": f.average_price,
                                 "order_ids": f.order_ids, "statuses": f.statuses} for f in res.fills],
                         unwound=[{"symbol": f.tradingsymbol, "side": f.side, "qty": f.quantity,
                                   "avg": f.average_price, "order_ids": f.order_ids} for f in res.unwound],
                         margin_required=res.margin_required, margin_available=res.margin_available)

    def _config_summary(self) -> dict:
        return {k: (str(v) if not isinstance(v, (int, float, bool, str, type(None))) else v)
                for k, v in self.cfg.__dict__.items()}
