"""Ежедневный алерт в Telegram: только НОВЫЕ или ухудшившиеся проблемы, а не весь список каждый день.

    python alert.py --dry-run          # показать текст, ничего не отправлять и не запоминать
    python alert.py                    # отправить и запомнить состояние
    python alert.py --always           # отправить сводку, даже если новых проблем нет
    python alert.py --min-status ACTION  # включить и статус "Склад или воздух"

Нужны TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID (.env). Запуск по расписанию — cron (см. README).
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
from datetime import date
from pathlib import Path

import pandas as pd

import calc
import loaders
import tg_subscribers
import transit
from config import CHANNEL_RU, Settings

STATE_FILE = Path(os.getenv("ALERT_STATE_FILE", "state/alert_state.json"))
TG_LIMIT = 3900  # лимит Telegram 4096, оставляем запас


def load_state(path: Path | None = None) -> dict[str, int]:
    path = path or STATE_FILE  # путь берём в момент вызова, а не при импорте модуля
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict[str, int], path: Path | None = None) -> None:
    path = path or STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=0), encoding="utf-8")


def row_key(r) -> str:
    return f"{r['asin']}|{r['store']}"


def select_alerts(report: pd.DataFrame, previous: dict[str, int], min_status: str = "URGENT",
                  min_lost: float = 0.0) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, int]]:
    """Возвращает (все проблемные, новые или ухудшившиеся, новое состояние).

    Проблемный = статус не менее срочный, чем min_status И потери за 30 дней не меньше min_lost
    (иначе вялые позиции с парой продаж в месяц засоряют алерт). Новый = раньше не был в списке
    или его статус стал срочнее (меньше rank).
    """
    limit = calc.STATUS_RANK[min_status]
    problem = report[(report["status_rank"] <= limit) & (report["lost_30"] >= min_lost)].copy()
    problem["key"] = problem.apply(row_key, axis=1)
    state = {k: int(v) for k, v in zip(problem["key"], problem["status_rank"])}
    is_new = problem["key"].map(lambda k: k not in previous or state[k] < previous[k])
    return problem, problem[is_new], state


def _fmt_date(d) -> str:
    return "—" if pd.isna(d) else pd.Timestamp(d).strftime("%d.%m")


def build_message(problem: pd.DataFrame, new: pd.DataFrame, top: int = 15, dashboard_url: str | None = None,
                  today: date | None = None) -> str:
    today = today or date.today()
    counts = problem["status"].value_counts()
    head = [f"<b>FBA США: пополнение ({today:%d.%m.%Y})</b>"]
    parts = [f"{calc.STATUS_RU[s]}: {int(counts.get(s, 0))}" for s in calc.STATUS_RANK if counts.get(s, 0)]
    head.append(f"Проблемных ASIN: {len(problem)} ({'; '.join(parts) or '—'})")
    head.append(f"Новых или ухудшившихся: {len(new)}")
    if len(problem):
        head.append(f"Недопродажи за 30 дн. без действий: ≈{int(problem['lost_30'].sum()):,} шт.".replace(",", " "))

    lines = []
    shown = new.sort_values(["status_rank", "lost_30"], ascending=[True, False]).head(top)
    for r in shown.itertuples():
        title = " / ".join(x for x in (r.parent_group, r.color, r.size) if x)
        if r.status == "OUT":
            when = "в ауте"
        else:
            when = f"кончится {_fmt_date(r.stockout_date)}"
        chan = CHANNEL_RU.get(r.channel, "—")
        lines.append(
            f"• <b>{html.escape(calc.STATUS_RU[r.status])}</b> [{r.abc}] {html.escape(title)} · <code>{r.asin}</code>\n"
            f"  сток {int(r.on_hand)}, едет {int(r.inbound_counted)}, {r.velocity:.1f}/дн, {when}; "
            f"{html.escape(chan)}, слать ≈{int(r.qty_need)}"
        )
    if len(new) > len(shown):
        lines.append(f"… и ещё {len(new) - len(shown)} в дашборде")
    if dashboard_url:
        lines.append(f'<a href="{html.escape(dashboard_url)}">Открыть дашборд</a>')

    text = "\n".join(head) + ("\n\n" + "\n".join(lines) if lines else "")
    if len(text) > TG_LIMIT:
        text = text[: TG_LIMIT - 20].rsplit("\n", 1)[0] + "\n… (обрезано)"
    return text


def send_telegram(text: str, token: str, chat_id: str) -> None:
    import requests

    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"Telegram вернул {r.status_code}: {r.text[:300]}")


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--always", action="store_true", help="слать сводку, даже если новых проблем нет")
    ap.add_argument("--min-status", default="URGENT", choices=[s for s in calc.STATUS_RANK if s not in ("OK", "NO_SALES")])
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--min-lost", type=float, default=float(os.getenv("ALERT_MIN_LOST_30", "5")),
                    help="не алертить позиции, где недопродажи за 30 дн меньше N шт (по умолчанию 5)")
    a = ap.parse_args(argv)

    data = loaders.load_all()
    settings = transit.apply_fact_leads(Settings.from_env(), data.get("transit"), os.getenv("LEAD_SEA"))
    batches = data["batches"]
    if batches is None and os.getenv("USE_PLANNER_INCOMING", "").lower() in ("1", "true", "yes"):
        batches = data["incoming"]
    report = calc.build_report(data["hopted"], data["reference"], data["sources"], batches, settings)
    problem, new, state = select_alerts(report, load_state(), a.min_status, a.min_lost)
    text = build_message(problem, new, a.top, os.getenv("DASHBOARD_URL"))

    if a.dry_run:
        print(text)
        print("\n[dry-run: ничего не отправлено, состояние не сохранено]")
        return 0
    if new.empty and not a.always:
        print(f"Новых проблем нет (всего проблемных: {len(problem)}), сообщение не отправляю.")
        save_state(state)
        return 0

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chats: list[str] = []
    if token:
        chats = tg_subscribers.recipients(tg_subscribers.sync(token))
    if not token or not chats:
        print("Нет TELEGRAM_BOT_TOKEN или никто не подписан (нажмите Start у бота) — вот что было бы отправлено:\n")
        print(text)
        return 2  # состояние не сохраняем, чтобы алерт не потерялся
    for chat in chats:
        send_telegram(text, token, chat)
    save_state(state)  # сохраняем только после успешной отправки
    print(f"Отправлено {len(chats)} подписчикам: новых {len(new)}, всего проблемных {len(problem)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
