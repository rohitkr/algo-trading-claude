"""Pre-trade risk checks and position sizing. Every limit comes from EngineConfig.

ENTRY signals must pass every check; the result lists each check (passed or
failed) for the audit log. EXIT signals are never blocked by risk limits (stop
losses and square-offs must always go out); only exact duplicates are refused.

Sizing: lots = min(strategy lots, RISK_MAX_LOTS_PER_TRADE, RISK_MAX_QTY_PER_TRADE / lot,
                   RISK_MAX_RISK_PER_TRADE / risk per lot,
                   usable margin / margin per lot)
where usable margin = min(RISK_CAPITAL, broker available margin) x RISK_MARGIN_UTILISATION_PCT.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .config import EngineConfig
from .interfaces import RiskCheck, RiskDecision, Signal


@dataclass
class RiskContext:
    """Engine state the checks need (filled by the engine for each entry)."""
    now: datetime
    session: tuple | None                 # (open, close) times today, None = not a trading day
    halted: str | None                    # reason new entries are halted (daily loss, reconciliation, ...)
    data_lag_s: float | None              # age of the newest underlying bar
    api_budget: int | None                # Breeze calls left today (None = unlimited)
    open_positions: int
    trades_today: int
    daily_pnl: float
    reentries_for_root: int
    signal_blocked: str | None            # MANUAL_EXIT / unmanaged: this signal must never trade again
    already_sent: bool
    contract_already_open: bool
    lot_size: int
    risk_per_unit: float | None           # estimated loss per unit at the stop / max loss when hedged
    margin_per_lot: float | None
    available_margin: float | None
    hedge: dict = field(default_factory=dict)


def estimate_risk_per_unit(signal: Signal, cfg: EngineConfig, wing_ref: float | None) -> tuple[float | None, dict]:
    """Loss per unit if the stop is hit (naked), capped by the defined max loss when hedged."""
    info: dict = {}
    stop = signal.stop or {}
    if stop.get("basis") == "spot" and signal.spot is not None:
        stop_risk = abs(signal.spot - stop["level"]) * cfg.positional_delta
    elif stop.get("basis") == "premium" and signal.ref_price:
        stop_risk = stop["level"] - signal.ref_price
    else:
        stop_risk = None
    info["stop_risk_per_unit"] = None if stop_risk is None else round(stop_risk, 2)
    if cfg.hedged and signal.ref_price is not None and wing_ref is not None:
        credit = signal.ref_price - wing_ref
        max_loss = max(0.0, cfg.hedge_width - credit)
        info.update(net_credit_per_unit=round(credit, 2), max_loss_per_unit=round(max_loss, 2))
        return (max_loss if stop_risk is None else min(stop_risk, max_loss)), info
    return stop_risk, info


class RiskManager:
    def __init__(self, cfg: EngineConfig):
        self.cfg = cfg

    def evaluate_entry(self, sig: Signal, rc: RiskContext) -> RiskDecision:
        c = self.cfg
        checks: list[RiskCheck] = []

        def check(name: str, ok: bool, detail: str = ""):
            checks.append(RiskCheck(name, bool(ok), detail))

        check("not_halted", rc.halted is None, rc.halted or "")
        in_session = rc.session is not None and rc.session[0] <= rc.now.time() <= rc.session[1]
        check("market_hours", in_session, f"now {rc.now:%H:%M:%S}, session {rc.session}")
        check("entry_window", c.entry_start_time <= rc.now.time() <= c.entry_end_time,
              f"entries only {c.entry_start_time:%H:%M}-{c.entry_end_time:%H:%M}")
        check("before_force_exit", not c.intraday_only or rc.now.time() < c.force_exit_time,
              f"force exit at {c.force_exit_time:%H:%M}")
        age = (rc.now - sig.ts).total_seconds() - 60           # the bar ended 1 minute after its stamp
        check("signal_fresh", age <= c.max_data_lag_s, f"signal bar ended {age:.0f}s ago (max {c.max_data_lag_s:.0f}s)")
        check("data_fresh", rc.data_lag_s is not None and rc.data_lag_s <= c.max_data_lag_s,
              f"newest bar {rc.data_lag_s if rc.data_lag_s is None else round(rc.data_lag_s)}s old")
        check("api_budget", rc.api_budget is None or rc.api_budget >= c.min_api_budget,
              f"{rc.api_budget} Breeze calls left (keep {c.min_api_budget} for exits)")
        check("daily_loss", not c.max_daily_loss_enabled or rc.daily_pnl > -c.max_daily_loss,
              f"today {rc.daily_pnl:,.0f} vs limit -{c.max_daily_loss:,.0f}" if c.max_daily_loss_enabled else "disabled")
        check("daily_profit", not c.max_daily_profit_enabled or rc.daily_pnl < c.max_daily_profit,
              f"today {rc.daily_pnl:,.0f} vs cap {c.max_daily_profit:,.0f}" if c.max_daily_profit_enabled else "disabled")
        check("signal_not_blocked", rc.signal_blocked is None, rc.signal_blocked or "")
        check("max_trades_per_day", rc.trades_today < c.max_trades_per_day, f"{rc.trades_today}/{c.max_trades_per_day}")
        check("max_open_positions", rc.open_positions < c.max_open_positions,
              f"{rc.open_positions}/{c.max_open_positions}")
        check("reentry_limit", not sig.is_reentry or rc.reentries_for_root < c.max_reentries,
              f"{rc.reentries_for_root}/{c.max_reentries} re-entries used")
        check("duplicate_intent", not rc.already_sent, "intent id already sent" if rc.already_sent else "")
        check("duplicate_contract", not rc.contract_already_open, "same contract already open")
        check("stop_loss_defined", bool(sig.stop.get("level")), str(sig.stop))

        # sizing
        lot = rc.lot_size
        limits = {"strategy": sig.lots, "max_lots": c.max_lots_per_trade, "max_qty": c.max_qty_per_trade // lot}
        risk_lot = rc.risk_per_unit * lot if rc.risk_per_unit is not None else None
        if risk_lot is not None and risk_lot > 0:
            limits["max_risk"] = int(c.max_risk_per_trade // risk_lot)
        check("risk_estimate_available", risk_lot is not None, "" if risk_lot is not None else "no reference price")
        usable = None
        if rc.available_margin is not None:
            usable = min(c.capital, rc.available_margin) * c.margin_utilisation_pct / 100
            if rc.margin_per_lot:
                limits["margin"] = int(usable // rc.margin_per_lot)
        check("margin_known", rc.margin_per_lot is not None and usable is not None,
              f"margin/lot {rc.margin_per_lot}, usable {usable}")
        lots = max(0, min(limits.values()))
        binding = min(limits, key=limits.get)
        check("position_size", lots >= 1, f"{lots} lots (limits {limits}; binding: {binding})")
        metrics = {"lots": lots, "qty": lots * lot, "limits": limits, "binding_limit": binding,
                   "risk_per_lot": None if risk_lot is None else round(risk_lot, 2),
                   "est_risk": None if risk_lot is None else round(risk_lot * lots, 2),
                   "margin_per_lot": rc.margin_per_lot, "est_margin": None if rc.margin_per_lot is None
                   else round(rc.margin_per_lot * lots, 2), "usable_margin": usable, **rc.hedge}
        ok = all(ch.passed for ch in checks)
        return RiskDecision(ok, lots if ok else 0, checks, metrics)

    def evaluate_exit(self, sig: Signal, already_sent: bool, has_position: bool) -> RiskDecision:
        checks = [RiskCheck("position_exists", has_position), RiskCheck("duplicate_intent", not already_sent)]
        return RiskDecision(all(c.passed for c in checks), 0, checks)
