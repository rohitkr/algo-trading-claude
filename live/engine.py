"""The trading engine: one Account running one or more independent algo instances.

    Account (one per process)              TradingEngine (one per instance)
      market data, broker, kill switches     one strategy, its RiskManager, its own state file,
      reconciliation across instances        its own audit log, its own daily caps, its own order tag
      unknown broker positions, account caps

Startup: every instance's saved state is cross-checked against the broker before anything else; the
broker's state wins (Account.reconcile).

One Account.step(now) per poll:
  1. global kill switch    -> square off every instance, stop the process
  2. per-instance kill     -> KILL_<id>: square off that instance, skip it while the file exists
  3. new trading day?      -> each instance resets its daily counters/caps and re-bases day P&L
  4. each instance         -> strategy signals -> risk -> orders; intraday force exit / carry rules;
                              failed-exit retries; mark to market + its own daily caps
  5. account caps          -> ACCOUNT_MAX_DAILY_LOSS / _PROFIT across all instances (optional)
  6. data health           -> stale feed blocks entries; optional square-off on long outages
  7. reconciliation        -> broker positions are the truth, attributed to the owning instance
  8. save every state file
The engine never imports Breeze or Kite: it only sees the interfaces in live/interfaces.py.
"""
from __future__ import annotations

import logging
import time as _time
from datetime import date, datetime, timedelta
from pathlib import Path
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


# =========================================================================================
# Account: the process-level layer shared by all instances
# =========================================================================================
class Account:
    def __init__(self, cfg: EngineConfig, market: MarketDataProvider, broker: ExecutionBroker,
                 instances: list["TradingEngine"], state: EngineState, audit: AuditLog):
        self.cfg, self.market, self.broker, self.state, self.audit = cfg, market, broker, state, audit
        self.instances: list[TradingEngine] = []
        self.stopped = False
        self.save_hooks: list[Callable[[], None]] = []
        self._last_reconcile: datetime | None = None
        self._last_outage_alert: datetime | None = None
        self._data_errors_seen = 0
        self._mismatch_seen: dict[str, int] = {}
        for inst in instances:
            self.add(inst)

    def add(self, inst: "TradingEngine") -> None:
        if any(i.id == inst.id for i in self.instances):
            raise ValueError(f"duplicate instance id {inst.id}")
        inst.account = self
        self.instances.append(inst)

    def instance(self, iid: str) -> "TradingEngine":
        return next(i for i in self.instances if i.id == iid)

    # -- loop -------------------------------------------------------------------------------
    def run(self, now_fn: Callable[[], datetime], sleep: Callable[[float], None] = _time.sleep,
            warnings: list[str] | None = None) -> None:
        now = now_fn()
        self.audit.write("engine_start", now, mode=self.cfg.mode, broker=self.broker.name, live_orders=self.broker.live,
                         instances={i.id: i.summary() for i in self.instances}, warnings=warnings or [])
        for w in warnings or []:
            self.audit.write("config_warning", now, level=logging.WARNING, detail=w)
        self.startup(now)
        cal = self.instances[0].selector
        try:
            while not self.stopped:
                now = now_fn()
                if not cal.is_trading_day(now.date()) or now.time() > self.cfg.stop_time:
                    self.audit.write("engine_stop", now, reason="not a trading day" if not cal.is_trading_day(now.date())
                                     else f"after {self.cfg.stop_time:%H:%M}",
                                     open_positions={i.id: list(i.state.positions) for i in self.instances})
                    break
                self.step(now)
                sleep(self.cfg.poll_seconds)
        except KeyboardInterrupt:
            self.audit.write("engine_stop", now_fn(), level=logging.WARNING, reason="Ctrl-C (positions NOT closed)",
                             open_positions={i.id: list(i.state.positions) for i in self.instances})
        finally:
            self.save()

    def startup(self, now: datetime) -> dict:
        """Cross-check every instance's saved state against the broker; the broker's positions win."""
        res = self.reconcile(now, startup=True)
        for inst in self.instances:
            inst.audit.write("startup_reconcile", now, positions=list(inst.state.positions),
                             carried=[p for p, x in inst.state.positions.items() if x.get("carry_allowed")],
                             halted=inst.state.halted, level=logging.INFO if res.get("ok") else logging.WARNING)
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
        sel = self.instances[0].selector
        today = now.date()
        if not sel.is_trading_day(today):
            return
        self._roll_day(today, now)
        open_t, close_t = sel.cal.session(today)
        in_session = datetime.combine(today, open_t) + MINUTE <= now <= datetime.combine(today, close_t) + 2 * MINUTE
        for inst in self.instances:
            kill = inst.kill_file
            if kill.exists():
                if not inst.killed:
                    inst.square_off_all(now, f"kill switch {kill}")
                    inst.killed = True
                continue
            inst.killed = False
            if in_session:
                try:
                    inst._step(now)
                except Exception as exc:                          # one instance's bug must not stop the others
                    inst.audit.write("instance_error", now, level=logging.ERROR, error=f"{type(exc).__name__}: {exc}")
                    log.exception("instance %s failed", inst.id)
        if not in_session:
            return
        self._account_caps(now)
        self._data_health(now)
        if self._last_reconcile is None or (now - self._last_reconcile).total_seconds() >= self.cfg.reconcile_seconds:
            self.reconcile(now)
        self.save()

    def save(self) -> None:
        for inst in self.instances:
            inst._save_state()
        self.state.save()
        for hook in self.save_hooks:
            hook()

    def request_reconcile(self) -> None:
        self._last_reconcile = None

    def square_off_all(self, now: datetime, reason: str) -> None:
        for inst in self.instances:
            inst.square_off_all(now, reason, reconcile=False)
        self.halt(now, f"square-off: {reason}")
        self.reconcile(now)

    def halt(self, now: datetime, reason: str) -> None:
        self.state.halted = reason
        self.audit.write("halt_all_entries", now, level=logging.CRITICAL, reason=reason)

    def _roll_day(self, today: date, now: datetime) -> None:
        if self.state.day != today.isoformat():
            self.state.day = today.isoformat()
            if self.state.halted and self.state.halted.startswith("account "):
                self.state.halted = None
        for inst in self.instances:
            inst._roll_day(today, now)

    # -- account-wide caps (optional) --------------------------------------------------------
    def _account_caps(self, now: datetime) -> None:
        c = self.cfg
        pnl = sum(i.daily_pnl() for i in self.instances)
        if c.account_max_daily_profit > 0 and pnl >= c.account_max_daily_profit and \
                self.state.session_status != MAX_PROFIT_REACHED:
            self.state.session_status = MAX_PROFIT_REACHED
            self.audit.write("account_max_daily_profit", now, level=logging.WARNING, pnl=round(pnl, 2))
            self.square_off_all(now, f"account {MAX_PROFIT_REACHED} ({pnl:,.0f})")
            self.state.halted = f"account {MAX_PROFIT_REACHED}"
        elif c.account_max_daily_loss > 0 and pnl <= -c.account_max_daily_loss and self.state.session_status is None:
            self.state.session_status = MAX_LOSS_REACHED
            self.halt(now, f"account max daily loss reached ({pnl:,.0f})")

    # -- data health ---------------------------------------------------------------------------
    def data_lag(self, now: datetime) -> float | None:
        bars = self.market.spot_bars(now.date(), now)
        if bars is None or bars.empty:
            open_t = self.instances[0].selector.cal.session(now.date())[0]
            return max(0.0, (now - datetime.combine(now.date(), open_t)).total_seconds() - 60)
        return (now - (bars.index.max().to_pydatetime() + MINUTE)).total_seconds()

    def _data_health(self, now: datetime) -> None:
        errors = getattr(self.market, "errors", 0)
        if errors > self._data_errors_seen:
            self.audit.write("data_error", now, level=logging.WARNING, errors=errors - self._data_errors_seen,
                             last_error=getattr(self.market, "last_error", None))
            self._data_errors_seen = errors
        lag = self.data_lag(now)
        close_t = self.instances[0].selector.cal.session(now.date())[1]
        if lag is None or lag < self.cfg.data_outage_s or now.time() > close_t:
            return
        if self._last_outage_alert and (now - self._last_outage_alert).total_seconds() < 300:
            return
        self._last_outage_alert = now
        held = {i.id: list(i.state.positions) for i in self.instances if i.state.positions}
        self.audit.write("data_outage", now, level=logging.CRITICAL, lag_seconds=round(lag), open_positions=held,
                         action="square off" if self.cfg.square_off_on_data_outage else "alert only (stops blind)")
        if self.cfg.square_off_on_data_outage and held:
            self.square_off_all(now, f"market data down {lag:.0f}s")

    # -- reconciliation: the broker's positions are the truth ---------------------------------
    def _confirmed(self, tag: str, startup: bool) -> bool:
        """A mismatch is acted on only after ENGINE_RECONCILE_CONFIRMATIONS consecutive checks (broker
        position reports can lag a fill); at startup there is no race, so immediately."""
        self._mismatch_seen[tag] = self._mismatch_seen.get(tag, 0) + 1
        return startup or self._mismatch_seen[tag] >= self.cfg.reconcile_confirmations

    def reconcile(self, now: datetime, startup: bool = False) -> dict:
        """Compare every instance's book with the broker's net positions and adopt the broker's state.

        Each broker contract is attributed to the instance(s) whose positions or pending entries claim it.
        Contract claimed by ONE instance position (the default, SHARED_CONTRACTS=false):
          * broker flat on every leg        -> MANUAL_EXIT for that instance: no order, no re-entry
          * same side, consistently smaller -> adopt the broker quantity and keep managing
          * anything else                   -> that instance stops managing it (UNMANAGED) and halts
        Contract claimed by several (SHARED_CONTRACTS=true): the broker must equal the sum; otherwise the
          difference cannot be attributed, so every claimant halts new entries (nothing is changed).
        Pending entries (sent, crash before the result was recorded): adopt / drop / UNMANAGED + halt.
        Broker positions nobody claims (options of UNDERLYING) -> your manual trade: read-only, alert,
          halt all instances (ENGINE_HALT_ON_UNKNOWN_POSITIONS).
        Open orders at startup: tagged with an instance id -> that instance halts; untagged -> yours, ignored.
        """
        self._last_reconcile = now
        try:
            actual = dict(self.broker.positions())
            open_orders = self.broker.open_orders()
        except Exception as exc:
            self.audit.write("reconcile_error", now, level=logging.ERROR, error=f"{type(exc).__name__}: {exc}")
            return {"ok": False, "error": str(exc)}
        events: list[dict] = []
        pending_confirmation: list[str] = []

        claims: dict[tuple, list] = {}                        # key -> [(instance, tag, expected qty)]
        for inst in self.instances:
            for pid, pos in inst.state.positions.items():
                for k, q in inst.pos_expected(pos).items():
                    claims.setdefault(k, []).append((inst, pid, q))
            for pid, pend in inst.state.pending_entries.items():
                for k, q in inst.pending_expected(pend).items():
                    claims.setdefault(k, []).append((inst, "pending:" + pid, q))
        shared = {k for k, v in claims.items() if len(v) > 1}

        for k in sorted(shared, key=str):                     # several claimants: only the sum can be checked
            exp = sum(q for _, _, q in claims[k])
            if exp == actual.get(k, 0):
                self._mismatch_seen.pop("shared:" + key_label(k), None)
                continue
            if not self._confirmed("shared:" + key_label(k), startup):
                pending_confirmation.append("shared:" + key_label(k))
                continue
            owners = sorted({i.id for i, _, _ in claims[k]})
            events.append({"event": "shared_contract_mismatch", "contract": key_label(k), "engine": exp,
                           "broker": actual.get(k, 0), "instances": owners})
            self.audit.write("shared_contract_mismatch", now, level=logging.CRITICAL, contract=key_label(k),
                             engine=exp, broker=actual.get(k, 0), instances=owners,
                             action="cannot attribute the difference: every claimant halts new entries")
            for inst in {i for i, _, _ in claims[k]}:
                inst._halt(now, f"reconciliation: shared contract {key_label(k)} does not match the broker")

        for inst in self.instances:
            for pid, pos in list(inst.state.positions.items()):
                exp = inst.pos_expected(pos)
                if set(exp) & shared:
                    continue
                act = {k: actual.get(k, 0) for k in exp}
                tag = f"{inst.id}:{pid}"
                if act == exp:
                    self._mismatch_seen.pop(tag, None)
                    continue
                if not self._confirmed(tag, startup):
                    pending_confirmation.append(tag)
                    continue
                self._mismatch_seen.pop(tag, None)
                detail = {key_label(k): {"engine": exp[k], "broker": act[k]} for k in exp}
                if all(q == 0 for q in act.values()):
                    events.append(inst._manual_exit(pos, now, detail, startup))
                elif consistent_reduction(exp, act):
                    events.append(inst._adopt_quantity(pos, now, act, detail))
                else:
                    events.append(inst._unmanage(pid, pos, now, exp, act, "position changed outside the engine"))
            for pid, pend in list(inst.state.pending_entries.items()):
                exp = inst.pending_expected(pend)
                if set(exp) & shared:
                    continue
                act = {k: actual.get(k, 0) for k in exp}
                tag = f"{inst.id}:pending:{pid}"
                if not self._confirmed(tag, startup):
                    pending_confirmation.append(tag)
                    continue
                self._mismatch_seen.pop(tag, None)
                del inst.state.pending_entries[pid]
                if all(q == 0 for q in act.values()):
                    inst._stop_managing({"pid": pid, "strategy": pend["strategy"]}, "entry not filled (broker flat)")
                    events.append({"event": "pending_entry_dropped", "instance": inst.id, "position_id": pid})
                    inst.audit.write("pending_entry_dropped", now, level=logging.WARNING, position_id=pid,
                                     reason="entry was sent but the broker holds nothing")
                elif act == exp or consistent_reduction(exp, act):
                    events.append(inst._adopt_pending(pend, act, now))
                else:
                    events.append(inst._unmanage(pid, {"pid": pid, "strategy": pend["strategy"]}, now, exp, act,
                                                 "pending entry does not match broker"))

        # read-only positions: the instances' UNMANAGED ones and the account's unknown ones
        known: set = set(claims)
        for holder in [i.state for i in self.instances] + [self.state]:
            for uid, u in list(holder.unmanaged.items()):
                k = tuple(key_from_json(u["key"])) if u.get("key") else None
                if k is None:
                    continue
                known.add(k)
                if actual.get(k, 0) == 0:
                    del holder.unmanaged[uid]
                    self.audit.write("unmanaged_gone", now, position_id=uid, contract=key_label(k),
                                     reason="broker no longer holds it")
                else:
                    u["broker_qty"] = actual[k]
        underlying = {i.cfg.underlying for i in self.instances}
        for k, q in actual.items():
            if k in known or not q or len(k) != 4 or k[0] not in underlying:
                continue                                  # yours / not an option of the traded underlying
            tag = f"unknown:{key_label(k)}"
            if not self._confirmed(tag, startup):
                pending_confirmation.append(tag)
                continue
            uid = f"BROKER {key_label(k)}"
            self.state.unmanaged[uid] = {"key": key_to_json(k), "broker_qty": q, "first_seen": now.isoformat(),
                                         "reason": "open at the broker, not opened by any instance"}
            events.append({"event": "unknown_position", "contract": key_label(k), "broker_qty": q})
            self.audit.write("unknown_position", now, level=logging.CRITICAL, contract=key_label(k), broker_qty=q,
                             action="adopted read-only: no instance will trade or close it")
            if self.cfg.halt_on_unknown_positions:
                self.halt(now, f"reconciliation: unknown broker position {key_label(k)} x{q}")

        ids = {i.id: i for i in self.instances}
        tagged = [o for o in open_orders if o.get("tag") in ids]
        if startup:
            for o in tagged:
                ids[o["tag"]]._halt(now, f"reconciliation: open order {o.get('order_id')} of this instance at startup")
            if open_orders and not tagged:
                self.audit.write("manual_open_orders", now, level=logging.INFO, count=len(open_orders),
                                 note="open orders without an instance tag: treated as yours, ignored")
        result = {"ok": not events and not pending_confirmation and not (startup and tagged),
                  "events": events, "pending_confirmation": pending_confirmation,
                  "open_orders": len(open_orders), "instance_open_orders": len(tagged),
                  "positions": {i.id: len(i.state.positions) for i in self.instances},
                  "unmanaged": list(self.state.unmanaged)}
        level = logging.DEBUG if result["ok"] and not tagged else logging.WARNING
        self.audit.write("reconcile", now, level=level, startup=startup, **result)
        return result


def consistent_reduction(exp: dict, act: dict) -> bool:
    ratios = set()
    for k, e in exp.items():
        a = act[k]
        if e == 0 or a == 0 or (a > 0) != (e > 0) or abs(a) > abs(e):
            return False
        ratios.add(round(a / e, 6))
    return len(ratios) == 1 and ratios != {1.0}


# =========================================================================================
# TradingEngine: one algo instance
# =========================================================================================
class TradingEngine:
    def __init__(self, cfg: EngineConfig, market: MarketDataProvider, broker: ExecutionBroker,
                 selector: ContractSelector, strategies: list, risk: RiskManager, state: EngineState,
                 audit: AuditLog, account: Account | None = None):
        self.cfg, self.market, self.broker, self.selector = cfg, market, broker, selector
        self.strategies, self.risk, self.state, self.audit = strategies, risk, state, audit
        self.id = cfg.instance_id
        self.by_name = {s.name: s for s in strategies}
        for s in strategies:
            s.set_state(state.strategies.get(s.name, {}))
        self.killed = False
        self.last_mtm: dict = {}
        self.closed: list[dict] = []                   # positions closed by this process (replay comparison)
        self.account: Account
        if account is None:                            # a single instance on its own (tests, simple runs)
            acct_state = EngineState.load(Path(state.path).with_name(Path(state.path).stem + "_account.json"))
            Account(cfg, market, broker, [self], acct_state, audit)
        else:
            account.add(self)

    @property
    def kill_file(self) -> Path:
        return self.cfg.kill_file.with_name(f"{self.cfg.kill_file.name}_{self.id}")

    def summary(self) -> dict:
        c = self.cfg
        return {"strategy": c.strategy, "position_mode": c.position_mode,
                "hedge_width": c.hedge_width if c.hedged else None, "intraday_only": c.intraday_only,
                "expiry_offset": c.expiry_offset, "max_daily_loss": c.max_daily_loss if c.max_daily_loss_enabled
                else None, "max_daily_profit": c.max_daily_profit if c.max_daily_profit_enabled else None,
                "lots": c.positional_lots if c.strategy == "positional" else c.zerodte_lots}

    # -- single-instance conveniences (delegate to the account) --------------------------------
    @property
    def stopped(self) -> bool:
        return self.account.stopped

    @property
    def save_hooks(self) -> list:
        return self.account.save_hooks

    def run(self, now_fn, sleep=_time.sleep) -> None:
        self.account.run(now_fn, sleep)

    def startup(self, now: datetime) -> dict:
        return self.account.startup(now)

    def step(self, now: datetime) -> None:
        self.account.step(now)

    def reconcile(self, now: datetime, startup: bool = False) -> dict:
        return self.account.reconcile(now, startup)

    def save(self) -> None:
        self.account.save()

    def _save_state(self) -> None:
        for s in self.strategies:
            self.state.strategies[s.name] = s.get_state()
        self.state.save()

    # =====================================================================================
    # one poll for this instance
    # =====================================================================================
    def _step(self, now: datetime) -> None:
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
        self._carry_rules(now)
        self._retry_pending_exits(now)
        self._mark_to_market(now)

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
        keys = {tuple(key_to_json(contract_key(main)))} | ({tuple(key_to_json(contract_key(wing)))} if wing else set())
        mine, others = self._held_keys()
        clash = keys & (mine | (set() if cfg.shared_contracts else others))
        halted = self.state.halted or self.account.state.halted
        rc = RiskContext(
            now=now, session=self.selector.cal.session(now.date()) if self.selector.is_trading_day(now.date()) else None,
            halted=halted, data_lag_s=self.account.data_lag(now), api_budget=self.market.api_budget_remaining(),
            open_positions=len(self.state.positions), trades_today=self.state.trades_today,
            daily_pnl=self.daily_pnl(), reentries_for_root=self.state.counters.get("reentries", {}).get(root, 0),
            signal_blocked=self.state.blocked.get(root),
            already_sent=self.state.was_sent(f"{sig.position_id}:entry"),
            contract_already_open=bool(clash),
            lot_size=lot, risk_per_unit=risk_unit, margin_per_lot=margin_per_lot, available_margin=available,
            hedge=hedge_info)
        decision = self.risk.evaluate_entry(sig, rc)
        checks = [{"check": c.name, "passed": c.passed, "detail": c.detail} for c in decision.checks]
        if clash:
            checks.append({"check": "contract_free", "passed": False,
                           "detail": f"{sorted(clash)} already held by "
                                     f"{'this instance' if keys & mine else 'another instance'}"})
        self.audit.write("risk_check", now, position_id=sig.position_id, ok=decision.ok, lots=decision.lots,
                         checks=checks, metrics=decision.metrics, level=logging.INFO if decision.ok else logging.WARNING)
        if not decision.ok:
            strat.on_entry_result(sig, False)
            return

        intent = self._intent(sig, decision.lots, wing, "entry")
        sent_id = f"{sig.position_id}:entry"
        carry = self.cfg.carry_allowed
        hold_until = sig.meta.get("final_exit") if carry and sig.meta.get("final_exit") else \
            datetime.combine(sig.ts.date(), self.cfg.force_exit_time).isoformat()
        self.state.mark_sent(sent_id)
        self.state.pending_entries[sig.position_id] = {       # lets a restart adopt it if we crash mid-order
            "pid": sig.position_id, "strategy": sig.strategy, "ts": sig.ts.isoformat(), "wall": now.isoformat(),
            "spot": sig.spot, "reason": sig.reason, "stop": sig.stop, "is_reentry": sig.is_reentry,
            "ref_price": main_ref, "wing_ref": wing_ref, "carry_allowed": carry, "hold_until": hold_until,
            "legs": [{"key": key_to_json(l.key), "side": l.side.value, "role": l.role.value, "qty": l.quantity,
                      "contract": contract_to_dict(self._contract(l))} for l in intent.legs]}
        self.account.save()                            # duplicate guard + pending entry must survive a crash
        res = self.broker.execute(intent)
        self._audit_order(now, intent, res)
        if not res.ok:
            strat.on_entry_result(sig, False)
            if res.uncertain:                          # keep the pending entry: reconciliation decides
                self._halt(now, f"broker error on {intent.intent_id}: {res.message}")
                self.account.reconcile(now)
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
                             sig.stop, legs, res.margin_required, carry, hold_until)
        self._open(pos, now, sig.is_reentry, signal_ref_price=main_ref)
        strat.on_entry_result(sig, True)

    def _held_keys(self) -> tuple[set, set]:
        """Contracts held by this instance, and by every other instance or the account's read-only list."""
        def keys(inst):
            out = {tuple(l["key"]) for p in inst.state.positions.values() for l in p["legs"]}
            out |= {tuple(l["key"]) for p in inst.state.pending_entries.values() for l in p["legs"]}
            out |= {tuple(u["key"]) for u in inst.state.unmanaged.values() if u.get("key")}
            return out
        others = set().union(*[keys(i) for i in self.account.instances if i is not self]) if \
            len(self.account.instances) > 1 else set()
        others |= {tuple(u["key"]) for u in self.account.state.unmanaged.values() if u.get("key")}
        return keys(self), others

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
                         position_mode=pos["position_mode"], carry_allowed=pos["carry_allowed"],
                         hold_until=pos["hold_until"], is_reentry=is_reentry,
                         order_ids=[o for l in pos["legs"] for o in l["order_ids"]], **extra)
        self.account.request_reconcile()              # reconcile right after every order

    def _leg(self, key, side, role, qty, contract, price, symbol="", order_ids=None) -> dict:
        return {"key": key_to_json(key), "contract": contract_to_dict(contract), "role": role, "side": side,
                "qty": qty, "symbol": symbol, "entry_price": price, "day_ref_price": price, "last_price": price,
                "closed_qty": 0, "exit_value": 0.0, "order_ids": order_ids or [], "exit_order_ids": []}

    def _position(self, pid, strategy, is_reentry, ts, now, spot, reason, stop, legs, margin, carry: bool,
                  hold_until: str) -> dict:
        main = next(x for x in legs if x["role"] == "MAIN")
        wing = next((x for x in legs if x["role"] == "HEDGE"), None)
        hedge = None
        if wing and main["entry_price"] is not None and wing["entry_price"] is not None:
            credit = main["entry_price"] - wing["entry_price"]
            width = abs(wing["contract"]["strike"] - main["contract"]["strike"])
            hedge = {"strike": wing["contract"]["strike"], "entry_price": wing["entry_price"],
                     "net_credit": round(credit, 2), "max_loss": round((width - credit) * main["qty"], 2),
                     "width": width}
        entry_ts = ts.isoformat() if isinstance(ts, datetime) else ts
        return {"pid": pid, "root": root_of(pid), "instance": self.id, "strategy": strategy, "status": "open",
                "is_reentry": is_reentry, "entry_ts": entry_ts, "entry_date": entry_ts[:10],
                "entry_wall": now.isoformat(), "entry_spot": spot, "reason": reason, "stop": stop, "legs": legs,
                "position_mode": "HEDGED" if wing else "NAKED", "hedge": hedge, "margin": margin,
                "carry_allowed": carry, "hold_until": hold_until, "exit_attempts": 0,
                "entry_credit": main["entry_price"]}

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
        # broker-side ids carry the instance id: the executor de-duplicates on intent_id across all instances
        return OrderIntent(f"{self.id}:{sig.position_id}:{tag}", f"{self.id}:{sig.position_id}", Action.ENTRY,
                           tuple(legs), sig.ts, sig.strategy, sig.reason, meta={"tag": self.id})

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
        intent = OrderIntent(f"{self.id}:{pid}:exit{attempt or ''}", f"{self.id}:{pid}", Action.EXIT, legs,
                             sig.ts if sig else now, pos["strategy"], reason, meta={"tag": self.id})
        self.state.mark_sent(f"{pid}:exit{attempt or ''}")
        res = self.broker.execute(intent)
        self._audit_order(now, intent, res)
        for f in res.fills:
            for l in remaining:
                if tuple(key_from_json(l["key"])) == f.key:
                    l["closed_qty"] += f.quantity
                    l["exit_value"] += f.average_price * f.quantity
                    l.setdefault("exit_order_ids", []).extend(f.order_ids)
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
            self.account.reconcile(now)
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
        exit_ts = (sig.ts if sig else now)
        entry_day = date.fromisoformat(pos.get("entry_date") or pos["entry_ts"][:10])
        rec = dict(instance=self.id, position_id=pos["pid"], strategy=pos["strategy"], status=status,
                   underlying=main["contract"]["underlying"], expiry=main["contract"]["expiry"],
                   right=main["contract"]["right"], strike=main["contract"]["strike"], side=main["side"],
                   quantity=main["qty"], entry_ts=pos["entry_ts"], entry_spot=pos["entry_spot"],
                   entry_price=main["entry_price"], stop=pos["stop"], exit_ts=exit_ts.isoformat(),
                   exit_spot=sig.spot if sig else None, exit_price=main.get("exit_price"), exit_reason=reason,
                   gross_pnl=round(gross, 2), pnl_today=round(day, 2), pnl_estimated=estimated,
                   days_held=len(self.selector.cal.trading_days(entry_day, exit_ts.date())) - 1,
                   is_reentry=pos["is_reentry"],
                   hedge_strike=wing["contract"]["strike"] if wing else None,
                   hedge_entry_price=wing["entry_price"] if wing else None,
                   hedge_exit_price=wing.get("exit_price") if wing else None,
                   net_credit=(pos["hedge"] or {}).get("net_credit"), max_loss=(pos["hedge"] or {}).get("max_loss"),
                   margin=pos["margin"], mode=self.cfg.mode, position_mode=pos.get("position_mode"),
                   order_ids=[o for l in pos["legs"] for o in l["order_ids"]],
                   exit_order_ids=[o for l in pos["legs"] for o in l.get("exit_order_ids", [])])
        self.audit.write("position_closed", now, pnl=round(gross, 2),
                         level=logging.WARNING if status != "CLOSED" else logging.INFO, **rec)
        self.audit.trade(rec)
        self.closed.append(rec)
        self.account.request_reconcile()
        return rec

    def _carry_rules(self, now: datetime) -> None:
        """Positions that may not carry (intraday instances, 0DTE): square off at FORCE_EXIT_TIME and never
        let one into a new day. Positions that may carry: a safety exit only if still open past hold_until."""
        for pid, pos in list(self.state.positions.items()):
            if pos["status"] == "exit_pending":
                continue
            hold_until = datetime.fromisoformat(pos["hold_until"]) if pos.get("hold_until") else None
            if pos.get("carry_allowed"):
                if hold_until and now >= hold_until + 2 * MINUTE:
                    self._exit_position(pid, now, f"safety exit: still open after its final exit {hold_until:%Y-%m-%d %H:%M}",
                                        external=True)
                continue
            entry_day = datetime.fromisoformat(pos["entry_ts"]).date()
            if entry_day < now.date():
                self._exit_position(pid, now, f"intraday: carried over from {entry_day} (should not happen)",
                                    external=True)
            elif now >= datetime.combine(now.date(), self.cfg.force_exit_time) + MINUTE:
                # once the FORCE_EXIT_TIME bar has completed: the same moment the strategies' own time exits act
                self._exit_position(pid, now, f"intraday force exit {self.cfg.force_exit_time:%H:%M}", external=True)

    def square_off_all(self, now: datetime, reason: str, halt_reason: str | None = None, reconcile: bool = True) -> None:
        """Close every position this instance holds (retrying failures), then halt this instance."""
        self.audit.write("square_off", now, level=logging.CRITICAL, reason=reason, positions=list(self.state.positions))
        for pid in list(self.state.positions):
            for _ in range(max(1, self.cfg.exit_retry_limit)):
                if self._exit_position(pid, now, f"square-off: {reason}", external=True):
                    break
        self._halt(now, halt_reason or f"square-off: {reason}")
        if reconcile:
            self.account.reconcile(now)

    def _stop_managing(self, pos: dict, reason: str) -> None:
        strat = self.by_name.get(pos["strategy"])
        if strat is not None and hasattr(strat, "on_external_close"):
            strat.on_external_close(pos["pid"], reason)

    # =====================================================================================
    # P&L: one measure for both of this instance's daily caps
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
        """Realised today + unrealised mark-to-market of this instance's open positions (vs today's
        reference price: the entry fill on the entry day, the previous day's last mark afterwards)."""
        return self.state.realized_today + self.last_mtm.get("unrealized_today", 0.0)

    def _check_daily_limits(self, now: datetime) -> None:
        c, pnl = self.cfg, self.daily_pnl()
        if c.max_daily_profit_enabled and pnl >= c.max_daily_profit and self.state.session_status != MAX_PROFIT_REACHED:
            self.state.session_status = MAX_PROFIT_REACHED
            self.audit.write("max_daily_profit", now, level=logging.WARNING, pnl=round(pnl, 2),
                             cap=c.max_daily_profit, action="square off everything this instance holds "
                             "(incl. overnight positions), no more trades today")
            self.square_off_all(now, f"{MAX_PROFIT_REACHED} ({pnl:,.0f} >= {c.max_daily_profit:,.0f})",
                                halt_reason=MAX_PROFIT_REACHED)
        elif c.max_daily_loss_enabled and pnl <= -c.max_daily_loss and self.state.session_status is None:
            self.state.session_status = MAX_LOSS_REACHED
            self._halt(now, f"max daily loss reached ({pnl:,.0f})")

    # =====================================================================================
    # reconciliation actions (decided by Account.reconcile)
    # =====================================================================================
    @staticmethod
    def pos_expected(pos: dict) -> dict[tuple, int]:
        out: dict[tuple, int] = {}
        for l in pos["legs"]:
            k = key_from_json(l["key"])
            sign = -1 if l["side"] == "SELL" else 1
            out[k] = out.get(k, 0) + sign * (l["qty"] - l["closed_qty"])
        return out

    @staticmethod
    def pending_expected(pend: dict) -> dict[tuple, int]:
        return {key_from_json(l["key"]): (-1 if l["side"] == "SELL" else 1) * l["qty"] for l in pend["legs"]}

    def expected_positions(self) -> dict[tuple, int]:
        out: dict[tuple, int] = {}
        for pos in self.state.positions.values():
            for k, q in self.pos_expected(pos).items():
                out[k] = out.get(k, 0) + q
        return out

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
        return {"event": MANUAL_EXIT, "instance": self.id, "position_id": pos["pid"]}

    def _adopt_quantity(self, pos: dict, now: datetime, act: dict, detail: dict) -> dict:
        for l in pos["legs"]:
            l["qty"] = l["closed_qty"] + abs(act[key_from_json(l["key"])])
        self.audit.write("adopted_broker_quantity", now, level=logging.WARNING, position_id=pos["pid"],
                         detail=detail, action="engine quantity set to the broker's; still managed")
        return {"event": "adopted_quantity", "instance": self.id, "position_id": pos["pid"]}

    def _unmanage(self, pid: str, pos: dict, now: datetime, exp: dict, act: dict, reason: str) -> dict:
        """Cannot make sense of it: stop all management of this signal (no stop, re-entry or orders),
        keep what the broker holds read-only, halt this instance's new entries."""
        self._stop_managing(pos, "UNMANAGED: " + reason)
        self.state.blocked[root_of(pid)] = "UNMANAGED"
        self.state.positions.pop(pid, None)
        detail = {key_label(k): {"engine": exp[k], "broker": act[k]} for k in exp}
        lost_hedge = pos.get("carry_allowed") and any(
            l["role"] == "HEDGE" and act.get(key_from_json(l["key"])) == 0 for l in pos.get("legs", []))
        for k in exp:
            if act[k]:
                self.state.unmanaged[f"{pid} {key_label(k)}"] = {
                    "key": key_to_json(k), "broker_qty": act[k], "engine_qty": exp[k], "position_id": pid,
                    "first_seen": now.isoformat(), "reason": reason}
        self.audit.write("position_unmanaged", now, level=logging.CRITICAL, position_id=pid, detail=detail,
                         reason=("overnight position LOST ITS HEDGE; " if lost_hedge else "") + reason,
                         action="no further orders for this signal; check it in Kite")
        self._halt(now, f"reconciliation: {pid} {reason}")
        return {"event": "UNMANAGED", "instance": self.id, "position_id": pid}

    def _adopt_pending(self, pend: dict, act: dict, now: datetime) -> dict:
        """Crash between sending an entry and recording it: rebuild the position from the broker + saved signal."""
        legs = []
        for l in pend["legs"]:
            k = key_from_json(l["key"])
            price = pend["ref_price"] if l["role"] == "MAIN" else pend.get("wing_ref")
            legs.append(self._leg(k, l["side"], l["role"], abs(act[k]), contract_from_dict(l["contract"]), price))
        pos = self._position(pend["pid"], pend["strategy"], pend["is_reentry"], pend["ts"], now, pend["spot"],
                             pend["reason"], pend["stop"], legs, None, pend.get("carry_allowed", False),
                             pend.get("hold_until") or datetime.combine(date.fromisoformat(pend["ts"][:10]),
                                                                        self.cfg.force_exit_time).isoformat())
        pos["entry_price_estimated"] = True
        self._open(pos, now, pend["is_reentry"], adopted_from_broker=True,
                   note="entry prices are the signal's reference prices (actual fills unknown)")
        self.audit.write("adopted_pending_entry", now, level=logging.WARNING, position_id=pend["pid"],
                         action="position rebuilt from broker quantities + saved signal; managed normally")
        return {"event": "adopted_pending_entry", "instance": self.id, "position_id": pend["pid"]}

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
        carried = [p for p, x in self.state.positions.items() if x.get("carry_allowed")]
        stray = [p for p, x in self.state.positions.items() if not x.get("carry_allowed")]
        self.audit.write("day_start", now, previous_day=prev, carried_positions=carried, stray_positions=stray,
                         halted=self.state.halted, level=logging.CRITICAL if stray else logging.INFO)

    def _halt(self, now: datetime, reason: str) -> None:
        self.state.halted = reason
        self.audit.write("halt_new_entries", now, level=logging.CRITICAL, reason=reason)

    def _drain_notes(self, strat, now: datetime) -> None:
        for n in strat.notes:
            self.audit.write("decision", now, **n)
        strat.notes.clear()

    def _audit_order(self, now: datetime, intent: OrderIntent, res: ExecutionResult) -> None:
        self.audit.write("order", now, level=logging.INFO if res.ok else logging.ERROR,
                         intent_id=intent.intent_id, action=intent.action.value, position_id=intent.position_id.split(":", 1)[-1],
                         tag=intent.meta.get("tag"), broker=self.broker.name, live_orders=self.broker.live,
                         ok=res.ok, message=res.message, uncertain=res.uncertain, plan=res.plan,
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
