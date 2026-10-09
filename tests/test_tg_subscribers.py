import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tg_subscribers as ts


def upd(i, cid, text, **chat):
    return {"update_id": i, "message": {"text": text, "chat": {"id": cid, **chat}}}


def test_start_stop_and_offset():
    d = {"chats": {}, "offset": 0}
    w = ts.apply_updates(d, [upd(10, 5, "/start", first_name="Maxi"), upd(11, -100, "/start@maximum_fba_alert_bot", title="FBA"),
                             upd(12, 7, "привет"), upd(13, 5, "/start")])
    assert d["chats"] == {"5": "Maxi", "-100": "FBA"} and d["offset"] == 14 and w == ["5", "-100"]
    ts.apply_updates(d, [upd(14, 5, "/stop")])
    assert list(d["chats"]) == ["-100"] and d["offset"] == 15


def test_save_load_and_recipients(tmp_path, monkeypatch):
    p = tmp_path / "s.json"
    ts.save({"chats": {"1": "a"}, "offset": 3}, p)
    d = ts.load(p)
    assert d["offset"] == 3 and ts.load(tmp_path / "none.json") == {"chats": {}, "offset": 0}
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1, 2")
    assert ts.recipients(d) == ["1", "2"]
