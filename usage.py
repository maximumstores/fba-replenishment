"""Учёт использования дашборда по единому стандарту «Использование» (Scorecard).

Пишем три журнала в BigQuery (датасет USAGE_DATASET, по умолчанию fba_replenishment):
  login_log(email, logged_in_at)          — вход (новая сессия)
  page_views(email, section, viewed_at)   — какой раздел открыл
  edit_log(email, action, edited_at)      — что поменял в настройках
Запись идёт в фоне и никогда не ломает дашборд: при ошибке она просто запоминается в LAST_ERROR.

Показатель для Scorecard — «Регулярность»: среднее по сотрудникам долей рабочих дней периода, в которые был вход.
Сотрудники = все, кто когда-либо заходил до конца периода. Время переводится в Киев.
"""
from __future__ import annotations

import os
import threading
from datetime import date, timedelta

import pandas as pd

KYIV = "Europe/Kiev"
LAST_ERROR: dict[str, str] = {}
_ENSURED = {"done": False}


def dataset() -> str:
    return os.getenv("USAGE_DATASET", "fba_replenishment").strip()


def enabled() -> bool:
    return os.getenv("USAGE_LOG", "on").strip().lower() not in ("off", "0", "false", "no")


def _t(name: str) -> str:
    from loaders import _project
    return f"`{_project()}.{dataset()}.{name}`"


DDL = {
    "login_log": "email STRING NOT NULL, logged_in_at TIMESTAMP NOT NULL",
    "page_views": "email STRING NOT NULL, section STRING NOT NULL, viewed_at TIMESTAMP NOT NULL",
    "edit_log": "email STRING NOT NULL, action STRING NOT NULL, edited_at TIMESTAMP NOT NULL",
}


def _client():
    from loaders import bq_client
    return bq_client()


def _ensure_tables(client) -> None:
    if _ENSURED["done"]:
        return
    for name, cols in DDL.items():
        client.query(f"CREATE TABLE IF NOT EXISTS {_t(name)} ({cols})").result()
    _ENSURED["done"] = True


def _write(sql: str, params: dict[str, str]) -> None:
    from google.cloud import bigquery

    try:
        client = _client()
        _ensure_tables(client)
        cfg = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter(k, "STRING", v) for k, v in params.items()])
        client.query(sql, job_config=cfg).result()
        LAST_ERROR.pop("write", None)
    except Exception as exc:  # журнал не важнее работы
        LAST_ERROR["write"] = f"{type(exc).__name__}: {exc}"[:400]


def _async(sql: str, params: dict[str, str]) -> None:
    if enabled() and params.get("email"):
        threading.Thread(target=_write, args=(sql, params), daemon=True).start()


def log_login(email: str) -> None:
    _async(f"INSERT INTO {_t('login_log')} (email, logged_in_at) VALUES (@email, CURRENT_TIMESTAMP())",
           {"email": email})


def log_page_view(email: str, section: str) -> None:
    _async(f"INSERT INTO {_t('page_views')} (email, section, viewed_at) VALUES (@email, @section, CURRENT_TIMESTAMP())",
           {"email": email, "section": section})


def log_edit(email: str, action: str) -> None:
    _async(f"INSERT INTO {_t('edit_log')} (email, action, edited_at) VALUES (@email, @action, CURRENT_TIMESTAMP())",
           {"email": email, "action": action})


# ── чтение и расчёт ────────────────────────────────────────────────────────
def load_logs() -> dict[str, pd.DataFrame]:
    client = _client()
    out = {}
    for name, ts in (("login_log", "logged_in_at"), ("page_views", "viewed_at"), ("edit_log", "edited_at")):
        df = client.query(f"SELECT * FROM {_t(name)}").to_dataframe()
        df[ts] = pd.to_datetime(df[ts], utc=True).dt.tz_convert(KYIV)
        out[name] = df
    return out


def workdays(start: date, end: date) -> int:
    return sum(1 for i in range((end - start).days + 1) if (start + timedelta(days=i)).weekday() < 5)


def regularity(logins: pd.DataFrame, end: date, days: int) -> dict:
    """Регулярность за `days` дней, оканчивающихся `end` (включительно, время Киева)."""
    start = end - timedelta(days=days - 1)
    if logins is None or logins.empty:
        return {"pct": 0.0, "came": 0, "base": 0, "avg_days": 0.0, "workdays": workdays(start, end), "per_user": pd.DataFrame()}
    df = logins.copy()
    df["day"] = df["logged_in_at"].dt.date
    base_users = sorted(df.loc[df["day"] <= end, "email"].str.lower().unique())
    win = df[(df["day"] >= start) & (df["day"] <= end)].copy()
    win["email"] = win["email"].str.lower()
    wd = workdays(start, end)
    win_wd = win[win["day"].map(lambda d: d.weekday() < 5).astype(bool)]
    days_by_user = win_wd.groupby("email")["day"].nunique()
    logins_by_user = win.groupby("email").size()
    last = win.groupby("email")["logged_in_at"].max()
    per_user = pd.DataFrame({"email": base_users})
    per_user["logins"] = per_user["email"].map(logins_by_user).fillna(0).astype(int)
    per_user["days"] = per_user["email"].map(days_by_user).fillna(0).astype(int)
    per_user["share"] = per_user["days"] / wd if wd else 0.0
    last_by = last.to_dict()
    per_user["last"] = pd.to_datetime(
        pd.Series([last_by.get(e, pd.NaT) for e in per_user["email"]], index=per_user.index), utc=True
    ).dt.tz_convert(KYIV)
    pct = float(per_user["share"].mean() * 100) if len(per_user) else 0.0
    return {"pct": pct, "came": int((per_user["logins"] > 0).sum()), "base": len(base_users),
            "avg_days": float(per_user["days"].mean()) if len(per_user) else 0.0, "workdays": wd,
            "per_user": per_user.sort_values(["days", "logins"], ascending=False)}


def weekly_scorecard(logins: pd.DataFrame, end: date, weeks: int = 8) -> pd.DataFrame:
    """% для Scorecard по неделям (пн–вс); текущая неделя считается по сегодняшний день."""
    monday = end - timedelta(days=end.weekday())
    rows = []
    for i in range(weeks):
        start = monday - timedelta(weeks=i)
        stop = min(start + timedelta(days=6), end)
        r = regularity(logins, stop, (stop - start).days + 1)
        rows.append({"Неделя": f"{start:%d.%m} – {start + timedelta(days=6):%d.%m}", "Регулярность, %": round(r["pct"]),
                     "Зашли": f"{r['came']} из {r['base']}"})
    return pd.DataFrame(rows)
