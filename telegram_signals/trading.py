"""Phase 3: turn a Telegram signal into YOUR trade. The tip only gives a direction (BUY CE / SELL PE = bullish,
BUY PE / SELL CE = bearish); what to trade is set by the rules below (saved in the Telegram DB, edited on /telegram):

  action "sell_opposite"  bullish -> SELL PE, bearish -> SELL CE   (strike: the tip's strike -/+ offset strikes)
         "credit_spread"  bullish -> bull put spread (SELL PE + BUY a PE `width` strikes lower),
                          bearish -> bear call spread (SELL CE + BUY a CE `width` strikes higher)
         "follow"         the tip as given (BUY the same option)

Modes: off (nothing), alert (log what would be placed), approve (Place / Skip on the page, expires), auto (place).
Every trade goes through the trader's StrategyService.create_and_trade (the Strategy Builder engine: risk checks,
BUY legs first, all-or-nothing), so nothing here talks to the broker directly.

Safety gates, before anything is placed: the signal must be newer than `max_signal_age_s` (so the messages caught up
at startup can never trigger a trade), its index allowed, the time inside the window, today's placed count under
`max_trades_per_day`, and each signal is acted on at most once.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .parser import SL_HIT, build_signals

log = logging.getLogger("telegram_signals")
IST = ZoneInfo("Asia/Kolkata")
MODES = ("off", "alert", "approve", "auto")
ACTIONS = ("sell_opposite", "credit_spread", "follow")


@dataclass
class SignalRules:
    mode: str = "alert"
    indices: list = field(default_factory=lambda: ["NIFTY", "SENSEX"])
    action: str = "sell_opposite"
    strike_offset: int = 0                 # strikes away from the tip's strike, out of the money for what we sell
    spread_width: int = 4                  # credit spread: strikes between the sold and the bought (hedge) leg
    lots: dict = field(default_factory=lambda: {"NIFTY": 1, "SENSEX": 1})
    leg_sl_points: float | None = None     # SL on the sold leg, premium points (blank = the builder's automatic wide SL)
    max_loss: float | None = None          # combined, rupees (blank = off)
    max_profit: float | None = None
    order_type: str = "MIS"
    start: str = "09:20"
    end: str = "14:45"
    max_trades_per_day: int = 3
    max_signal_age_s: int = 120
    approve_timeout_s: int = 120
    exit_on_channel_sl: bool = True        # their "STOP LOSS HIT" squares off our trade for that signal

    @classmethod
    def from_json(cls, d: dict | None) -> "SignalRules":
        base = asdict(cls())
        base.update({k: v for k, v in (d or {}).items() if k in base})
        r = cls(**base)
        r.validate()
        return r

    def validate(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if self.action not in ACTIONS:
            raise ValueError(f"action must be one of {ACTIONS}")
        if self.order_type not in ("MIS", "CNC", "BTST"):
            raise ValueError("order type must be MIS, CNC or BTST")
        for k in ("start", "end"):
            time.fromisoformat(getattr(self, k))
        if self.spread_width < 1 or self.strike_offset < 0 or self.max_trades_per_day < 0:
            raise ValueError("spread width >= 1, strike offset >= 0, max trades/day >= 0")
        if any(int(v) < 1 for v in self.lots.values()):
            raise ValueError("lots must be 1 or more")


def _step(strikes: list[float], near: float) -> float:
    s = sorted(strikes)
    i = min(range(len(s)), key=lambda k: abs(s[k] - near))
    diffs = [b - a for a, b in zip(s[max(0, i - 3):i + 3], s[max(1, i - 2):i + 4]) if b > a]
    return min(diffs) if diffs else 50.0


class SignalTrader:
    def __init__(self, store, strategies, svc):
        self.store, self.strategies, self.svc = store, strategies, svc
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- rules ------------------------------------------------------------------------------------------
    def rules(self) -> SignalRules:
        try:
            return SignalRules.from_json(self.store.get_setting("rules"))
        except Exception:
            return SignalRules()

    def save_rules(self, d: dict) -> SignalRules:
        r = SignalRules.from_json({**asdict(self.rules()), **d})
        self.store.set_setting("rules", asdict(r))
        return r

    # -- signal -> legs ---------------------------------------------------------------------------------
    def plan(self, s: dict, rules: SignalRules | None = None) -> dict:
        """The trade for a signal under the rules: {"legs": [...], "config": {...}, "summary": "..."}."""
        r = rules or self.rules()
        idx = s["index"]
        exps = self.svc.instruments.expiries(idx)
        if not exps:
            raise ValueError(f"no listed {idx} expiry")
        expiry: date = exps[0]                                   # the tip never names one: nearest weekly
        step = _step(self.svc.instruments.strikes(idx, expiry), s["strike"])
        base, bull = float(s["strike"]), s["direction"] == "BULLISH"
        lots = int(r.lots.get(idx, 1))
        leg = lambda side, typ, k: {"underlying": idx, "expiry": expiry.isoformat(), "strike": k, "option_type": typ,  # noqa: E731
                                    "side": side, "lots": lots, "price_type": "MARKET", "entry_price": None,
                                    "sl_value": None, "sl_type": "POINTS", "tp_value": None, "tp_type": "POINTS"}
        if r.action == "follow":
            legs = [leg(s["action"], s["option_type"], base)]
        else:
            typ = "PE" if bull else "CE"
            sold = base - r.strike_offset * step if bull else base + r.strike_offset * step
            legs = [leg("SELL", typ, sold)]
            if r.leg_sl_points:
                legs[0].update(sl_value=float(r.leg_sl_points))
            if r.action == "credit_spread":
                hedge = sold - r.spread_width * step if bull else sold + r.spread_width * step
                legs.insert(0, leg("BUY", typ, hedge))            # hedge first (the engine also sends BUY first)
        config = {"name": f"TG #{s['id']} {'▲' if bull else '▼'} {idx}", "order_type": r.order_type,
                  "exit_loss_amount": r.max_loss, "exit_profit_amount": r.max_profit}
        summary = " + ".join(f"{l['side']} {idx} {int(l['strike'])} {l['option_type']} ×{lots}" for l in legs)
        return {"legs": legs, "config": config, "summary": f"{summary} · {expiry:%d %b}"}

    # -- the loop -----------------------------------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="telegram-trader", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(2):
            try:
                self.tick()
            except Exception:                                    # never let it die; never touch the trader
                log.exception("telegram trader tick failed")

    def _now(self) -> datetime:
        return self.svc.clock()                                  # naive IST, like the trader

    def _ist(self, iso: str) -> datetime:
        return datetime.fromisoformat(iso).astimezone(IST).replace(tzinfo=None)

    def tick(self) -> None:
        with self._lock:
            r = self.rules()
            signals, messages = build_signals(self.store.recent(400))
            now = self._now()
            for s in sorted(signals.values(), key=lambda x: x["date"]):
                a = self.store.action(s["id"])
                if a is None:
                    self._new_signal(s, r, now)
                elif a["status"] == "PROPOSED" and (now - self._ist(a["created_at"])).total_seconds() > r.approve_timeout_s:
                    self.store.save_action(s["id"], status="EXPIRED", reason=f"not approved within {r.approve_timeout_s}s")
            if r.exit_on_channel_sl:
                for m in messages:
                    if m["kind"] == SL_HIT and m["signal_id"]:
                        self._channel_sl(m["signal_id"])

    def _new_signal(self, s: dict, r: SignalRules, now: datetime) -> None:
        age = (now - self._ist(s["date"])).total_seconds()
        if age > r.max_signal_age_s:
            return                                               # history / catch-up: never acted on, not logged
        mode = r.mode
        if mode == "off":
            self.store.save_action(s["id"], mode=mode, status="SKIPPED", reason="trading is off")
            return
        why = self._gate(s, r, now)
        try:
            plan = self.plan(s, r)
        except Exception as exc:                                 # noqa: BLE001
            self.store.save_action(s["id"], mode=mode, status="SKIPPED", reason=f"no plan: {exc}")
            return
        if why:
            self.store.save_action(s["id"], mode=mode, status="SKIPPED", reason=why, plan=plan)
        elif mode == "alert":
            self.store.save_action(s["id"], mode=mode, status="ALERT", plan=plan, reason="alert mode: not placed")
        elif mode == "approve":
            self.store.save_action(s["id"], mode=mode, status="PROPOSED", plan=plan)
        else:
            self.store.save_action(s["id"], mode=mode, status="PROPOSED", plan=plan)
            self._place(s["id"], plan)

    def _gate(self, s: dict, r: SignalRules, now: datetime) -> str | None:
        if s["index"] not in r.indices:
            return f"{s['index']} is not in your list"
        t = now.time()
        if not (time.fromisoformat(r.start) <= t <= time.fromisoformat(r.end)):
            return f"outside your time window {r.start}-{r.end}"
        day_start_utc = datetime.combine(now.date(), time(0), IST).astimezone(timezone.utc).isoformat(timespec="seconds")
        if self.store.placed_on(day_start_utc) >= r.max_trades_per_day:
            return f"max {r.max_trades_per_day} trades a day reached"
        return None

    def _place(self, signal_id: int, plan: dict) -> dict:
        res = self.strategies.create_and_trade({"config": plan["config"], "legs": plan["legs"]})
        if res.get("ok"):
            note = f"{len(res.get('failed') or [])} leg(s) not placed" if res.get("failed") else None
            return self.store.save_action(signal_id, status="PLACED", strategy_id=res["strategy_id"], reason=note)
        return self.store.save_action(signal_id, status="FAILED", reason="; ".join(res.get("errors") or ["not placed"]))

    def _channel_sl(self, signal_id: int) -> None:
        a = self.store.action(signal_id)
        if not a or a["status"] != "PLACED" or a["channel_exit"] or not a["strategy_id"]:
            return
        st = self.strategies.repo.strategy(a["strategy_id"])
        self.store.save_action(signal_id, channel_exit=1)
        if st and st["status"] == "ACTIVE":
            self.strategies.exit_all(a["strategy_id"])
            self.store.save_action(signal_id, reason="their SL hit: squared off")

    # -- page actions -------------------------------------------------------------------------------------
    def approve(self, signal_id: int) -> dict:
        with self._lock:
            a = self.store.action(signal_id)
            if not a or a["status"] != "PROPOSED":
                raise ValueError("this signal is not waiting for approval")
            r = self.rules()
            if (self._now() - self._ist(a["created_at"])).total_seconds() > r.approve_timeout_s:
                self.store.save_action(signal_id, status="EXPIRED", reason="approved too late")
                raise ValueError(f"too late: approval window is {r.approve_timeout_s}s")
            return self._place(signal_id, a["plan"])

    def skip(self, signal_id: int) -> dict:
        with self._lock:
            a = self.store.action(signal_id)
            if not a or a["status"] != "PROPOSED":
                raise ValueError("this signal is not waiting for approval")
            return self.store.save_action(signal_id, status="SKIPPED", reason="skipped by you")
