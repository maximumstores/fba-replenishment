"""Подписка на бота без chat_id в секретах: кто нажал /start — получает рассылку, /stop — отписался.

Список хранится в state/subscribers.json (коммитится вместе с состоянием алертов). Перед каждой рассылкой
sync() читает новые сообщения боту (getUpdates) и обновляет список; работает и в личке, и в группе
(в группе писать /start@maximum_fba_alert_bot).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

SUBS_FILE = Path("state/subscribers.json")


def load(path: Path | None = None) -> dict:
    p = Path(path or SUBS_FILE)
    if p.exists():
        d = json.loads(p.read_text(encoding="utf-8"))
        d.setdefault("chats", {})
        d.setdefault("offset", 0)
        return d
    return {"chats": {}, "offset": 0}


def save(d: dict, path: Path | None = None) -> None:
    p = Path(path or SUBS_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def apply_updates(d: dict, updates: list[dict]) -> list[str]:
    """Применяет /start и /stop; возвращает chat_id, которым надо ответить «подписал»."""
    welcome: list[str] = []
    for u in updates:
        d["offset"] = max(d["offset"], int(u["update_id"]) + 1)
        m = u.get("message") or u.get("channel_post") or {}
        text = (m.get("text") or "").strip().lower()
        chat = m.get("chat") or {}
        if not chat or not text.startswith("/"):
            continue
        cid = str(chat["id"])
        cmd = text.split()[0].split("@")[0]
        if cmd == "/start":
            title = chat.get("title") or chat.get("username") or chat.get("first_name") or cid
            if cid not in d["chats"]:
                welcome.append(cid)
            d["chats"][cid] = d["chats"].get(cid) if cid in d["chats"] and title == cid else str(title)
        elif cmd == "/stop":
            d["chats"].pop(cid, None)
    return welcome


def sync(token: str, path: Path | None = None) -> dict:
    import requests

    d = load(path)
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                     params={"offset": d["offset"], "timeout": 0, "allowed_updates": json.dumps(["message", "channel_post"])},
                     timeout=30)
    if not r.ok:
        raise RuntimeError(f"Telegram getUpdates {r.status_code}: {r.text[:300]}")
    for cid in apply_updates(d, r.json().get("result", [])):
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": cid, "text": "Подписал. Сюда будут приходить алерты по FBA-аутам. Отписаться: /stop"}, timeout=30)
    save(d, path)
    return d


def recipients(d: dict) -> list[str]:
    """Подписчики + необязательный TELEGRAM_CHAT_ID (можно несколько через запятую)."""
    extra = [c.strip() for c in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
    return list(dict.fromkeys([*d["chats"], *extra]))
