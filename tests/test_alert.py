import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import alert
import calc
from config import Settings

TODAY = date(2026, 10, 7)


def make_report(rows):
    return calc.build_report(pd.DataFrame(rows), None, None, None, Settings(), TODAY)


def hop(asin, fulfil, u7=14, u30=60, store="S1"):
    return {
        "ASIN": asin, "Store name": store, "FBA fulfillable quantity": fulfil,
        "FBA inbound receiving quantity": 0, "FBA inbound shipped quantity": 0, "FBA inbound working quantity": 0,
        "Units shipped last 7 days": u7, "Units shipped last 30 days": u30,
    }


def test_first_run_everything_is_new_then_nothing():
    rep = make_report([hop("B0OUT00001", 0), hop("B0FINE0001", 5000, 7, 30)])
    problem, new, state = alert.select_alerts(rep, {})
    assert list(problem["asin"]) == ["B0OUT00001"] and len(new) == 1
    problem2, new2, _ = alert.select_alerts(rep, state)
    assert len(problem2) == 1 and new2.empty  # на следующий день тот же ASIN — не новый


def test_worsened_status_alerts_again():
    rep = make_report([hop("B0AAAAAAA1", 0)])  # OUT = rank 0
    previous = {"B0AAAAAAA1|S1": calc.STATUS_RANK["URGENT"]}  # вчера был URGENT
    _, new, _ = alert.select_alerts(rep, previous)
    assert len(new) == 1


def test_improved_status_does_not_alert():
    rep = make_report([hop("B0AAAAAAA1", 12, 14, 60)])  # ~6 дней запаса: нигде не успеть → CRITICAL (1)
    previous = {"B0AAAAAAA1|S1": calc.STATUS_RANK["OUT"]}  # вчера был хуже (0)
    problem, new, _ = alert.select_alerts(rep, previous)
    assert len(problem) == 1 and new.empty


def test_min_status_widens_list():
    # 16 дней запаса при 2/день, наличие на AWD неизвестно → AWD (7 дн.) успевает → PLAN
    rep = make_report([hop("B0AAAAAAA1", 32, 14, 60)])
    assert rep.iloc[0]["status"] == "PLAN"
    assert alert.select_alerts(rep, {}, "URGENT")[0].empty
    assert alert.select_alerts(rep, {}, "ACTION")[0].empty
    assert len(alert.select_alerts(rep, {}, "PLAN")[0]) == 1


def test_message_contents_and_limit():
    rows = [hop(f"B0{i:08d}", 0, 14, 60 + i) for i in range(200)]
    rep = make_report(rows)
    problem, new, _ = alert.select_alerts(rep, {})
    text = alert.build_message(problem, new, top=15, dashboard_url="https://example.com/?a=1&b=2", today=TODAY)
    assert "07.10.2026" in text and "Проблемных ASIN: 200" in text
    assert "и ещё 185 в дашборде" in text
    assert len(text) <= alert.TG_LIMIT
    assert "&amp;" in text  # ссылка экранирована для HTML-режима Telegram


def test_message_escapes_html_in_names():
    rep = make_report([hop("B0AAAAAAA1", 0)])
    rep["parent_group"] = "A <b> & B"
    problem, new, _ = alert.select_alerts(rep, {})
    text = alert.build_message(problem, new, today=TODAY)
    assert "A &lt;b&gt; &amp; B" in text


def test_state_roundtrip_and_corrupt_file(tmp_path):
    p = tmp_path / "s.json"
    assert alert.load_state(p) == {}
    alert.save_state({"a|b": 1}, p)
    assert alert.load_state(p) == {"a|b": 1}
    p.write_text("{битый json", encoding="utf-8")
    assert alert.load_state(p) == {}


def test_main_dry_run_does_not_touch_state(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(Path(__file__).resolve().parent.parent)
    monkeypatch.setattr(alert, "STATE_FILE", tmp_path / "state.json")
    assert alert.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "FBA США" in out and "dry-run" in out
    assert not (tmp_path / "state.json").exists()


def test_main_without_telegram_keeps_state_unsaved(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(Path(__file__).resolve().parent.parent)
    monkeypatch.setattr(alert, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert alert.main([]) == 2
    assert not (tmp_path / "state.json").exists()  # алерт не должен потеряться


def test_main_sends_and_then_stays_quiet(tmp_path, monkeypatch):
    monkeypatch.chdir(Path(__file__).resolve().parent.parent)
    monkeypatch.setattr(alert, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setattr(alert.tg_subscribers, "sync", lambda token: {"chats": {"1": "g"}, "offset": 0})
    sent = []
    monkeypatch.setattr(alert, "send_telegram", lambda text, token, chat: sent.append(text))
    assert alert.main([]) == 0 and len(sent) == 1
    assert alert.main([]) == 0 and len(sent) == 1  # второй запуск: новых нет
    assert alert.main(["--always"]) == 0 and len(sent) == 2
