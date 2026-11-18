"""Служебное сообщение о сбое: python notify.py "текст". Нужен TELEGRAM_BOT_TOKEN.
Шлёт ТОЛЬКО в чат из ALERT_CHAT (переменная репо ALERT_CHAT_ID), никогда подписчикам отчёта.
Если ALERT_CHAT не задан — ничего не шлёт, только пишет в лог."""
from __future__ import annotations

import os
import sys

import requests



def main() -> int:
    text = " ".join(sys.argv[1:]).strip()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not text or not token:
        print("нет текста или TELEGRAM_BOT_TOKEN")
        return 1
    chat_id = os.getenv("ALERT_CHAT", "").strip()
    if not chat_id:
        print("ALERT_CHAT не задан, сообщение о сбое не отправлено:\n" + text)
        return 0
    chats = [chat_id]
    for chat in chats:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True}, timeout=30)
        print(chat, r.status_code)
    print(f"отправлено {len(chats)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
