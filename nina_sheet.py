"""Данные Нины для DE и UK: вкладки «Order DE» / «Orders UK» файла Stock UK-DE (читаются сервисным аккаунтом, Viewer).

Order DE:  A ASIN, C Category, D Color, E Size, H A/B, O Available, N Inbound+FC Transfer+FC Processing,
           Y PLAN REST OCT-NOV (план на 2 мес без прошедшей части), AO Price (€) — цена за штуку.
Orders UK: те же колонки, плана по месяцам P,Q,R (колонки сразу после Available); цены нет → берём цену DE по ASIN (€, ориентир).
Колонки ищем по названию шапки (строка 1), не по букве — чтобы переживать вставку колонок.
"""
from __future__ import annotations

import os
import re

import pandas as pd

import loaders
import restock_report as rr

STOCK_SHEET_DEFAULT = "1XKUoiKUa8aF0DT7pNgXRqEvlejVu_FYVpMRPyGcOlLI"
CA_SHEET_DEFAULT = "1FRP8eWrJRNraSeua1kJn_rki9H4XshUdiKRMw9w8QnA"  # «CA Stock Amazon Merino-Elita»
US_SHEET_DEFAULT = "1acpHlfBiJjFm96VmFksRmKTnktULTgmu7iESYFDLUZw"  # «Stock Amazon Merino» (US), вкладка Orders
TABS = {"DE": "Order DE", "UK": "Orders UK", "CA": "Orders"}  # CA: вкладка «Orders» без пробела (рабочая, с ценой и планом)
ASIN_RE = re.compile(r"B0[A-Z0-9]{8}")


def _n(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    return rr._clean_number(v)


def parse_orders(header: list, rows: list[list], market: str, plan: str = "rest") -> pd.DataFrame:
    """Лист Нины -> NORMALIZED. plan='rest': колонка «PLAN REST OCT-NOV» (DE); 'months': сумма двух колонок после Available (UK)."""
    h = [" ".join(str(c).replace("\xa0", " ").split()).lower() for c in header]

    def idx(name: str, start: int = 0) -> int | None:
        name = name.lower()
        return next((i for i in range(start, len(h)) if h[i] == name), None)

    c_av, c_in = idx("available"), idx("inbound+fc transfer+fc processing")
    if c_av is None or c_in is None:
        raise ValueError(f"{TABS[market]}: нет колонок Available / Inbound в шапке")
    c_cat, c_col, c_sz = idx("category"), idx("color"), idx("size")
    c_ab = next((i for i, x in enumerate(h) if x.startswith("a/b category")), None)
    c_price = idx("price")
    c_rest = idx("plan rest oct-nov")
    if plan == "rest" and c_rest is None:
        raise ValueError(f"{TABS[market]}: нет колонки «PLAN REST OCT-NOV»")

    def cell(r: list, i: int | None):
        return r[i] if i is not None and i < len(r) else ""

    out = []
    for r in rows:
        asin = str(cell(r, 0)).strip().upper()
        if not ASIN_RE.fullmatch(asin):
            continue
        demand = _n(cell(r, c_rest)) if plan == "rest" else _n(cell(r, c_av + 1)) + _n(cell(r, c_av + 2))
        out.append({"market": market, "ab": str(cell(r, c_ab)).strip(), "category": str(cell(r, c_cat)).strip(),
                    "color": str(cell(r, c_col)).strip(), "size": str(cell(r, c_sz)).strip(), "asin": asin,
                    "available": _n(cell(r, c_av)), "inbound": _n(cell(r, c_in)), "demand_2m": max(demand, 0.0),
                    "lost_eur": 0.0, "price_eur": _n(cell(r, c_price))})
    df = pd.DataFrame(out, columns=rr.NORMALIZED)
    # один ASIN может стоять в нескольких строках (старые/новые) — берём строку с большим планом
    df = df.sort_values("demand_2m", ascending=False).drop_duplicates("asin").reset_index(drop=True)
    return df[df["demand_2m"] > 0].reset_index(drop=True)  # без плана ASIN в отчёты не попадает


def _num_text(v) -> float:
    """Число из ячейки, в т.ч. текстом с пробелом тысяч: '2 204' -> 2204 (у Нины часть плана записана текстом)."""
    if isinstance(v, (int, float)):
        return float(v)
    t = re.sub(r"[^0-9.,-]", "", str(v)).replace(",", ".")
    try:
        return float(t) if t not in ("", "-", ".") else 0.0
    except ValueError:
        return 0.0


def parse_us_plan(header: list, rows: list[list], today=None) -> pd.DataFrame:
    """US, вкладка Orders: план на 2 мес = колонки «Plan M'YY» текущего и следующего месяца (ищем по шапке).
    Один ASIN в нескольких строках (старый/новый) — берём большее значение."""
    from datetime import date

    today = today or date.today()
    want = []
    y, m = today.year, today.month
    for _ in range(2):
        want.append((m, y % 100))
        m, y = (1, y + 1) if m == 12 else (m + 1, y)
    pat = re.compile(r"^plan(\d{1,2})'(\d{2})$")
    cols = {}
    for i, c in enumerate(header):
        mt = pat.match(re.sub(r"\s+", "", str(c).replace("\xa0", " ")).lower())
        if mt:
            cols[(int(mt.group(1)), int(mt.group(2)))] = i
    missing = [k for k in want if k not in cols]
    if missing:
        raise ValueError(f"Orders (US): нет колонок плана за {missing}")
    plan: dict[str, float] = {}
    for r in rows:
        asin = str(r[0]).strip().upper() if r else ""
        if not ASIN_RE.fullmatch(asin):
            continue
        v = sum(_num_text(r[cols[k]]) if cols[k] < len(r) else 0.0 for k in want)
        plan[asin] = max(plan.get(asin, 0.0), v)
    return pd.DataFrame({"asin": list(plan), "demand_2m": [max(v, 0.0) for v in plan.values()]})


def load_us_plan() -> pd.DataFrame:
    book = loaders._gspread_book("sheet:" + os.getenv("NINA_US_SHEET", US_SHEET_DEFAULT))
    header, rows = read_tab(book, "Orders")
    return parse_us_plan(header, rows)


def read_tab(book, tab: str) -> tuple[list, list[list]]:
    sheets = book.worksheets()
    ws = next((w for w in sheets if w.title == tab), None) or \
        next((w for w in sheets if loaders._norm_tab(w.title) == loaders._norm_tab(tab)), None)
    if ws is None:
        raise ValueError(f"Нет вкладки «{tab}» в файле Нины")
    header = ws.get("A1:AZ1", value_render_option="FORMATTED_VALUE")[0]
    rows = ws.get("A2:AZ20000", value_render_option="UNFORMATTED_VALUE")
    return header, rows


def load_nina(markets: tuple[str, ...] = ("DE", "UK")) -> dict[str, pd.DataFrame]:
    book = loaders._gspread_book("sheet:" + os.getenv("NINA_STOCK_SHEET", STOCK_SHEET_DEFAULT))
    result: dict[str, pd.DataFrame] = {}
    for m in markets:
        header, rows = read_tab(book, TABS[m])
        result[m] = parse_orders(header, rows, m, plan="rest" if m == "DE" else "months")
    if "UK" in result:  # у UK нет цены: берём цену DE по ASIN
        de = result.get("DE")
        if de is None:
            de = parse_orders(*read_tab(book, TABS["DE"]), "DE")
        price = de.drop_duplicates("asin").set_index("asin")["price_eur"]
        result["UK"]["price_eur"] = result["UK"]["asin"].map(price).fillna(0.0)
    return result


def load_ca_plan() -> pd.DataFrame:
    """CA: план на 2 мес (Oct+Nov — две колонки после Available) и цена по ASIN из вкладки «Orders» файла CA Stock Amazon Merino-Elita.
    Остатки CA берём из Hopted (так решил Сергей), из таблицы Нины — только план и цена."""
    book = loaders._gspread_book("sheet:" + os.getenv("NINA_CA_SHEET", CA_SHEET_DEFAULT))
    header, rows = read_tab(book, TABS["CA"])
    return parse_orders(header, rows, "CA", plan="months")[["asin", "demand_2m", "price_eur"]]
