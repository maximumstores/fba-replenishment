"""Фактические сроки доставки по отправкам (лист calc_shipments из Logistics Dashboard).

Считаем только доставленные отправки выбранного маркета: срок = «Дата доставки» − ETD.
Плановый срок = ETA − ETD. Разница показывает, насколько реальная доставка позже обещанной.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date

import numpy as np
import pandas as pd

from config import Settings

EXCEL_ORIGIN = "1899-12-30"
MAX_DAYS = 150  # отсекаем явные ошибки в датах


def _to_date(s: pd.Series) -> pd.Series:
    """Даты в листе — серийные числа Excel (иногда с долей дня). Текст и пустые → NaT."""
    num = pd.to_numeric(s.astype(str).str.replace(",", ".").str.strip().replace("", np.nan), errors="coerce")
    num = num.where(num > 30000)
    return pd.to_datetime(num, unit="D", origin=EXCEL_ORIGIN, errors="coerce")


def transit_stats(df: pd.DataFrame | None, market: str = "US", today: date | None = None) -> dict | None:
    if df is None or df.empty:
        return None
    need = ["Маркет", "Тип доставки", "ETD", "ETA", "Дата доставки", "Статус"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise ValueError(f"В файле отправок нет колонок: {', '.join(missing)}")
    d = df[df["Маркет"].astype(str).str.strip().str.upper() == market.upper()].copy()
    d["etd"], d["eta"], d["delivered"] = _to_date(d["ETD"]), _to_date(d["ETA"]), _to_date(d["Дата доставки"])
    d["plan"] = (d["eta"] - d["etd"]).dt.days
    d["fact"] = (d["delivered"] - d["etd"]).dt.total_seconds() / 86400
    d["late"] = (d["delivered"] - d["eta"]).dt.total_seconds() / 86400
    done = d[(d["Статус"].astype(str).str.strip() == "Доставлено") & (d["fact"] > 0) & (d["fact"] < MAX_DAYS)]
    if done.empty:
        return None
    table = done.groupby("Тип доставки").agg(
        n=("fact", "size"), plan=("plan", "median"), fact=("fact", "median"),
        p75=("fact", lambda x: x.quantile(0.75)), late=("late", "median"),
    ).sort_values("n", ascending=False).round(1).reset_index()
    table.columns = ["Способ", "Отправок", "План ETD→ETA, дн (медиана)", "Факт ETD→доставка, дн (медиана)",
                     "Факт, дн (75% отправок быстрее)", "Позже ETA, дн (медиана)"]

    today_ts = pd.Timestamp(today or date.today())
    moving = d[d["Статус"].astype(str).str.strip() == "В пути"]
    overdue = moving[moving["eta"].notna() & (moving["eta"] < today_ts)]
    return {
        "market": market,
        "table": table,
        "n": int(len(done)),
        "median": float(done["fact"].median()),
        "p75": float(done["fact"].quantile(0.75)),
        "late_median": float(done["late"].median()),
        "etd_min": done["etd"].min(), "etd_max": done["etd"].max(),
        "in_transit": int(len(moving)),
        "overdue": int(len(overdue)),
        "overdue_median_days": float((today_ts - overdue["eta"]).dt.days.median()) if len(overdue) else 0.0,
    }


def apply_fact_leads(settings: Settings, stats: dict | None, env_lead_sea: str | None) -> Settings:
    """Срок «Море» берём из фактических отправок, если его не задали вручную переменной LEAD_SEA."""
    if stats and not (env_lead_sea or "").strip():
        return replace(settings, lead_sea=float(round(stats["median"])))
    return settings
