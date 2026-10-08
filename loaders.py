"""Загрузка входных данных. Любой источник можно заменить CSV-файлом.

HOPTED_SOURCE     csv-путь | sheet:<ID файла Hopted> | bq        (bq = mt.hopted_us_native, обновляется ночью)
REFERENCE_SOURCE  csv-путь | bq                                 (forecast.dim_product + план месяца)
SOURCES_SOURCE    csv-путь | bq                                 (остатки AWD / склад и заказы: mt.amazon_starting_balance_native)
                  (старое имя SOURCES_CSV тоже работает)
BATCHES_CSV       необязательно: asin, qty, eta, destination    (партии из Orders-Stock)
INCOMING_SOURCE   необязательно: csv-путь | bq                  (приходы по месяцам из планировщика, forecast.psi_projection)
SHIPMENTS_SOURCE  необязательно: csv-путь | sheet:<ID>          (лист calc_shipments из Logistics Dashboard: сроки доставки)

load_all() возвращает данные и список «источников» (meta) для вкладки «Источники» в дашборде.
"""
from __future__ import annotations

import base64
import json
import math
import os
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import calc

HOPTED_TAB = os.getenv("HOPTED_TAB", "US V2")
HOPTED_RANGE = os.getenv("HOPTED_RANGE", "A:T")  # правее T лежат формулы-сводки, они не нужны
SHIPMENTS_TAB = os.getenv("SHIPMENTS_TAB", "calc_shipments")
BQ_DEFAULT_PROJECT = "reorder-497714"

REFERENCE_SQL = """
SELECT
  p.asin, p.old_asin, p.group_key, p.parent_group, p.category, p.color, p.size,
  p.abcd_class, p.active_us,
  pp.plan_units AS plan_units_month
FROM `{project}.forecast.dim_product` p
LEFT JOIN (
  SELECT asin, SUM(plan_units) AS plan_units
  FROM `{project}.forecast.psi_projection`
  WHERE month = DATE_TRUNC(CURRENT_DATE(), MONTH)
  GROUP BY asin
) pp USING (asin)
"""
HOPTED_SQL = "SELECT * FROM `{project}.mt.hopted_us_native`"
SOURCES_SQL = "SELECT * FROM `{project}.mt.amazon_starting_balance_native`"
INCOMING_SQL = """
SELECT asin, month, SUM(incoming) AS incoming
FROM `{project}.forecast.psi_projection`
WHERE incoming > 0 AND month >= DATE_TRUNC(CURRENT_DATE(), MONTH)
GROUP BY asin, month
"""

# В листе остатков заголовки лежат в строке с «ASIN» в колонке A; нужные колонки ищем по тексту заголовка
SOURCES_HEADERS = {
    "awd_qty": "AWD US",
    "wrh_qty": "WRH US+ Inbound",
    "order_qty": "Order US",
    "fba_stock_transit": "Stock+In transit US",
}
SOURCES_ALIASES = {"awd_us": "awd_qty", "wrh_us_inbound": "wrh_qty", "order_us": "order_qty"}  # имена из ручной выгрузки


def service_account_info() -> dict | None:
    """Ключ сервисного аккаунта из GOOGLE_SERVICE_ACCOUNT_JSON (json или base64 от json).

    Так ключ приходит из Streamlit Secrets и GitHub Secrets, где файла нет."""
    raw = (os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip()
    if not raw:
        return None
    if not raw.startswith("{"):
        raw = base64.b64decode(raw).decode("utf-8")
    info = json.loads(raw)
    if info.get("type") != "service_account" or not info.get("private_key"):
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON: это не ключ сервисного аккаунта (нет type/private_key)")
    return info


def bq_client():
    """Клиент BigQuery: ключ из GOOGLE_SERVICE_ACCOUNT_JSON, иначе учётные данные окружения."""
    from google.cloud import bigquery

    project = os.getenv("BQ_PROJECT", BQ_DEFAULT_PROJECT)
    info = service_account_info()
    if info:
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/bigquery"])
        return bigquery.Client(project=project, credentials=creds)
    return bigquery.Client(project=project)


def _bq_query(sql: str) -> pd.DataFrame:
    return bq_client().query(sql).to_dataframe()


def _project() -> str:
    return os.getenv("BQ_PROJECT", BQ_DEFAULT_PROJECT)


def _clean_header(values: list) -> list[str]:
    """Пустые и повторяющиеся заголовки получают уникальные имена, первое вхождение остаётся как есть."""
    seen: dict[str, int] = {}
    out = []
    for i, h in enumerate(values):
        name = "" if h is None or (isinstance(h, float) and math.isnan(h)) else str(h).strip()
        if name in seen or not name:
            seen[name] = seen.get(name, 0) + 1
            name = f"{name}__{i}"
        else:
            seen[name] = 1
        out.append(name)
    return out


def _rows_to_df(values: list[list]) -> pd.DataFrame:
    if not values:
        return pd.DataFrame()
    header = _clean_header(values[0])
    width = len(header)
    body = [(row + [""] * width)[:width] for row in values[1:]]
    return pd.DataFrame(body, columns=header)


def _bq_frame_to_rows(df: pd.DataFrame) -> list[list]:
    return df.astype(object).where(df.notna(), "").values.tolist()


def _csv(path: str) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def _gspread_values(spec: str, tab: str, rng: str | None, credentials_file: str | None) -> list[list]:
    import gspread

    creds = credentials_file or os.getenv("GOOGLE_CREDENTIALS_FILE")
    info = None if creds else service_account_info()
    if creds:
        client = gspread.service_account(filename=creds)
    elif info:
        client = gspread.service_account_from_dict(
            info, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    else:  # Cloud Shell / Cloud Run: берём учётные данные окружения (gcloud auth application-default login)
        try:
            import google.auth

            adc, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
            client = gspread.authorize(adc)
        except Exception as exc:
            raise RuntimeError("Для чтения Google Sheets нужен GOOGLE_CREDENTIALS_FILE (json сервисного аккаунта), GOOGLE_SERVICE_ACCOUNT_JSON "
                               f"или учётные данные окружения (gcloud auth application-default login): {exc}") from exc
    ws = client.open_by_key(spec[len("sheet:"):]).worksheet(tab)
    return ws.get(rng, value_render_option="UNFORMATTED_VALUE") if rng else ws.get(value_render_option="UNFORMATTED_VALUE")


def _gspread_tab(spec: str, tab: str, rng: str | None, credentials_file: str | None) -> pd.DataFrame:
    return _rows_to_df(_gspread_values(spec, tab, rng, credentials_file))


def load_hopted(spec: str, credentials_file: str | None = None) -> pd.DataFrame:
    if spec.startswith("sheet:"):
        return _gspread_tab(spec, HOPTED_TAB, HOPTED_RANGE, credentials_file)
    if spec.lower() == "bq":
        rows = _bq_frame_to_rows(_bq_query(HOPTED_SQL.format(project=_project())))
        start = next((i for i, r in enumerate(rows) if str(r[0]).strip() == "Hopted ID"), None)
        if start is None:
            raise ValueError("В mt.hopted_us_native не нашёл строку заголовков (первая колонка «Hopted ID»)")
        return _rows_to_df(rows[start:])
    return _csv(spec)


def load_reference(spec: str | None) -> pd.DataFrame | None:
    if not spec:
        return None
    if spec.lower() == "bq":
        return _bq_query(REFERENCE_SQL.format(project=_project()))
    return _csv(spec)


def _sources_from_bq_rows(rows: list[list]) -> pd.DataFrame:
    """Лист остатков: заголовки в строке, где col_A = «ASIN»; данные — строки с ASIN формата B0XXXXXXXX."""
    head_i = next((i for i, r in enumerate(rows) if str(r[0]).strip() == "ASIN"), None)
    if head_i is None:
        raise ValueError("В mt.amazon_starting_balance_native не нашёл строку заголовков (колонка A = «ASIN»)")
    header = [str(h).strip() for h in rows[head_i]]
    idx = {}
    for key, title in SOURCES_HEADERS.items():
        if title not in header:
            raise ValueError(f"В листе остатков нет колонки «{title}» — формат листа изменился")
        idx[key] = header.index(title)
    out = []
    for r in rows[head_i + 1:]:
        asin = str(r[0]).strip().upper()
        if len(asin) == 10 and asin[0] == "B" and asin.isalnum():
            out.append({"asin": asin, **{k: r[i] for k, i in idx.items()}})
    return pd.DataFrame(out)


def load_sources(spec: str | None) -> pd.DataFrame | None:
    """Остатки на AWD (awd_qty), складе США с inbound (wrh_qty) и заказы в производстве (order_qty) по ASIN."""
    if not spec or (spec.lower() != "bq" and not Path(spec).exists()):
        return None
    if spec.lower() == "bq":
        return _sources_from_bq_rows(_bq_frame_to_rows(_bq_query(SOURCES_SQL.format(project=_project()))))
    df = _csv(spec)
    return df.rename(columns={c: SOURCES_ALIASES.get(str(c).strip().lower(), c) for c in df.columns})


AWD_LIVE_TAB = "AWD"
AWD_ONHAND_TITLES = ("on-hand quantity", "available in awd")


def _num(v) -> float:
    try:
        return float(str(v).replace(",", ".").replace("\xa0", "").replace(" ", "")) if str(v).strip() else 0.0
    except ValueError:
        return 0.0


def awd_from_rows(rows: list[list]) -> pd.DataFrame:
    """Вкладка AWD (отчёт Amazon): заголовок — строка, где есть «ASIN» и «On-hand quantity»/«Available in AWD».

    Берём последний такой заголовок в первых 30 строках, суммируем по ASIN."""
    head = None
    for i, r in enumerate(rows[:30]):
        low = [str(c).strip().lower() for c in r]
        if "asin" in low and any(t in low for t in AWD_ONHAND_TITLES):
            head = i
    if head is None:
        raise ValueError("На вкладке AWD не нашёл заголовок с «ASIN» и «On-hand quantity»")
    low = [str(c).strip().lower() for c in rows[head]]
    ia = low.index("asin")
    io = next(low.index(t) for t in AWD_ONHAND_TITLES if t in low)
    out: dict[str, float] = {}
    for r in rows[head + 1:]:
        if len(r) <= max(ia, io):
            continue
        asin = str(r[ia]).strip().upper()
        if len(asin) == 10 and asin[0] == "B" and asin.isalnum():
            out[asin] = out.get(asin, 0.0) + _num(r[io])
    return pd.DataFrame({"asin": list(out), "awd_live": list(out.values())})


def apply_live_awd(sources: pd.DataFrame | None, live: pd.DataFrame) -> tuple[pd.DataFrame | None, str]:
    """Подменяет awd_qty свежими остатками AWD; ASIN, которых нет в отчёте, получают 0. Защита: странные суммы не применяем."""
    live_sum = float(live["awd_live"].sum())
    if sources is None or live.empty:
        return sources, "Свежий AWD не применён: нет базы остатков или отчёт пуст."
    old = sources.copy()
    old_sum = float(pd.to_numeric(old["awd_qty"], errors="coerce").fillna(0).sum())
    if old_sum > 0 and not (0.2 <= live_sum / old_sum <= 5):
        return sources, (f"Свежий AWD не применён: сумма {live_sum:,.0f} шт. слишком отличается от снимка "
                         f"{old_sum:,.0f} шт. — проверьте вкладку AWD.").replace(",", " ")
    old["asin"] = old["asin"].astype(str).str.upper()
    m = old.merge(live, on="asin", how="outer")
    for c in m.columns:
        if c not in ("asin", "awd_live", "awd_qty"):
            m[c] = m[c].where(m[c].notna(), 0)
    m["awd_qty"] = m["awd_live"].fillna(0)
    m = m.drop(columns="awd_live")
    return m, (f"AWD взят из свежего отчёта: {live_sum:,.0f} шт. (в снимке на начало месяца было {old_sum:,.0f} шт.).").replace(",", " ")


def load_incoming(spec: str | None) -> pd.DataFrame | None:
    """Приходы по месяцам из планировщика → строки партий (eta = конец месяца, destination AMZ)."""
    if not spec or (spec.lower() != "bq" and not Path(spec).exists()):
        return None
    raw = _bq_query(INCOMING_SQL.format(project=_project())) if spec.lower() == "bq" else _csv(spec)
    if raw.empty:
        return None
    month_end = pd.to_datetime(raw["month"]) + pd.offsets.MonthEnd(0)
    return pd.DataFrame({
        "asin": raw["asin"].astype(str), "qty": raw["incoming"],
        "eta": month_end.dt.strftime("%d.%m.%Y"), "destination": "AMZ",
    })


def load_shipments(spec: str | None, credentials_file: str | None = None) -> pd.DataFrame | None:
    if not spec:
        return None
    if spec.startswith("sheet:"):
        return _gspread_tab(spec, SHIPMENTS_TAB, None, credentials_file)
    return _csv(spec) if Path(spec).exists() else None


def _optional_csv(path: str | None) -> pd.DataFrame | None:
    if not path or not Path(path).exists():
        return None
    return _csv(path)


def _hopted_date(raw: pd.DataFrame):
    try:
        return calc.prepare_hopted(raw)["snapshot"].max()
    except Exception:
        return pd.NaT


def build_meta(data: dict, specs: dict, stats: dict | None) -> list[dict]:
    """Описание источников для вкладки «Источники»: откуда, для чего, сколько строк, на какую дату."""
    snap = _hopted_date(data["hopted"])
    age = (pd.Timestamp.today().normalize() - snap).days if pd.notna(snap) else None
    rows = [{
        "Источник": "Hopted, остатки и продажи FBA (США)", "Откуда": specs["hopted"],
        "Для чего": "Сток на FBA, едущее на FBA (inbound), продажи за 7 и 30 дней", "Строк": len(data["hopted"]),
        "Данные на": "" if pd.isna(snap) else f"{snap:%d.%m.%Y}",
        "Статус": "устарел" if age is not None and age > 7 else "ок",
    }]
    ref = data["reference"]
    rows.append({
        "Источник": "Справочник ASIN (dim_product + план месяца)", "Откуда": specs["reference"] or "—",
        "Для чего": "Группа, цвет, размер, признак активного ASIN в США, плановая скорость",
        "Строк": 0 if ref is None else len(ref), "Данные на": "", "Статус": "нет" if ref is None else "ок",
    })
    src = data["sources"]
    awd = wrh = None
    if src is not None:
        prep = calc.prepare_sources(src)
        awd, wrh = prep["awd_qty"].sum(), prep["wrh_qty"].sum()
    rows.append({
        "Источник": "Остатки AWD и склада США, заказы в производстве", "Откуда": specs["sources"] or "—",
        "Для чего": "Можно ли пополнить с AWD или склада; есть ли что отправлять морем или воздухом" + (
            "" if awd is None else f" (сейчас: AWD {awd:,.0f} шт., склад {wrh:,.0f} шт.)".replace(",", " ")),
        "Строк": 0 if src is None else len(src), "Данные на": ("AWD " + ("свежий" if data.get("awd_note", "").startswith("AWD взят") else "снимок 01.10") + ", склад "
                      + ("свежий (без DE)" if data.get("wrh_note", "").startswith("Склад взят") else "снимок 01.10") + ", заказы снимок 01.10")
                     if specs["sources"] == "bq" else "",
        "Статус": "нет" if src is None else "ок",
    })
    inc = data["incoming"]
    rows.append({
        "Источник": "Приходы из планировщика (по месяцам)", "Откуда": specs["incoming"] or "—",
        "Для чего": "Когда придёт новый товар; только по месяцам и по всем направлениям сразу (AWD, склад, FBA)",
        "Строк": 0 if inc is None else len(inc), "Данные на": "", "Статус": "нет" if inc is None else "ок",
    })
    bat = data["batches"]
    rows.append({
        "Источник": "Партии Orders-Stock (с датами приёмки)", "Откуда": specs["batches"] or "—",
        "Для чего": "Точные даты прихода партий на FBA", "Строк": 0 if bat is None else len(bat),
        "Данные на": "", "Статус": "нет" if bat is None else "ок",
    })
    ship = data["shipments"]
    rows.append({
        "Источник": "Отправки (Logistics Dashboard, calc_shipments)", "Откуда": specs["shipments"] or "—",
        "Для чего": "Фактические сроки доставки по способам",
        "Строк": 0 if ship is None else len(ship),
        "Данные на": "" if not stats else f"отправки с {stats['etd_min']:%d.%m.%Y} по {stats['etd_max']:%d.%m.%Y}",
        "Статус": "нет" if ship is None else "ок",
    })
    return rows


def load_all(env: dict | None = None, credentials_file: str | None = None) -> dict:
    import transit

    env = env if env is not None else os.environ
    specs = {
        "hopted": env.get("HOPTED_SOURCE", "demo/hopted_us.csv"),
        "reference": env.get("REFERENCE_SOURCE", "demo/reference.csv"),
        "sources": env.get("SOURCES_SOURCE") or env.get("SOURCES_CSV", "demo/sources.csv"),
        "batches": env.get("BATCHES_CSV", "demo/batches.csv"),
        "incoming": env.get("INCOMING_SOURCE", ""),
        "shipments": env.get("SHIPMENTS_SOURCE", ""),
        "awd_live": env.get("AWD_LIVE_SOURCE", ""),
        "wrh_live": env.get("WAREHOUSE_LIVE_SOURCE", ""),
    }
    warnings: list[str] = []
    try:
        shipments = load_shipments(specs["shipments"], credentials_file)
    except Exception as exc:  # сроки доставки необязательны: без них считаем по допущениям, а не падаем
        shipments = None
        who = (service_account_info() or {}).get("client_email", "сервисного аккаунта")
        warnings.append(
            f"Фактические сроки доставки не загружены ({type(exc).__name__}). Откройте доступ читателя к листу "
            f"Logistics Dashboard для {who}; пока срок «Море» берётся из настройки LEAD_SEA."
        )
    sources = load_sources(specs["sources"])
    awd_note = ""
    if specs["awd_live"].startswith("sheet:"):
        try:
            live = awd_from_rows(_gspread_values(specs["awd_live"], AWD_LIVE_TAB, None, credentials_file))
            sources, awd_note = apply_live_awd(sources, live)
        except Exception as exc:  # свежий AWD необязателен: остаёмся на снимке
            awd_note = f"Свежий AWD не загружен ({type(exc).__name__}: {exc}); AWD взят из снимка на начало месяца."
        if not awd_note.startswith("AWD взят"):
            warnings.append(awd_note)
    wrh_note = ""
    if specs["wrh_live"].startswith("sheet:"):
        try:
            book = _gspread_book(specs["wrh_live"], credentials_file)
            live_w = warehouse_live_from_rows(
                book.worksheet(WAREHOUSE_TOTAL_TAB).get(value_render_option="UNFORMATTED_VALUE"),
                book.worksheet(WAREHOUSE_BLOCKS_TAB).get(value_render_option="UNFORMATTED_VALUE"),
                tuple(x.strip() for x in env.get("WAREHOUSE_EXCLUDE", "DE").split(",") if x.strip()))
            sources, wrh_note = apply_live_wrh(sources, live_w)
        except Exception as exc:  # свежий склад необязателен: остаёмся на снимке
            wrh_note = f"Свежий склад не загружен ({type(exc).__name__}: {exc}); склад взят из снимка на начало месяца."
        if not wrh_note.startswith("Склад взят"):
            warnings.append(wrh_note)
    data = {
        "warnings": warnings,
        "awd_note": awd_note,
        "wrh_note": wrh_note,
        "hopted": load_hopted(specs["hopted"], credentials_file),
        "reference": load_reference(specs["reference"]),
        "sources": sources,
        "batches": _optional_csv(specs["batches"]),
        "incoming": load_incoming(specs["incoming"]),
        "shipments": shipments,
        "loaded_at": datetime.now(),
        "hopted_spec": specs["hopted"],
        "reference_spec": specs["reference"],
    }
    data["transit"] = transit.transit_stats(data["shipments"])
    data["meta"] = build_meta(data, specs, data["transit"])
    return data


def restock_diagnostics(spec: str, credentials_file: str | None = None) -> dict:
    """Диагностика вкладки Restock: шапка, число ASIN, суммы колонок SUM/MAX/Plan, строки по аккаунтам, первые строки."""
    rows = _gspread_values(spec, "Restock", None, credentials_file)
    if not rows:
        raise ValueError("Вкладка Restock пуста")
    head = [str(h) for h in rows[0]]
    ia = next((i for i, h in enumerate(head) if h.strip().upper() == "ASIN"), None)
    if ia is None:
        raise ValueError("На вкладке Restock нет колонки ASIN в первой строке")
    data = [r for r in rows[1:] if len(r) > ia and str(r[ia]).strip().upper().startswith("B0") and len(str(r[ia]).strip()) == 10]
    sums = {f"[{i}] {h}": round(sum(_num(r[i]) for r in data if len(r) > i))
            for i, h in enumerate(head) if i != ia and h.startswith(("SUM", "MAX", "Plan"))}
    accounts: dict[str, int] = {}
    for r in data:
        accounts[str(r[0])] = accounts.get(str(r[0]), 0) + 1
    return {"rows": len(rows), "header": head, "asin_rows": len(data), "asin_unique": len({str(r[ia]).strip() for r in data}),
            "items": [{"asin": str(r[ia]).strip().upper(), "restock": _num(r[8]) if len(r) > 8 else 0.0,
                       "wrh": _num(r[9]) if len(r) > 9 else 0.0, "awd": _num(r[10]) if len(r) > 10 else 0.0} for r in data],
            "sums": sums, "accounts": dict(sorted(accounts.items(), key=lambda kv: -kv[1])[:12]),
            "sample": [[str(c) for c in r] for r in data[:3]]}


def _gspread_book(spec: str, credentials_file: str | None = None):
    import gspread

    info = service_account_info()
    creds = credentials_file or os.getenv("GOOGLE_CREDENTIALS_FILE")
    if creds:
        client = gspread.service_account(filename=creds)
    elif info:
        client = gspread.service_account_from_dict(info, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    else:
        raise RuntimeError("Нет ключа сервисного аккаунта")
    return client.open_by_key(spec[len("sheet:"):])


def scan_workbook(spec: str, credentials_file: str | None = None, only: list[str] | None = None) -> list[dict]:
    """Обзор вкладок книги: размер, число ASIN-подобных ячеек, колонки с ASIN и первые строки (обрезаны). Только чтение."""
    out = []
    for ws in _gspread_book(spec, credentials_file).worksheets():
        if only and ws.title not in only:
            continue
        try:
            rows = ws.get("A1:AZ60", value_render_option="FORMATTED_VALUE")
        except Exception as exc:
            out.append({"tab": ws.title, "error": f"{type(exc).__name__}"})
            continue
        asin_cols: dict[int, int] = {}
        for r in rows:
            for i, c in enumerate(r):
                v = str(c).strip().upper()
                if len(v) == 10 and v[0] == "B" and v[1] == "0" and v.isalnum():
                    asin_cols[i] = asin_cols.get(i, 0) + 1
        out.append({"tab": ws.title, "rows_total": ws.row_count, "cols_total": ws.col_count,
                    "asin_cols": {k: v for k, v in sorted(asin_cols.items())},
                    "head": [[str(c).replace("\n", " ")[:18] for c in r[:16]] for r in rows[:5]]})
    return out


def warehouse_pivots_check(spec: str, credentials_file: str | None = None) -> dict:
    """Суммы по сводным вкладкам склада: Pivot Table warehouse (ASIN→количество) и pivot Warehouses FFbox (4 блока)."""
    book = _gspread_book(spec, credentials_file)

    def asin_ok(v) -> bool:
        v = str(v).strip().upper()
        return len(v) == 10 and v[:2] == "B0" and v.isalnum()

    res: dict = {}
    rows = book.worksheet("Pivot Table warehouse").get(value_render_option="UNFORMATTED_VALUE")
    pairs = [(str(r[0]).strip().upper(), _num(r[1])) for r in rows if len(r) > 1 and asin_ok(r[0])]
    total_cell = next((r[:3] for r in rows if r and str(r[0]).strip().lower() == "grand total"), None)
    res["pivot_warehouse"] = {"asin": len(pairs), "uniq": len({a for a, _ in pairs}), "sum": round(sum(q for _, q in pairs)),
                              "nonzero": sum(1 for _, q in pairs if q > 0), "grand_total_cell": total_cell,
                              "head": [[str(c)[:20] for c in r[:6]] for r in rows[:4]]}
    rows = book.worksheet("pivot Warehouses FFbox").get(value_render_option="UNFORMATTED_VALUE")
    names = [str(c).strip() for c in rows[0]] if rows else []
    blocks = {}
    for col in (0, 3, 6, 9):
        label = names[col] if col < len(names) and names[col] else f"блок {col}"
        items = [(str(r[col]).strip().upper(), _num(r[col + 1])) for r in rows if len(r) > col + 1 and asin_ok(r[col])]
        blocks[label] = {"asin": len(items), "sum": round(sum(q for _, q in items)), "nonzero": sum(1 for _, q in items if q > 0)}
    res["pivot_ffbox"] = blocks
    return res


WAREHOUSE_TOTAL_TAB = "Pivot Table warehouse"
WAREHOUSE_BLOCKS_TAB = "pivot Warehouses FFbox"


def _asin_like(v) -> bool:
    v = str(v).strip().upper()
    return len(v) == 10 and v[:2] == "B0" and v.isalnum()


def warehouse_live_from_rows(total_rows: list[list], block_rows: list[list], exclude: tuple[str, ...] = ("DE",)) -> pd.DataFrame:
    """Склад США = итог по ASIN со всех складов (Pivot Table warehouse) минус исключённые блоки FFbox (по умолчанию DE = Германия)."""
    total: dict[str, float] = {}
    for r in total_rows:
        if len(r) > 1 and _asin_like(r[0]):
            total[str(r[0]).strip().upper()] = total.get(str(r[0]).strip().upper(), 0.0) + _num(r[1])
    names = [str(c).strip() for c in block_rows[0]] if block_rows else []
    minus: dict[str, float] = {}
    for col in (0, 3, 6, 9):
        label = names[col].upper().split() if col < len(names) and names[col] else []
        if label and label[-1] in {e.upper() for e in exclude}:
            for r in block_rows:
                if len(r) > col + 1 and _asin_like(r[col]):
                    a = str(r[col]).strip().upper()
                    minus[a] = minus.get(a, 0.0) + _num(r[col + 1])
    qty = {a: max(q - minus.get(a, 0.0), 0.0) for a, q in total.items()}
    return pd.DataFrame({"asin": list(qty), "wrh_live": list(qty.values())})


def apply_live_wrh(sources: pd.DataFrame | None, live: pd.DataFrame) -> tuple[pd.DataFrame | None, str]:
    """Подменяет wrh_qty остатком склада США; ASIN вне сводки получают 0. Не применяем пустой/нулевой результат."""
    live_sum = float(live["wrh_live"].sum()) if not live.empty else 0.0
    if sources is None or live_sum <= 0 or len(live) < 50:
        return sources, "Свежий склад не применён: сводка пуста или слишком мала; склад взят из снимка на начало месяца."
    old = sources.copy()
    old_sum = float(pd.to_numeric(old["wrh_qty"], errors="coerce").fillna(0).sum())
    old["asin"] = old["asin"].astype(str).str.upper()
    m = old.merge(live, on="asin", how="outer")
    for c in m.columns:
        if c not in ("asin", "wrh_live", "wrh_qty"):
            m[c] = m[c].where(m[c].notna(), 0)
    m["wrh_qty"] = m["wrh_live"].fillna(0)
    return m.drop(columns="wrh_live"), (
        f"Склад взят из сводки склада (без DE): {live_sum:,.0f} шт. В снимке на начало месяца было {old_sum:,.0f} шт. "
        "(там к складу добавлен inbound).").replace(",", " ")


def warehouse_block_items(spec: str, credentials_file: str | None = None) -> dict[str, dict[str, float]]:
    """Остатки по ASIN в каждом блоке pivot Warehouses FFbox: {метка блока: {asin: qty}}."""
    rows = _gspread_book(spec, credentials_file).worksheet(WAREHOUSE_BLOCKS_TAB).get(value_render_option="UNFORMATTED_VALUE")
    names = [str(c).strip() for c in rows[0]] if rows else []
    out: dict[str, dict[str, float]] = {}
    for col in (0, 3, 6, 9):
        label = names[col] if col < len(names) and names[col] else f"блок {col}"
        out[label] = {str(r[col]).strip().upper(): _num(r[col + 1]) for r in rows if len(r) > col + 1 and _asin_like(r[col])}
    return out


def velocity_by_market(spec: str, credentials_file: str | None = None) -> pd.DataFrame:
    """Вкладка «today velocity» файла Сергея: скорость продаж по ASIN и рынкам (Market Place)."""
    book = _gspread_book(spec, credentials_file)
    ws = next((w for w in book.worksheets() if w.title.strip().lower() == "today velocity"), None)
    if ws is None:
        raise ValueError("Не нашёл вкладку «today velocity»")
    rows = ws.get(value_render_option="UNFORMATTED_VALUE")
    head = next((i for i, r in enumerate(rows[:10]) if "asin" in [str(c).strip().lower() for c in r]), None)
    if head is None:
        raise ValueError("На вкладке «today velocity» нет шапки с ASIN")
    low = [str(c).strip().lower() for c in rows[head]]
    ia, ivel = low.index("asin"), low.index("velocity")
    im = next(i for i, c in enumerate(low) if c.startswith("market"))
    out = [{"asin": str(r[ia]).strip().upper(), "market": str(r[im]).strip().upper(), "velocity": _num(r[ivel])}
           for r in rows[head + 1:] if len(r) > max(ia, ivel, im) and _asin_like(r[ia])]
    df = pd.DataFrame(out)
    return df.groupby(["asin", "market"], as_index=False)["velocity"].sum() if not df.empty else df


def block_market_shares(blocks: dict[str, dict[str, float]], vel: pd.DataFrame) -> pd.DataFrame:
    """Для каждого склада: доля продаж его ASIN по рынкам, взвешенная остатком (сумма по рынкам = 100%)."""
    if vel.empty:
        return pd.DataFrame()
    piv = vel.pivot_table(index="asin", columns="market", values="velocity", aggfunc="sum", fill_value=0.0)
    share = piv.div(piv.sum(axis=1).replace(0, float("nan")), axis=0)
    rows = []
    for label, items in blocks.items():
        stock = pd.Series({a: q for a, q in items.items() if q > 0})
        both = share.index.intersection(stock.index)
        if len(both) == 0:
            rows.append({"Склад": label, "ASIN с продажами": 0})
            continue
        w = stock[both]
        sh = share.loc[both].mul(w, axis=0).sum() / w[share.loc[both].notna().any(axis=1)].sum()
        rows.append({"Склад": label, "ASIN с продажами": len(both),
                     **{f"доля {m}, %": round(100 * float(v)) for m, v in sh.sort_values(ascending=False).head(5).items()}})
    return pd.DataFrame(rows)


CA_HINT_TABS = ["Fulfillment-BOX CA", "Fulfillment-BOX TX", "Fulfillment-BOX FL", "Fulfillment-BOX DE", "pivot Warehouses FFbox",
                "International Link Logistics 3pl", "sumac3pl(arhive)", "All stock at warehouses", "Pivot Table warehouse", "Scorecard"]
CA_HINT_WORDS = ("canada", "canad", "toronto", "ontario", "vancouver", "calgary", "mississauga", "montreal", "california",
                 "calif", "los angeles", "ontario, ca", "usa", "united states", "amazon.ca", " cad", "can ")


def find_hints(spec: str, tabs: list[str] | None = None, words: tuple[str, ...] = CA_HINT_WORDS,
               credentials_file: str | None = None, rows: int = 400) -> dict:
    """Ищет слова-подсказки (страны, города) в вкладках одним batch-запросом; возвращает совпадения и шапку вкладок."""
    book = _gspread_book(spec, credentials_file)
    titles = [w.title for w in book.worksheets()]
    use = [t for t in (tabs or CA_HINT_TABS) if t in titles]
    ranges = [f"'{t}'!A1:AZ{rows}" for t in use]
    resp = book.values_batch_get(ranges, params={"valueRenderOption": "FORMATTED_VALUE"}) if ranges else {"valueRanges": []}
    found, heads = [], {}
    for t, vr in zip(use, resp.get("valueRanges", [])):
        vals = vr.get("values", [])
        heads[t] = [[str(c).replace("\n", " ")[:20] for c in r[:12]] for r in vals[:3]]
        for ri, r in enumerate(vals):
            for ci, c in enumerate(r):
                low = str(c).lower()
                hit = next((w for w in words if w in low), None)
                if hit and not (len(low) == 10 and low[:2] == "b0"):
                    found.append({"tab": t, "cell": f"R{ri + 1}C{ci + 1}", "word": hit, "text": str(c)[:80]})
        if len(found) > 400:
            break
    counts: dict[str, dict[str, int]] = {}
    for f in found:
        counts.setdefault(f["tab"], {}).setdefault(f["word"], 0)
        counts[f["tab"]][f["word"]] += 1
    return {"tabs_read": use, "counts": counts, "samples": found[:40], "heads": heads}


def tab_preview(spec: str, tab: str, rows: int = 80, credentials_file: str | None = None) -> pd.DataFrame:
    """Первые строки вкладки как есть (без шапки), для просмотра в админской диагностике."""
    book = _gspread_book(spec, credentials_file)
    ws = next((w for w in book.worksheets() if w.title.strip().lower() == tab.strip().lower()), None)
    if ws is None:
        raise ValueError(f"Нет вкладки «{tab}»")
    vals = ws.get(f"A1:AZ{rows}", value_render_option="FORMATTED_VALUE")
    width = max((len(r) for r in vals), default=0)
    df = pd.DataFrame([[str(c) for c in (r + [""] * width)[:width]] for r in vals])
    df.columns = [f"C{i + 1}" for i in range(width)]
    df.index = range(1, len(df) + 1)
    return df.loc[:, (df != "").any(axis=0)] if not df.empty else df
