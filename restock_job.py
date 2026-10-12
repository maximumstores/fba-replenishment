"""Регулярный отчёт по аутам: остатки US/CA/DE/UK из BigQuery → вкладки в Google Sheets → Telegram при изменении списка.

Запуск: python restock_job.py [--dry-run] [--always] [--markets US,CA,DE,UK]
Env: RESTOCK_REPORT_SHEET (id таблицы «FBA Restock Report», сервис-аккаунт = Editor), TELEGRAM_BOT_TOKEN, DASHBOARD_URL,
     GOOGLE_SERVICE_ACCOUNT_JSON, BQ_PROJECT.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import loaders
import market_stock
import restock_report as rr
import tg_subscribers

STATE_FILE = Path("state/restock_state.json")
SHEET_URL = "https://docs.google.com/spreadsheets/d/{id}/edit"
CURRENCY = {"US": "$", "CA": "CAD", "UK": "£", "DE": "€"}
HAS_PLAN = {"US"}  # прогноз спроса на 2 мес есть пока только для US


def open_book(sheet_id: str):
    import gspread

    info = loaders.service_account_info()
    if not info:
        raise RuntimeError("Нет GOOGLE_SERVICE_ACCOUNT_JSON для записи в таблицу")
    return gspread.service_account_from_dict(info, scopes=["https://www.googleapis.com/auth/spreadsheets"]).open_by_key(sheet_id)


def run_market(market: str, df: pd.DataFrame, state: dict, stamp: str, book=None):
    now = rr.out_now(df)
    plan = rr.need_2m(df) if market in HAS_PLAN else df.iloc[0:0].assign(need_units=0.0)
    if book is not None:
        rr.publish(book, market, now, plan, stamp, with_plan=market in HAS_PLAN,
                   lost_label=f"Потери, {CURRENCY.get(market, '')} (по себестоимости)")
    cur1, cur2 = set(now["asin"]), set(plan["asin"])
    a1, r1 = rr.diff_lists(set(state.get(f"{market}:now", [])), cur1)
    a2, r2 = rr.diff_lists(set(state.get(f"{market}:plan", [])), cur2)
    state[f"{market}:now"], state[f"{market}:plan"] = sorted(cur1), sorted(cur2)
    changed = bool(a1 or r1 or a2 or r2)
    text = rr.render_text(market, now, plan, a1, r1, a2, r2, cur=CURRENCY.get(market, '€'), basis=' (по себестоимости)') if market in HAS_PLAN else _text_now_only(market, now, a1, r1)
    return text, changed, now, plan


def _text_now_only(market: str, now: pd.DataFrame, added: list[str], removed: list[str]) -> str:
    lines = [f"{market}: аут сейчас — {len(now)} ASIN (+{len(added)} / −{len(removed)}), потери ≈ {now['lost_eur'].sum():,.0f} {CURRENCY.get(market, '')} (по себестоимости)"]
    for _, r in rr.grouped(now).head(5).iterrows():
        lines.append(f"• {r['ab'] or '—'} · {r['category'] or '—'} · {r['color'] or '—'}: {r['asins']} ASIN")
    return "\n".join(lines).replace(",", " ")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--always", action="store_true", help="слать сводку, даже если список не изменился")
    ap.add_argument("--markets", default="US,CA,DE,UK")
    a = ap.parse_args(argv)

    markets = tuple(m.strip().upper() for m in a.markets.split(",") if m.strip())
    data = market_stock.load_markets(markets)
    stamp = datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%d.%m.%Y %H:%M")
    sheet_id = os.getenv("RESTOCK_REPORT_SHEET", "")
    book = open_book(sheet_id) if sheet_id and not a.dry_run else None
    state = rr.load_state(STATE_FILE)

    texts, changed = [], False
    if book is not None:  # пустой лист по умолчанию после первого запуска больше не нужен
        try:
            default = book.worksheet("Sheet1")
            if len(book.worksheets()) > 1 and not default.get_all_values():
                book.del_worksheet(default)
        except Exception:
            pass
    for m in markets:
        t, ch, *_ = run_market(m, data[m], state, stamp, book)
        texts.append(t)
        changed = changed or ch
    msg = "\n\n".join(texts)
    if sheet_id:
        msg += "\n\nТаблица: " + SHEET_URL.format(id=sheet_id)
    if os.getenv("DASHBOARD_URL"):
        msg += "\nПриложение: " + os.environ["DASHBOARD_URL"]

    if a.dry_run:
        print(msg + "\n\n[dry-run: ничего не записано и не отправлено]")
        return 0
    if not changed and not a.always:
        print("Списки не изменились, сообщение не отправляю.")
        rr.save_state(STATE_FILE, state)
        return 0
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chats = tg_subscribers.recipients(tg_subscribers.sync(token)) if token else []
    if not chats:
        print("Некому слать (нет токена или подписчиков). Нажмите Start у бота и запустите снова. Диагностика: "
              + (tg_subscribers.diagnose(token) if token else "нет TELEGRAM_BOT_TOKEN") + "\n\n" + msg)
        rr.save_state(STATE_FILE, state)  # таблица уже обновлена; список запоминаем, чтобы первое сообщение не потерялось при следующем запуске
        return 2
    import requests

    for chat in chats:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": msg[:4000], "disable_web_page_preview": True}, timeout=30)
        if not r.ok:
            raise RuntimeError(f"Telegram вернул {r.status_code}: {r.text[:300]}")
    rr.save_state(STATE_FILE, state)
    print(f"Отправлено {len(chats)} подписчикам")
    return 0


if __name__ == "__main__":
    sys.exit(main())
