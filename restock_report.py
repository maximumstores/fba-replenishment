"""Регулярный отчёт по аутам на данных Нины (сводные таблицы), без участия команды.

Два отчёта по каждому рынку (US / UK / DE / CA):
  1) «Аут сейчас»          — ASIN, у которых Available <= 0 прямо сейчас;
  2) «Аут с учётом плана»  — срочное количество к рестоку на 2 месяца вперёд:
        need = Demand forecast next 2 month − (Available + Inbound на сейчас), берём только need > 0.

Группировка как у Нины: A/B, Category, Color; сортировка по потерянной выручке (€).
Алерт — когда меняется список ASIN (добавились / ушли) относительно прошлого запуска.

Вход — DataFrame с колонками NORMALIZED (см. normalize); откуда они берутся в таблицах Нины, решает адаптер
(названия колонок сопоставляются по синонимам ALIASES).
"""
from __future__ import annotations

import json
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

import numpy as np
import pandas as pd

NORMALIZED = ["market", "ab", "category", "color", "size", "asin", "available", "inbound", "demand_2m", "lost_eur", "price_eur"]
NUMERIC = ["available", "inbound", "demand_2m", "lost_eur", "price_eur"]
GROUP_BY = ["ab", "category", "color"]

# Названия колонок в таблицах Нины → нормализованные (регистр не важен, лишние пробелы режутся)
ALIASES = {
    "market": ["market", "marketplace", "рынок"],
    "ab": ["a/b", "a/b category+color", "a/b category+color for winter'25-'26", "ab", "abc"],
    "category": ["category", "категория", "category+parent"],
    "color": ["color", "цвет"],
    "size": ["size", "размер"],
    "asin": ["asin"],
    "available": ["available", "sum of available", "sum из available", "available (units)", "fba available"],
    "inbound": ["inbound", "inbound+fc transfer+fc processing", "sum из inbound+fc transfer+fc processing"],
    "demand_2m": ["demand forecast next 2 month", "demand forecast next 2 months", "forecast 2m", "demand 2m"],
    "lost_eur": ["lost sale", "lost sale oct", "sum из lost sale oct", "sum of lost sale oct"],
    "price_eur": ["price", "price_eur", "price eur", "цена"],
}


def _clean_number(v) -> float:
    s = str(v).replace("€", "").replace("$", "").replace("\xa0", "").replace(" ", "").replace("%", "").strip()
    if not s or s.lower() in ("nan", "none", "-", "#n/a"):
        return 0.0
    if "," in s and "." not in s:
        s = s.replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def normalize(raw: pd.DataFrame, market: str = "") -> pd.DataFrame:
    """Приводит таблицу Нины к нормализованным колонкам; обязательные: asin, available. Остальное — по возможности."""
    lowered = {str(c).strip().lower(): c for c in raw.columns}
    out = pd.DataFrame(index=raw.index)
    for key, names in ALIASES.items():
        src = next((lowered[n] for n in names if n in lowered), None)
        out[key] = raw[src] if src is not None else ("" if key not in NUMERIC else 0.0)
    if not market and "market" in out and (out["market"] != "").any():
        market = ""
    if market:
        out["market"] = market
    out["asin"] = out["asin"].astype(str).str.strip().str.upper()
    out = out[out["asin"].str.fullmatch(r"B0[A-Z0-9]{8}")].copy()
    for c in NUMERIC:
        out[c] = out[c].map(_clean_number)
    for c in ("ab", "category", "color", "size", "market"):
        out[c] = out[c].astype(str).str.strip().replace({"nan": ""})
    return out.reset_index(drop=True)


def _lost(df: pd.DataFrame, units_short: pd.Series) -> pd.Series:
    """Потерянная выручка: берём готовую (Lost Sale, €) у Нины, иначе нехватка × цена."""
    have = df["lost_eur"] > 0
    return np.where(have, df["lost_eur"], units_short * df["price_eur"])


def out_now(df: pd.DataFrame) -> pd.DataFrame:
    """Отчёт 1: ASIN без доступного стока сейчас."""
    r = df[df["available"] <= 0].copy()
    r["need_units"] = np.maximum(r["demand_2m"] - (r["available"] + r["inbound"]), 0.0)
    r["lost_eur"] = _lost(r, r["need_units"])
    return r.sort_values("lost_eur", ascending=False).reset_index(drop=True)


def need_2m(df: pd.DataFrame) -> pd.DataFrame:
    """Отчёт 2: срочное количество к рестоку = Demand 2 мес − (Available + Inbound), только положительное."""
    r = df.copy()
    r["need_units"] = np.maximum(r["demand_2m"] - (r["available"] + r["inbound"]), 0.0)
    r = r[r["need_units"] > 0].copy()
    r["lost_eur"] = _lost(r, r["need_units"])
    return r.sort_values("lost_eur", ascending=False).reset_index(drop=True)


def grouped(report: pd.DataFrame, by: list[str] | None = None) -> pd.DataFrame:
    """Группировка как у Нины (A/B → Category → Color) с суммами и сортировкой по потерянной выручке."""
    by = by or GROUP_BY
    if report.empty:
        return pd.DataFrame(columns=[*by, "asins", "need_units", "lost_eur"])
    g = report.groupby(by, dropna=False, as_index=False).agg(
        asins=("asin", "nunique"), need_units=("need_units", "sum"), lost_eur=("lost_eur", "sum"))
    return g.sort_values("lost_eur", ascending=False).reset_index(drop=True)


def diff_lists(prev: set[str], cur: set[str]) -> tuple[list[str], list[str]]:
    """(добавились, ушли) — алерт только когда список изменился."""
    return sorted(cur - prev), sorted(prev - cur)


def load_state(path: str | Path) -> dict[str, list[str]]:
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_state(path: str | Path, state: dict[str, list[str]]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def render_text(market: str, now: pd.DataFrame, plan: pd.DataFrame, added1: list[str], removed1: list[str],
                added2: list[str], removed2: list[str], top: int = 8, url: str = "") -> str:
    lines = [f"{market}: отчёт по аутам",
             f"1) Аут сейчас: {len(now)} ASIN (+{len(added1)} / −{len(removed1)}), потери ≈ {now['lost_eur'].sum():,.0f} €",
             f"2) Срочно к рестоку на 2 мес: {len(plan)} ASIN (+{len(added2)} / −{len(removed2)}), "
             f"{plan['need_units'].sum():,.0f} шт., потери ≈ {plan['lost_eur'].sum():,.0f} €"]
    g = grouped(plan).head(top)
    if not g.empty:
        lines.append("Топ групп по потерям (A/B · категория · цвет):")
        for _, r in g.iterrows():
            lines.append(f"• {r['ab'] or '—'} · {r['category'] or '—'} · {r['color'] or '—'}: {r['need_units']:,.0f} шт., {r['lost_eur']:,.0f} €")
    if url:
        lines.append(url)
    return "\n".join(lines).replace(",", " ")


def send_email(subject: str, body: str, to: list[str]) -> None:
    """SMTP: SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASSWORD, SMTP_FROM (по умолчанию = SMTP_USER)."""
    host, user, pwd = os.getenv("SMTP_HOST"), os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD")
    if not (host and user and pwd and to):
        raise RuntimeError("Нет SMTP_HOST / SMTP_USER / SMTP_PASSWORD или списка получателей")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, os.getenv("SMTP_FROM", user), ", ".join(to)
    msg.set_content(body)
    with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=30) as s:
        s.starttls()
        s.login(user, pwd)
        s.send_message(msg)
