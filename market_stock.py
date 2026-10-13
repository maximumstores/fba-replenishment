"""Остатки по рынкам US / UK / DE / CA из BigQuery (mt.hopted_*_native, обновляются ночью) + справочник товаров.

Даёт DataFrame в формате restock_report.NORMALIZED, из которого строятся отчёты «Аут сейчас» и «Рест на 2 мес».
Demand 2 мес берём из forecast.plan_sales (план по ASIN) — пока только для US: в плане нет разбивки по рынкам.
Деньги: цены в BigQuery нет, поэтому «потери» считаем по себестоимости группы (forecast.group_cogs), это явно подписано в отчёте.
"""
from __future__ import annotations

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
    # Один FNSKU под несколькими SKU Amazon показывает одним и тем же остатком в каждой строке: берём максимум, а не сумму
    fn, sku = col("FNSKU"), col("SKU")
    key = (df[fn] if fn is not None else df[sku] if sku is not None else out["asin"]).astype(str).str.strip().str.upper()
    out["fnsku"] = key.where(key.ne("") & key.ne("NAN") & key.ne("NONE"), out["asin"])
    out = out[out["asin"].str.fullmatch(r"B0[A-Z0-9]{8}")]
    out = out.groupby(["asin", "fnsku"], as_index=False).agg(
        available=("available", "max"), inbound=("inbound", "max"), days_supply=("days_supply", "max"))
    return out.groupby("asin", as_index=False).agg(available=("available", "sum"), inbound=("inbound", "sum"),
                                                   days_supply=("days_supply", "max"))


DIM_SQL = """SELECT d.asin, COALESCE(NULLIF(d.ab_winter_a,''), NULLIF(d.abcd_class,''), '') AS ab, d.category, d.color, d.size,
       c.avg_cogs AS cogs, d.active_us, d.active_ca, d.active_de, d.active_uk
FROM `{p}.forecast.dim_product` d LEFT JOIN `{p}.forecast.group_cogs` c USING (group_key)"""
DEMAND_SQL = """SELECT asin, SUM(plan_units) AS demand_2m FROM `{p}.forecast.plan_sales`
WHERE month >= DATE_TRUNC(CURRENT_DATE(), MONTH) AND month < DATE_ADD(DATE_TRUNC(CURRENT_DATE(), MONTH), INTERVAL 2 MONTH)
GROUP BY asin"""


def build_market(market: str, stock: pd.DataFrame, dim: pd.DataFrame, demand: pd.DataFrame | None) -> pd.DataFrame:
    """Склеивает остатки, справочник и (если есть) спрос в нормализованный вид. Цена = себестоимость группы (прокси)."""
    df = stock.merge(dim.drop_duplicates("asin"), on="asin", how="left")
    flag = f"active_{market.lower()}"
    if flag in df.columns:  # в отчёт только ASIN, активные на этом рынке (в Hopted много старых и выведенных)
        df = df[df[flag].fillna(False).astype(bool)]
    df["demand_2m"] = 0.0
    if demand is not None and not demand.empty:
        df = df.merge(demand.drop_duplicates("asin"), on="asin", how="left", suffixes=("", "_p"))
        df["demand_2m"] = df.pop("demand_2m_p").fillna(0.0)
    df["price_eur"] = pd.to_numeric(df.get("cogs"), errors="coerce").fillna(0.0)
    out = pd.DataFrame({"market": market, "ab": df["ab"].fillna(""), "category": df["category"].fillna("(нет в справочнике)"),
                        "color": df["color"].fillna(""), "size": df["size"].fillna(""), "asin": df["asin"],
                        "available": df["available"], "inbound": df["inbound"], "demand_2m": df["demand_2m"],
                        "lost_eur": 0.0, "price_eur": df["price_eur"]})
    return out[rr.NORMALIZED].reset_index(drop=True)


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
    return result
