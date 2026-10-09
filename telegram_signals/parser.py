"""Turn the channel's messages into meaning. Pure functions, no I/O: the raw text in the store is never changed, so
history can always be re-parsed with a better parser.

The channel ("Nifty Sensex VIP setups", studied on 500 messages, 7 Sep - 8 Oct 2026) posts:
  * a SIGNAL as two messages: a header  "🟢 BUY NIFTY 22450 CE / 💰 Entry : ₹150 - ₹154 / 📊 Intraday Trade"
    and, as a reply to it, the details  "🎯 TP 1: ₹169 / TP 2 / TP 3 / 🛑 Stop Loss: ₹135 / 📝 Rationale / ⏳ Valid for"
  * UPDATES as replies to the header: "🎯 Target 1 done / 💹 Ltp ₹174", "🛑 STOP LOSS HIT | NIFTY 22450 CE / Live LTP",
    price ticks "₹163 🔥🔥🔥" (most of the traffic) and a screenshot after target 3
  * rare free text: trailing / booking / "EXIT COMPLETELY" advice (ADVISORY - shown to you, never acted on alone),
    greetings, promos and call notices (NOISE).
Anything that looks trade-related but fits none of these is UNCLEAR: shown, never traded.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

INDICES = ("NIFTY", "SENSEX", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "BANKEX")
_NUM = r"([\d,]+(?:\.\d+)?)"

SIGNAL_RE = re.compile(rf"\b(BUY|SELL)\s+({'|'.join(INDICES)})\s+(\d{{4,6}})\s*(CE|PE)\b", re.I)
SYMBOL_RE = re.compile(rf"\b({'|'.join(INDICES)})\s+(\d{{4,6}})\s*(CE|PE)\b", re.I)
ENTRY_RE = re.compile(rf"Entry\s*:?\s*₹?\s*{_NUM}(?:\s*(?:-|–|to)\s*₹?\s*{_NUM})?", re.I)
TP_RE = re.compile(rf"\bTP\s*(\d)\s*:?\s*₹?\s*{_NUM}", re.I)
SL_RE = re.compile(rf"Stop\s*Loss\s*:?\s*₹?\s*{_NUM}", re.I)
RATIONALE_RE = re.compile(r"Rationale\s*:\s*([^\n|]+)", re.I)
VALID_RE = re.compile(r"Valid\s*for\s*:\s*([^\n|]+)", re.I)
TARGET_DONE_RE = re.compile(r"Target\s*(\d)\s*(?:done|hit|achieved)", re.I)
SL_HIT_RE = re.compile(r"STOP\s*LOSS\s*HIT|\bSL\s*HIT\b", re.I)
LTP_RE = re.compile(rf"\bLtp\s*:?\s*₹?\s*{_NUM}", re.I)
TICK_RE = re.compile(rf"^\s*₹?\s*{_NUM}\s*(?:🔥\s*)+$")
EXIT_RE = re.compile(r"\bexit\b|\bbook\s+all\b|\bsquare\s*off\b", re.I)
TRAIL_RE = re.compile(rf"\btrail\w*\s+sl\b(?:\s+(?:near|at|to))?\s*{_NUM}?", re.I)
BOOK_RE = re.compile(rf"\b(?:book|booking)\b.*?(?:near|at)\s*{_NUM}", re.I)
TRADEY_RE = re.compile(r"\b(ce|pe|sl|stop|target|tgt|entry|buy|sell|strike|premium|exit|book)\b", re.I)

# kinds
SIGNAL, DETAILS, TARGET, SL_HIT, TICK, MEDIA, ADVISORY, NOISE, UNCLEAR = (
    "SIGNAL", "DETAILS", "TARGET", "SL_HIT", "TICK", "MEDIA", "ADVISORY", "NOISE", "UNCLEAR")


def _f(s: str | None) -> float | None:
    return float(s.replace(",", "")) if s else None


@dataclass
class Parsed:
    kind: str
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def parse(text: str, *, reply_to: int | None = None, has_media: bool = False) -> Parsed:
    t = (text or "").strip()
    if not t:
        return Parsed(MEDIA if has_media else NOISE)

    m = SIGNAL_RE.search(t)
    if m and ENTRY_RE.search(t):
        e = ENTRY_RE.search(t)
        lo, hi = _f(e.group(1)), _f(e.group(2)) or _f(e.group(1))
        side, idx, strike, typ = m.group(1).upper(), m.group(2).upper(), int(m.group(3)), m.group(4).upper()
        d = {"action": side, "index": idx, "strike": strike, "option_type": typ,
             "entry_low": min(lo, hi), "entry_high": max(lo, hi),
             # the tip's own trade: BUY CE / SELL PE = bullish, BUY PE / SELL CE = bearish (we use only this)
             "direction": "BULLISH" if (side == "BUY") == (typ == "CE") else "BEARISH",
             "intraday": bool(re.search(r"intraday", t, re.I))}
        d.update(_details(t))                                  # some channels put TP/SL in the same message
        return Parsed(SIGNAL, d)

    if SL_RE.search(t) and TP_RE.search(t):
        return Parsed(DETAILS, _details(t))

    m = TARGET_DONE_RE.search(t)
    if m:
        ltp = LTP_RE.search(t)
        return Parsed(TARGET, {"target": int(m.group(1)), "ltp": _f(ltp.group(1)) if ltp else None})

    if SL_HIT_RE.search(t):
        ltp = LTP_RE.search(t)
        s = SYMBOL_RE.search(t)
        return Parsed(SL_HIT, {"ltp": _f(ltp.group(1)) if ltp else None,
                               "symbol": f"{s.group(1).upper()} {s.group(2)} {s.group(3).upper()}" if s else None})

    m = TICK_RE.match(t)
    if m:
        return Parsed(TICK, {"price": _f(m.group(1))})

    if EXIT_RE.search(t) or TRAIL_RE.search(t) or BOOK_RE.search(t):
        d = {"exit": bool(EXIT_RE.search(t))}
        tr = TRAIL_RE.search(t)
        if tr and tr.group(1):
            d["trail_sl"] = _f(tr.group(1))
        bk = BOOK_RE.search(t)
        if bk:
            d["book_near"] = _f(bk.group(1))
        return Parsed(ADVISORY, d)

    if TRADEY_RE.search(t):
        return Parsed(UNCLEAR)
    return Parsed(NOISE)


def _details(t: str) -> dict:
    d: dict = {}
    tps = {int(n): _f(v) for n, v in TP_RE.findall(t)}
    if tps:
        d["targets"] = [tps[k] for k in sorted(tps)]
    sl = SL_RE.search(t)
    if sl:
        d["stop_loss"] = _f(sl.group(1))
    r = RATIONALE_RE.search(t)
    if r:
        d["rationale"] = r.group(1).strip()
    v = VALID_RE.search(t)
    if v:
        d["valid_for"] = v.group(1).strip()
    return d


# -- signals: a header + its details + every update replying to it ----------------------------------------------
STATUS_ORDER = ("OPEN", "T1", "T2", "T3", "SL_HIT")


def build_signals(messages: list[dict]) -> tuple[dict[int, dict], list[dict]]:
    """messages: store rows (msg_id, date, text, reply_to, has_media), any order. Returns ({signal msg_id: signal},
    [every message with its parse and the signal it belongs to]). A signal's status: OPEN -> T1/T2/T3 (highest target
    reported) or SL_HIT; ticks only update its last price."""
    rows = sorted(messages, key=lambda r: (r["date"], r["msg_id"]))
    signals: dict[int, dict] = {}
    out = []
    latest_signal: int | None = None
    for r in rows:
        p = parse(r["text"], reply_to=r.get("reply_to"), has_media=bool(r.get("has_media")))
        sid = None
        if p.kind == SIGNAL:
            sid = r["msg_id"]
            signals[sid] = {"id": sid, "date": r["date"], **p.data, "status": "OPEN", "last_price": None,
                            "targets_done": [], "updates": 0, "complete": "stop_loss" in p.data}
            latest_signal = sid
        elif r.get("reply_to") in signals:
            sid = r["reply_to"]
        elif p.kind in (ADVISORY, SL_HIT, TARGET) and latest_signal is not None:
            sid = latest_signal                               # not a reply: refers to the latest signal
        s = signals.get(sid) if sid else None
        if s is not None and p.kind != SIGNAL:
            s["updates"] += 1
            if p.kind == DETAILS:
                s.update({k: v for k, v in p.data.items()})
                s["complete"] = "stop_loss" in s
            elif p.kind == TARGET:
                s["targets_done"] = sorted(set(s["targets_done"]) | {p.data["target"]})
                if s["status"] != "SL_HIT":
                    s["status"] = f"T{max(s['targets_done'])}"
                s["last_price"] = p.data.get("ltp") or s["last_price"]
            elif p.kind == SL_HIT:
                s["status"] = "SL_HIT"
                s["last_price"] = p.data.get("ltp") or s["last_price"]
            elif p.kind == TICK:
                s["last_price"] = p.data["price"]
        out.append({**r, "kind": p.kind, "parsed": p.data, "signal_id": sid})
    return signals, out
