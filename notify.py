"""Служебное сообщение подписчикам бота: python notify.py "текст". Нужен TELEGRAM_BOT_TOKEN.
Используется, когда отчёт упал или не запустился (см. restock.yml, watchdog.yml)."""
from __future__ import annotations

import os
import sys

import requests

import tg_subscribers


def main() -> int:
    text = " ".join(sys.argv[1:]).strip()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not text or not token:
        print("нет текста или TELEGRAM_BOT_TOKEN")
        return 1
    chats = tg_subscribers.recipients(tg_subscribers.sync(token))
    for chat in chats:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True}, timeout=30)
    print(f"отправлено {len(chats)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
