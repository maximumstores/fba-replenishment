"""Остатки по рынкам US / UK / DE / CA из BigQuery (mt.hopted_*_native, обновляются ночью) + справочник товаров.

Даёт DataFrame в формате restock_report.NORMALIZED, из которого строятся отчёты «Аут сейчас» и «Рест на 2 мес».
Demand 2 мес берём из forecast.plan_sales (план по ASIN) — пока только для US: в плане нет разбивки по рынкам.
Деньги: цены в BigQuery нет, поэтому «потери» считаем по себестоимости группы (forecast.group_cogs), это явно подписано в отчёте.
"""
from __future__ import annotations

import re

import pandas as pd

import loaders
import restock_report as rr

MARKETS = {  # рынок -> (таблица, коды Marketplace Country Code в таблице)
    "US": ("hopted_us_native", None),
    "CA": ("hopted_ca_native", {"CA"}),
    "DE": ("hopted_de_native", {"DE"}),
    "UK": ("hopted_uk_native", {"GB", "UK"}),
}
HEADER_KEY = "Hopted ID"


def _num(v) -> float:
    return rr._clean_number(v)


def parse_hopted(rows: list[list], codes: set[str] | None = None) -> pd.DataFrame:
    """Таблица Hopted как лист: ищем строку заголовков, берём первое вхождение нужных колонок, суммируем по ASIN."""
    start = next((i for i, r in enumerate(rows) if str(r[0]).strip() == HEADER_KEY), None)
    if start is None:
        raise ValueError("не нашёл строку заголовков «Hopted ID»")
    df = loaders._rows_to_df(rows[start:])
    cols = {str(c).strip(): c for c in df.columns}

    def col(name: str):
        return cols.get(name)

    asin, ful = col("ASIN"), col("FBA fulfillable quantity")
    if asin is None or ful is None:
        raise ValueError("нет колонок ASIN / FBA fulfillable quantity")
    if codes and col("Marketplace Country Code") is not None:
        df = df[df[col("Marketplace Country Code")].astype(str).str.strip().str.upper().isin(codes)]
    out = pd.DataFrame({"asin": df[asin].astype(str).str.strip().str.upper(), "available": df[ful].map(_num)})
    # Inbound как у Нины: receiving + shipped + working + Reserved FC Transfer + Reserved FC Processing
    out["inbound"] = sum(df[col(n)].map(_num) if col(n) is not None else 0.0
                         for n in ("FBA inbound receiving quantity", "FBA inbound shipped quantity", "FBA inbound working quantity",
                                   "Reserved FC Transfer", "Reserved FC Processing"))
    out["days_supply"] = df[col("Days of supply")].map(_num) if col("Days of supply") is not None else 0.0
    # Один FNSKU под несколькими SKU внутри аккаунта Amazon показывает одним и тем же остатком в каждой строке: берём максимум; аккаунты потом суммируем
    fn, sku = col("FNSKU"), col("SKU")
    key = (df[fn] if fn is not None else df[sku] if sku is not None else out["asin"]).astype(str).str.strip().str.upper()
    out["fnsku"] = key.where(key.ne("") & key.ne("NAN") & key.ne("NONE"), out["asin"])
    st = col("Store name")
    out["store"] = df[st].astype(str).str.strip() if st is not None else ""
    out = out[out["asin"].str.fullmatch(r"B0[A-Z0-9]{8}")]
    # внутри одного Amazon-аккаунта дубли FNSKU схлопываем, а аккаунты (Store name) суммируем
    out = out.groupby(["store", "asin", "fnsku"], as_index=False).agg(
        available=("available", "max"), inbound=("inbound", "max"), days_supply=("days_supply", "max"))
    return out.groupby("asin", as_index=False).agg(available=("available", "sum"), inbound=("inbound", "sum"),
                                                   days_supply=("days_supply", "max"))


DIM_SQL = """SELECT d.asin, d.old_asin, COALESCE(NULLIF(d.ab_winter_a,''), NULLIF(d.abcd_class,''), '') AS ab, d.category, d.group_key, d.color, d.size,
       c.avg_cogs AS cogs, d.active_us, d.active_ca, d.active_de, d.active_uk
FROM `{p}.forecast.dim_product` d LEFT JOIN `{p}.forecast.group_cogs` c USING (group_key)"""
DEMAND_SQL = """SELECT asin, SUM(plan_units) AS demand_2m FROM `{p}.forecast.plan_sales`
WHERE month >= DATE_TRUNC(CURRENT_DATE(), MONTH) AND month < DATE_ADD(DATE_TRUNC(CURRENT_DATE(), MONTH), INTERVAL 2 MONTH)
GROUP BY asin"""


def merge_old_asins(stock: pd.DataFrame, dim: pd.DataFrame) -> pd.DataFrame:
    """Перелистинг: старый и новый ASIN — один товар. Сток и inbound старого ASIN прибавляем к новому, старый из списка убираем.
    Иначе новый ASIN выглядит как аут, хотя товар лежит под старым (и наоборот — старый шумит ложным аутом)."""
    if "old_asin" not in dim.columns:
        return stock
    pairs = dim[["asin", "old_asin"]].dropna()
    pairs = pairs[(pairs["asin"].str.upper() != "NEW") & (pairs["old_asin"].str.upper() != pairs["asin"].str.upper())]
    pairs = pairs.drop_duplicates("asin")
    if pairs.empty:
        return stock
    s = stock.set_index("asin")
    cols = [c for c in ("available", "inbound") if c in s.columns]
    add = pairs.assign(**{c: pairs["old_asin"].map(s[c]) for c in cols}).dropna(subset=cols, how="all").fillna({c: 0.0 for c in cols})
    add = add[add[cols].sum(axis=1) > 0].groupby("asin", as_index=False)[cols].sum()
    out = stock.copy()
    old_set = set(pairs["old_asin"])
    # прибавляем сток старого к новому
    out = out.set_index("asin")
    for _, r in add.iterrows():
        if r["asin"] in out.index:
            for c in cols:
                out.loc[r["asin"], c] = out.loc[r["asin"], c] + r[c]
        else:
            out.loc[r["asin"], cols] = [r[c] for c in cols]
    out = out.fillna({c: 0.0 for c in out.columns if c != "asin"}).reset_index()
    # старые ASIN, у которых есть новый наследник, в отчёт не идут
    return out[~out["asin"].isin(old_set)].reset_index(drop=True)


def build_market(market: str, stock: pd.DataFrame, dim: pd.DataFrame, demand: pd.DataFrame | None) -> pd.DataFrame:
    """Склеивает остатки, справочник и (если есть) спрос в нормализованный вид. Цена = себестоимость группы (прокси)."""
    stock = merge_old_asins(stock, dim)
    df = stock.merge(dim.drop_duplicates("asin"), on="asin", how="left")
    flag = f"active_{market.lower()}"
    if flag in df.columns:  # в отчёт только ASIN, активные на этом рынке (в Hopted много старых и выведенных)
        df = df[df[flag].fillna(False).astype(bool)]
    df["demand_2m"] = 0.0
    if demand is not None and not demand.empty:
        df = df.merge(demand.drop_duplicates("asin"), on="asin", how="left", suffixes=("", "_p"))
        df["demand_2m"] = df.pop("demand_2m_p").fillna(0.0)
    df["price_eur"] = pd.to_numeric(df.get("cogs"), errors="coerce").fillna(0.0)
    cat = df["group_key"] if "group_key" in df.columns else df["category"]
    out = pd.DataFrame({"market": market, "ab": df["ab"].fillna(""),
                        "category": cat.fillna(df["category"]).fillna("(нет в справочнике)"),
                        "color": df["color"].fillna(""), "size": df["size"].fillna(""), "asin": df["asin"],
                        "available": df["available"], "inbound": df["inbound"], "demand_2m": df["demand_2m"],
                        "lost_eur": 0.0, "price_eur": df["price_eur"]})
    return out[rr.NORMALIZED].reset_index(drop=True)


SOLD_SQL = """SELECT 'US' AS market, asin FROM `{p}.forecast.fact_sales_monthly` WHERE units > 0 GROUP BY 1, 2
UNION DISTINCT
SELECT country AS market, asin FROM `{p}.forecast.summer_src` WHERE kind = 'fact' AND units > 0 GROUP BY 1, 2"""
SOURCES_SQL = "SELECT * FROM `{p}.mt.amazon_starting_balance_native`"
# Orders-Stock: чем можно пополнить FBA. AWD есть только у US и DE.
SOURCE_COLS = {
    "US": {"awd": "AWD US", "wrh": "WRH US+ Inbound", "order_qty": "Order US"},
    "CA": {"wrh": "WRH CA+ Inbound", "order_qty": "Order CA"},
    "DE": {"awd": "AWD DE", "wrh": "WRH DE+ Inbound", "order_qty": "Order DE"},
    "UK": {"wrh": "WRH UK+Inbound", "order_qty": "Order UK"},
}


def parse_sources(rows: list[list]) -> dict[str, pd.DataFrame]:
    """Orders-Stock (amazon_starting_balance): строка шапки — та, где в колонке A стоит «ASIN»; колонки ищем по названию."""
    hi = next((i for i, r in enumerate(rows) if r and str(r[0]).strip().upper() == "ASIN"), None)
    if hi is None:
        return {}
    head = [" ".join(str(c).replace("\xa0", " ").split()).lower() for c in rows[hi]]
    out = {}
    for m, cols in SOURCE_COLS.items():
        idx = {k: next((i for i, h in enumerate(head) if h == v.lower()), None) for k, v in cols.items()}
        recs = []
        for r in rows[hi + 1:]:
            asin = str(r[0]).strip().upper() if r else ""
            if not re.fullmatch(r"B0[A-Z0-9]{8}", asin):
                continue
            recs.append({"asin": asin, **{k: (_num(r[i]) if i is not None and i < len(r) else 0.0) for k, i in idx.items()}})
        df = pd.DataFrame(recs)
        if not df.empty:
            df = df.groupby("asin", as_index=False).sum(numeric_only=True)
        for k in ("awd", "wrh", "order_qty"):
            if k not in df.columns:
                df[k] = 0.0
        out[m] = df
    return out


def add_sources(df: pd.DataFrame, src: pd.DataFrame | None) -> pd.DataFrame:
    """Колонки AWD / склад 3PL / в заказе по ASIN (нет данных → 0)."""
    df = df.copy()
    for k in ("awd", "wrh", "order_qty"):
        df[k] = df["asin"].map(src.set_index("asin")[k]).fillna(0.0) if src is not None and not src.empty else 0.0
    return df


def drop_never_sold(df: pd.DataFrame, sold: set[str], dim: pd.DataFrame) -> pd.DataFrame:
    """Просьба Нины (19.10.2026): новый товар без единой продажи в отчёт не попадает, пока не продастся в первый раз.
    Новый ASIN старого товара (есть old_asin в справочнике с продажами) остаётся — его сток уже сложен со старым."""
    if df is None or df.empty or not sold:
        return df
    old = {}
    if "old_asin" in dim.columns:
        for a, o in zip(dim["asin"], dim["old_asin"]):
            if isinstance(o, str) and o.strip():
                old.setdefault(str(a).strip().upper(), set()).add(o.strip().upper())
    keep = [a in sold or bool(old.get(a, set()) & sold) for a in df["asin"].astype(str).str.upper()]
    return df[keep].reset_index(drop=True)


def category_parent_map(dim: pd.DataFrame) -> dict[str, str]:
    """ASIN → «Category+parent» из справочника (dim_product.group_key, напр. «Mens Sets 320»).
    Старый ASIN (old_asin) получает группу своего наследника — так находятся строки Нины со старыми ASIN."""
    if "group_key" not in dim.columns:
        return {}
    d = dim.dropna(subset=["group_key"])
    d = d[d["group_key"].astype(str).str.strip() != ""]
    m = {str(a).strip().upper(): g for a, g in zip(d["asin"], d["group_key"])}
    if "old_asin" in d.columns:
        for a, g in zip(d["old_asin"], d["group_key"]):
            if isinstance(a, str) and a.strip():
                m.setdefault(a.strip().upper(), g)
    return m


def apply_category_parent(df: pd.DataFrame, cp: dict[str, str]) -> pd.DataFrame:
    """Колонка category = Category+parent из справочника; если ASIN в справочнике нет — оставляем как было."""
    if df is None or df.empty or not cp:
        return df
    df = df.copy()
    df["category"] = df["asin"].astype(str).str.upper().map(cp).fillna(df["category"])
    return df


def load_markets(markets: tuple[str, ...] = ("US", "CA", "DE", "UK")) -> dict[str, pd.DataFrame]:
    p = loaders._project()
    dim = loaders._bq_query(DIM_SQL.format(p=p))
    demand = loaders._bq_query(DEMAND_SQL.format(p=p))
    result = {}
    nina = {}
    want = tuple(m for m in markets if m in ("DE", "UK"))
    if want:  # DE/UK — по вкладкам Нины (план + цена €); при сбое падаем обратно на Hopted и говорим об этом
        try:
            import nina_sheet
            nina = nina_sheet.load_nina(want)
        except Exception as exc:
            print(f"[warn] вкладки Нины недоступны ({exc}); DE/UK считаю по Hopted без плана и цены")
    for m in markets:
        if m in nina:
            result[m] = nina[m]
            continue
        table, codes = MARKETS[m]
        rows = loaders._bq_frame_to_rows(loaders._bq_query(f"SELECT * FROM `{p}.mt.{table}`"))
        result[m] = build_market(m, parse_hopted(rows, codes), dim, demand if m == "US" else None)
        if m == "US":  # план — вкладка Orders Нины (Plan M'YY за 2 мес), остальное (сток, цена) как было
            try:
                import nina_sheet
                us = nina_sheet.load_us_plan().drop_duplicates("asin").set_index("asin")
                result[m]["demand_2m"] = result[m]["asin"].map(us["demand_2m"]).fillna(0.0)
            except Exception as exc:
                print(f"[warn] план US из Нины недоступен ({exc}); US считаю по плану из BigQuery")
        if m == "CA":  # остатки — Hopted, план и цена — таблица Нины по ASIN
            try:
                import nina_sheet
                ca = nina_sheet.load_ca_plan().drop_duplicates("asin").set_index("asin")
                df = result[m]
                df["demand_2m"] = df["asin"].map(ca["demand_2m"]).fillna(0.0)
                df["price_eur"] = df["asin"].map(ca["price_eur"]).fillna(0.0)
            except Exception as exc:
                print(f"[warn] план CA недоступен ({exc}); CA считаю по Hopted без плана")
    # DE/UK из вкладок Нины: перелистинг — сток старого ASIN прибавляем к новому (как у US/CA)
    for m in ("DE", "UK"):
        if m in nina and m in result:
            result[m] = merge_old_asins_report(result[m], dim)
    try:
        sold_df = loaders._bq_query(SOLD_SQL.format(p=p))
        sold = {mk: set(g["asin"].astype(str).str.upper()) for mk, g in sold_df.groupby("market")}
    except Exception as exc:
        print(f"[warn] история продаж недоступна ({exc}); новые ASIN не отфильтрованы")
        sold = {}
    try:
        src = parse_sources(loaders._bq_frame_to_rows(loaders._bq_query(SOURCES_SQL.format(p=p))))
    except Exception as exc:
        print(f"[warn] Orders-Stock недоступен ({exc}); колонки AWD / склад / заказ = 0")
        src = {}
    # по просьбе Нины (19.10.2026): во всех отчётах категория = Category+parent из справочника
    cp = category_parent_map(dim)
    out = {}
    for m, df in result.items():
        df = drop_never_sold(df, sold.get(m, set()), dim)
        out[m] = add_sources(apply_category_parent(df, cp), src.get(m))
    return out


def merge_old_asins_report(df: pd.DataFrame, dim: pd.DataFrame) -> pd.DataFrame:
    """merge_old_asins для готового отчётного вида: складываем available/inbound старого ASIN в новый,
    остальные поля нового ASIN сохраняем; строки старых ASIN убираем."""
    if df is None or df.empty or "old_asin" not in dim.columns:
        return df
    merged = merge_old_asins(df[["asin", "available", "inbound"]], dim).set_index("asin")
    base = df.drop_duplicates("asin").set_index("asin")
    keep = [a for a in merged.index if a in base.index]
    out = base.loc[keep].copy()
    out["available"] = merged.loc[keep, "available"]
    out["inbound"] = merged.loc[keep, "inbound"]
    return out.reset_index()[df.columns]
