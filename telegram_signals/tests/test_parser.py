"""The channel's message shapes, as seen in 500 real messages (7 Sep - 8 Oct 2026)."""
from __future__ import annotations

from telegram_signals.parser import build_signals, parse

HEADER = "🟢 BUY NIFTY 22450 CE\n💰 Entry : ₹150 - ₹154\n📊 Intraday Trade ⭐ ⭐"
DETAILS = ("🎯 TP 1: ₹169\n🎯 TP 2: ₹184\n🎯 TP 3: ₹204\n🛑 Stop Loss: ₹135\n📝 Rationale: Reversal\n"
           "⏳ Valid for: Intraday Only\n⚠️ Terms, Conditions, Disclaimer: Read everything here")


def test_signal_header():
    p = parse(HEADER)
    assert p.kind == "SIGNAL"
    assert p.data == {"action": "BUY", "index": "NIFTY", "strike": 22450, "option_type": "CE", "entry_low": 150.0,
                      "entry_high": 154.0, "direction": "BULLISH", "intraday": True}
    assert parse("🟢 BUY SENSEX 72900 PE\n💰 Entry : ₹311 - ₹315").data["direction"] == "BEARISH"


def test_details_and_updates():
    d = parse(DETAILS)
    assert d.kind == "DETAILS" and d.data["targets"] == [169, 184, 204] and d.data["stop_loss"] == 135
    assert d.data["rationale"] == "Reversal" and d.data["valid_for"] == "Intraday Only"
    t = parse("🎯 Target 2 done 🔥🔥🔥🔥🔥\n💹 Ltp ₹190\n✅ Book Major quantity here")
    assert (t.kind, t.data) == ("TARGET", {"target": 2, "ltp": 190.0})
    s = parse("🛑 STOP LOSS HIT | NIFTY 22450 CE\n💹 Live LTP: ₹135\n❌ Position closed. Capital preserved.")
    assert (s.kind, s.data) == ("SL_HIT", {"ltp": 135.0, "symbol": "NIFTY 22450 CE"})
    assert parse("₹163 🔥🔥🔥🔥🔥").data == {"price": 163.0} and parse("145 🔥🔥🔥").kind == "TICK"
    assert parse("", has_media=True).kind == "MEDIA"


def test_advice_noise_and_unclear_are_never_signals():
    assert parse("EXIT COMPLETELY \nBOOK ALL PROFITS").data == {"exit": True}
    a = parse("book small profit near 149 and trail sl near 143")
    assert a.kind == "ADVISORY" and a.data == {"exit": False, "trail_sl": 143.0, "book_near": 149.0}
    assert parse("Good morning traders").kind == "NOISE"
    assert parse("We are live pls join now").kind == "NOISE"
    assert parse("keep 74400 ce and 74200 pe for jodi \nstrangle buy near 215 combinedly").kind == "UNCLEAR"


def test_build_signals_links_updates_and_tracks_status():
    rows = [dict(msg_id=1, date="2026-10-08T04:45:00", text=HEADER, reply_to=None, has_media=0),
            dict(msg_id=2, date="2026-10-08T04:45:05", text=DETAILS, reply_to=1, has_media=0),
            dict(msg_id=3, date="2026-10-08T04:46:00", text="₹157 🔥🔥🔥", reply_to=1, has_media=0),
            dict(msg_id=4, date="2026-10-08T04:50:00", text="🎯 Target 1 done\n💹 Ltp ₹170", reply_to=1, has_media=0),
            dict(msg_id=5, date="2026-10-08T05:00:00", text="trail sl near 160", reply_to=None, has_media=0),
            dict(msg_id=6, date="2026-10-08T05:10:00", text="🛑 STOP LOSS HIT | NIFTY 22450 CE\n💹 Live LTP: ₹160",
                 reply_to=1, has_media=0)]
    sigs, out = build_signals(rows)
    s = sigs[1]
    assert s["complete"] and s["stop_loss"] == 135 and s["targets"] == [169, 184, 204]
    assert s["status"] == "SL_HIT" and s["targets_done"] == [1] and s["last_price"] == 160
    assert [o["signal_id"] for o in out] == [1] * 6
