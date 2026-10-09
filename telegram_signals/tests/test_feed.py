from __future__ import annotations

from datetime import datetime, timezone

from telegram_signals.config import TelegramConfig
from telegram_signals.feed import SignalFeed

T = datetime(2026, 10, 8, 5, 22, tzinfo=timezone.utc)


def feed(tmp_path, **env):
    cfg = TelegramConfig.from_env(env_file=None, environ={"TELEGRAM_DB": str(tmp_path / "m.sqlite"),
                                                          "TELEGRAM_SESSION": str(tmp_path / "s.session"), **env})
    return SignalFeed(cfg)


def test_feed_stays_off_with_a_reason_when_not_set_up(tmp_path):
    f = feed(tmp_path)
    f.start()
    assert f.status["state"] == "off" and "TELEGRAM_API_ID" in f.status["detail"]
    f = feed(tmp_path, TELEGRAM_API_ID="1", TELEGRAM_API_HASH="h")
    f.start()
    assert f.status["state"] == "off" and "login" in f.status["detail"]


def test_view_builds_signals_and_hides_ticks(tmp_path):
    f = feed(tmp_path)
    f.store.upsert(1, 1449, T, "🟢 BUY NIFTY 22500 PE\n💰 Entry : ₹155 - ₹159\n📊 Intraday Trade")
    f.store.upsert(1, 1450, T, "🎯 TP 1: ₹174\n🎯 TP 2: ₹189\n🎯 TP 3: ₹209\n🛑 Stop Loss: ₹140", reply_to=1449)
    f.store.upsert(1, 1451, T, "₹163 🔥🔥🔥🔥🔥", reply_to=1449)
    v = f.view()
    assert [s["id"] for s in v["signals"]] == [1449] and v["signals"][0]["direction"] == "BEARISH"
    assert [m["kind"] for m in v["messages"]] == ["DETAILS", "SIGNAL"]          # newest first, no ticks
    assert "TICK" in [m["kind"] for m in f.view(show_ticks=True)["messages"]]
