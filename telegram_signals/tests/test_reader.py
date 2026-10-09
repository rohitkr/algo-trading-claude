from __future__ import annotations

from datetime import datetime, timezone

import pytest

from telegram_signals.config import TelegramConfig
from telegram_signals.reader import pick_channel
from telegram_signals.store import MessageStore

DIALOGS = [(1, "Nifty Sensex VIP setups", None), (2, "Nifty Options Free", "niftyfree"), (3, "Family", None)]


def test_channel_found_by_exact_title_any_case_or_username():
    assert pick_channel(DIALOGS, "nifty  sensex vip SETUPS")[0] == 1
    assert pick_channel(DIALOGS, "@NiftyFree")[0] == 2


def test_no_guessing_when_not_found_or_ambiguous():
    with pytest.raises(ValueError, match="similar"):
        pick_channel(DIALOGS, "Nifty VIP")
    with pytest.raises(ValueError, match="@username"):
        pick_channel(DIALOGS + [(4, "Nifty Sensex VIP setups", "x")], "Nifty Sensex VIP setups")


def test_store_keeps_raw_text_and_tracks_edits(tmp_path):
    s = MessageStore(tmp_path / "m.sqlite")
    t = datetime(2026, 10, 8, 4, 0, tzinfo=timezone.utc)
    assert s.upsert(1, 10, t, "BUY NIFTY 22500 CE @120")
    assert not s.upsert(1, 10, t, "BUY NIFTY 22500 CE @120")             # same again: nothing changed
    assert s.upsert(1, 10, t, "BUY NIFTY 22500 CE @118", edit_date=t)     # edited
    assert s.upsert(1, 11, t, "SL hit", reply_to=10)
    rows = s.recent(10)
    assert [r["msg_id"] for r in rows] == [11, 10] and rows[1]["text"].endswith("@118") and rows[0]["reply_to"] == 10


def test_config_needs_keys_and_defaults_to_the_channel(tmp_path):
    cfg = TelegramConfig.from_env(env_file=None, environ={})
    assert cfg.channel == "Nifty Sensex VIP setups"
    with pytest.raises(ValueError, match="TELEGRAM_API_ID"):
        cfg.require_api()
    assert TelegramConfig.from_env(env_file=None, environ={"TELEGRAM_API_ID": "123", "TELEGRAM_API_HASH": "h"}).api_id == 123
