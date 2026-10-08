"""Расчёт: что и когда кончится на FBA, чем пополнять, сколько слать.

Идея: по каждой паре ASIN × магазин моделируем остаток по дням.
Остаток сегодня (+ то, что уже едет на FBA, с датами прихода) минус скорость продаж.
Первый день, когда товара не хватает на целый день продаж, = день обнуления.
Потом смотрим, какие каналы пополнения (AWD / склад / воздух) успевают до этого дня.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from config import CHANNELS, Settings

REAL_ASIN = re.compile(r"^B[0-9A-Z]{9}$")

# Чем меньше число, тем срочнее
STATUS_RANK = {"OUT": 0, "CRITICAL": 1, "URGENT": 2, "ACTION": 3, "PLAN": 4, "OK": 5, "NO_SALES": 6}
STATUS_RU = {
    "OUT": "Аут сейчас",
    "CRITICAL": "Не успеть ничем",
    "URGENT": "Только воздух",
    "ACTION": "Склад или воздух",
    "PLAN": "Успевает AWD",
    "OK": "Ок",
    "NO_SALES": "Нет продаж",
}
ALERT_STATUSES = ("OUT", "CRITICAL", "URGENT")

# Колонки Hopted (лист US V2, диапазон A:T). Ищем по названию, не по позиции.
HOPTED_COLUMNS = {
    "asin": "ASIN",
    "store": "Store name",
    "sku": "SKU",
    "name": "Product name",
    "snapshot": "Inventory age snapshot date",
    "fulfillable": "FBA fulfillable quantity",
    "inb_receiving": "FBA inbound receiving quantity",
    "inb_shipped": "FBA inbound shipped quantity",
    "inb_working": "FBA inbound working quantity",
    "res_transfer": "Reserved FC Transfer",
    "res_processing": "Reserved FC Processing",
    "reserved": "FBA reserved quantity",
    "units7": "Units shipped last 7 days",
    "units30": "Units shipped last 30 days",
    "total": "FBA total quantity",
}
HOPTED_REQUIRED = ("asin", "fulfillable", "inb_receiving", "inb_shipped", "inb_working", "units7", "units30")
HOPTED_NUMERIC = (
    "fulfillable", "inb_receiving", "inb_shipped", "inb_working",
    "res_transfer", "res_processing", "reserved", "units7", "units30", "total",
)


# ───────────────────────── подготовка данных ─────────────────────────

def to_number(s: pd.Series) -> pd.Series:
    """Числа из таблиц приходят строками ('1,234', '1 234', ''). Пустое и мусор = 0."""
    if s.dtype.kind in "iuf":
        return s.fillna(0).astype(float)
    cleaned = (
        s.astype(str)
        .str.replace(" ", "", regex=False)
        .str.replace(" ", "", regex=False)
        .str.replace(",", "", regex=False)
        .str.strip()
    )
    return pd.to_numeric(cleaned, errors="coerce").fillna(0.0)


def parse_dates(s: pd.Series) -> pd.Series:
    """Даты как '04.10.2026' (день первым) или серийные номера Google Sheets."""
    num = pd.to_numeric(s, errors="coerce")
    out = pd.to_datetime(s.where(num.isna()), dayfirst=True, errors="coerce", format="mixed")
    serial = pd.to_datetime(num.dropna(), unit="D", origin="1899-12-30", errors="coerce")
    out = out.copy()
    out.loc[serial.index] = serial
    return out


def prepare_hopted(raw: pd.DataFrame) -> pd.DataFrame:
    """Сырой лист US V2 → одна строка на (ASIN, магазин), числа числами."""
    by_lower = {str(c).strip().lower(): c for c in raw.columns}
    missing = [HOPTED_COLUMNS[k] for k in HOPTED_REQUIRED if HOPTED_COLUMNS[k].lower() not in by_lower]
    if missing:
        raise ValueError("В данных Hopted нет колонок: " + ", ".join(missing))

    df = pd.DataFrame(index=raw.index)
    for key, title in HOPTED_COLUMNS.items():
        src = by_lower.get(title.lower())
        df[key] = raw[src] if src is not None else (0 if key in HOPTED_NUMERIC else "")
    for key in HOPTED_NUMERIC:
        df[key] = to_number(df[key])
    df["asin"] = df["asin"].astype(str).str.strip().str.upper()
    df = df[df["asin"].ne("") & df["asin"].ne("NAN")].copy()
    df["store"] = df["store"].astype(str).str.strip().replace({"": "—", "nan": "—"})
    df["snapshot"] = parse_dates(df["snapshot"])
    df["sku"] = df["sku"].astype(str)
    df["name"] = df["name"].astype(str)

    agg = {k: "sum" for k in HOPTED_NUMERIC}
    agg.update(sku="first", name="first", snapshot="max")
    out = df.groupby(["asin", "store"], as_index=False).agg(agg)
    out["n_rows"] = df.groupby(["asin", "store"]).size().to_numpy()
    out["real_asin"] = out["asin"].map(lambda a: bool(REAL_ASIN.match(a)))
    return out


def prepare_reference(raw: pd.DataFrame) -> pd.DataFrame:
    """Справочник из Sales Planner (dim_product) → одна строка на ASIN."""
    cols = {str(c).strip().lower(): c for c in raw.columns}
    if "asin" not in cols:
        raise ValueError("В справочнике нет колонки asin")
    ref = pd.DataFrame({"asin": raw[cols["asin"]].astype(str).str.strip().str.upper()})
    for name in ("parent_group", "category", "color", "size", "group_key", "old_asin"):
        ref[name] = raw[cols[name]].astype(str).str.strip() if name in cols else ""
        ref[name] = ref[name].replace({"nan": "", "None": ""})
    # abcd_class у планера сломан (#N/A) — храним как справку, в расчёте не используем
    ref["abcd_planner"] = raw[cols["abcd_class"]].astype(str) if "abcd_class" in cols else ""
    if "active_us" in cols:
        ref["active_us"] = raw[cols["active_us"]].astype(str).str.strip().str.lower().isin(("true", "1", "yes", "да"))
    else:
        ref["active_us"] = True
    if "plan_units_month" in cols:
        ref["plan_units_month"] = to_number(raw[cols["plan_units_month"]])
    else:
        ref["plan_units_month"] = np.nan
    ref = ref[ref["asin"].ne("")]
    return ref.drop_duplicates("asin", keep="first").reset_index(drop=True)


def prepare_sources(raw: pd.DataFrame | None) -> pd.DataFrame | None:
    """Опционально: сколько товара доступно для отправки с AWD и со склада (колонки asin, awd_qty, wrh_qty)."""
    if raw is None or raw.empty:
        return None
    cols = {str(c).strip().lower(): c for c in raw.columns}
    if "asin" not in cols:
        raise ValueError("В файле остатков каналов нет колонки asin")
    out = pd.DataFrame({"asin": raw[cols["asin"]].astype(str).str.strip().str.upper()})
    out["awd_qty"] = to_number(raw[cols["awd_qty"]]) if "awd_qty" in cols else np.nan
    out["wrh_qty"] = to_number(raw[cols["wrh_qty"]]) if "wrh_qty" in cols else np.nan
    out["order_qty"] = to_number(raw[cols["order_qty"]]) if "order_qty" in cols else np.nan  # заказано в производстве, без дат
    return out.groupby("asin", as_index=False).sum(min_count=1)


def prepare_batches(raw: pd.DataFrame | None, today: date, s: Settings) -> dict[str, list[tuple[int, float]]]:
    """Опционально: партии, которые едут сразу на FBA (колонки asin, qty, eta, destination).

    Берём только destination = AMZ. Дата в Orders-Stock — крайний срок приёмки и не раньше сегодня,
    задержки там не видны, поэтому к дате добавляем batch_delay_days.
    """
    if raw is None or raw.empty:
        return {}
    cols = {str(c).strip().lower(): c for c in raw.columns}
    for need in ("asin", "qty", "eta"):
        if need not in cols:
            raise ValueError(f"В файле партий нет колонки {need}")
    df = pd.DataFrame({
        "asin": raw[cols["asin"]].astype(str).str.strip().str.upper(),
        "qty": to_number(raw[cols["qty"]]),
        "eta": parse_dates(raw[cols["eta"]]),
        "dest": raw[cols["destination"]].astype(str).str.strip().str.upper() if "destination" in cols else "AMZ",
    })
    df = df[(df["dest"] == "AMZ") & (df["qty"] > 0) & df["eta"].notna()]
    out: dict[str, list[tuple[int, float]]] = {}
    for r in df.itertuples():
        days_to_eta = max((r.eta.date() - today).days, 0)
        out.setdefault(r.asin, []).append((int(math.ceil(days_to_eta + s.batch_delay_days)), float(r.qty)))
    return out


# ───────────────────────── модель ─────────────────────────

@dataclass(frozen=True)
class SimResult:
    stockout_day: int | None   # день (0 = сегодня), когда товара не хватает на целый день продаж
    lost_30: float             # недопродано штук за ближайшие 30 дней, если ничего не делать
    lost_horizon: float        # то же за весь горизонт


def simulate(on_hand: float, velocity: float, arrivals: list[tuple[float, float]], horizon: int = 90) -> SimResult:
    """Остаток по дням. arrivals = [(день прихода, штук)], приход добавляется утром этого дня."""
    if velocity <= 0:
        return SimResult(None, 0.0, 0.0)
    by_day: dict[int, float] = {}
    for day, qty in arrivals:
        d = max(0, int(math.ceil(day)))
        by_day[d] = by_day.get(d, 0.0) + float(qty)
    level = max(float(on_hand), 0.0)
    stockout_day = None
    lost30 = lost = 0.0
    for d in range(horizon):
        level += by_day.get(d, 0.0)
        if level < velocity:
            if stockout_day is None:
                stockout_day = d
            shortfall = velocity - level
            lost += shortfall
            if d < 30:
                lost30 += shortfall
            level = 0.0
        else:
            level -= velocity
    return SimResult(stockout_day, lost30, lost)


def _available(qty) -> bool:
    """None/NaN = не знаем (считаем доступным), число > 0 = есть, 0 = нет."""
    return qty is None or (isinstance(qty, float) and math.isnan(qty)) or qty > 0


def pick_channel(stockout_day: int, leads: dict[str, float], avail: dict[str, float | None]) -> tuple[str | None, bool]:
    """Самый дешёвый канал, который успевает до обнуления. Нет такого — самый быстрый из доступных."""
    for ch in CHANNELS:
        if leads[ch] <= stockout_day and _available(avail.get(ch)):
            return ch, True
    candidates = [ch for ch in CHANNELS if _available(avail.get(ch))]
    if not candidates:
        return None, False
    return min(candidates, key=lambda c: leads[c]), False


def classify(velocity: float, stockout_day: int | None, channel: str | None, in_time: bool, s: Settings) -> str:
    if velocity <= 0:
        return "NO_SALES"
    if stockout_day is None:
        return "OK"
    if stockout_day == 0:
        return "OUT"
    if not in_time:
        return "CRITICAL"
    if channel == "AIR":
        return "URGENT"
    if channel == "WAREHOUSE":
        return "ACTION"
    if channel == "SEA":
        return "PLAN" if stockout_day <= s.lead_sea + s.plan_buffer_days else "OK"
    return "PLAN" if stockout_day <= s.lead_awd + s.plan_buffer_days else "OK"


def _row_velocity(units7: float, units30: float, plan_month: float, s: Settings) -> tuple[float, str]:
    fact = s.weight_7d * (units7 / 7.0) + (1.0 - s.weight_7d) * (units30 / 30.0)
    plan = (plan_month / 30.0) if plan_month == plan_month and plan_month > 0 else 0.0  # NaN-safe
    if s.plan_mode == "floor" and plan > fact:
        return plan, "план"
    if s.plan_mode == "fallback" and fact <= 0 and plan > 0:
        return plan, "план"
    return fact, "факт"


def assign_abc(df: pd.DataFrame, s: Settings) -> pd.Series:
    """ABC по продажам за 30 дней (сумма по ASIN). A = первые 80% продаж, B = до 95%, C = остальное, D = нет продаж."""
    per_asin = df.groupby("asin")["units30"].sum().sort_values(ascending=False)
    total = per_asin.sum()
    cls = pd.Series("D", index=per_asin.index)
    if total > 0:
        cum_before = per_asin.cumsum() - per_asin  # доля ДО текущего ASIN: самый первый всегда попадает в A
        share = cum_before / total
        sold = per_asin > 0
        cls[sold & (share < s.abc_a)] = "A"
        cls[sold & (share >= s.abc_a) & (share < s.abc_b)] = "B"
        cls[sold & (share >= s.abc_b)] = "C"
    return df["asin"].map(cls)


def build_report(
    hopted: pd.DataFrame,
    reference: pd.DataFrame | None = None,
    sources: pd.DataFrame | None = None,
    batches: pd.DataFrame | None = None,
    settings: Settings | None = None,
    today: date | None = None,
) -> pd.DataFrame:
    s = settings or Settings()
    today = today or date.today()
    h = prepare_hopted(hopted)
    return _build_report(h, reference, sources, batches, s, today)


REPORT_COLUMNS = [
    "asin", "store", "sku", "name", "snapshot", "parent_group", "color", "size", "abc",
    "status", "status_ru", "status_rank", "on_hand", "inbound_counted", "velocity", "velocity_source",
    "coverage_days_now", "next_arrival_day", "stockout_day", "stockout_date", "channel", "channel_in_time",
    "lead_days", "gap_days", "qty_need", "lost_30", "units7", "units30", "awd_qty", "wrh_qty", "order_qty",
]


def _build_report(h: pd.DataFrame, reference, sources, batches, s: Settings, today: date) -> pd.DataFrame:
    if reference is not None:
        ref = prepare_reference(reference)
        df = h.merge(ref, on="asin", how="left")
        df["in_reference"] = df["parent_group"].notna()
        df["active_us"] = df["active_us"].where(df["in_reference"], True).astype(bool)
        if s.only_active:
            df = df[df["active_us"]].copy()
    else:
        df = h.copy()
        df["in_reference"] = False
        for c in ("parent_group", "category", "color", "size", "group_key", "old_asin", "abcd_planner"):
            df[c] = ""
        df["plan_units_month"] = np.nan
        df["active_us"] = True
    for c in ("parent_group", "category", "color", "size", "group_key", "old_asin", "abcd_planner"):
        df[c] = df[c].fillna("")
    df["parent_group"] = df["parent_group"].replace("", "(нет в справочнике)")

    if df.empty:
        return pd.DataFrame(columns=REPORT_COLUMNS)

    src = prepare_sources(sources)
    if src is not None:
        df = df.merge(src, on="asin", how="left")
    else:
        df["awd_qty"] = np.nan
        df["wrh_qty"] = np.nan
        df["order_qty"] = np.nan
    batch_map = prepare_batches(batches, today, s)

    df["abc"] = assign_abc(df, s)

    # Партии из Orders-Stock привязываем к одной строке ASIN (с наибольшими продажами), чтобы не удвоить
    batch_owner: dict[str, int] = {}
    for idx, r in df.sort_values("units30", ascending=False).iterrows():
        batch_owner.setdefault(r["asin"], idx)

    # План задан на ASIN, а строки идут по магазинам: плановую скорость отдаём одной строке (где больше продаж и стока),
    # иначе один и тот же план посчитается в каждом магазине
    plan_owner: dict[str, int] = {}
    for idx, r in df.assign(_k=df["units30"] * 1e6 + df["fulfillable"] + df["inb_receiving"] + df["inb_shipped"]) \
            .sort_values("_k", ascending=False).iterrows():
        plan_owner.setdefault(r["asin"], idx)

    leads = s.leads
    rows = []
    for idx, r in df.iterrows():
        plan_month = r["plan_units_month"] if plan_owner.get(r["asin"]) == idx else np.nan
        velocity, vsrc = _row_velocity(r["units7"], r["units30"], plan_month, s)
        extra = r["res_transfer"] + r["res_processing"]
        on_hand = r["fulfillable"] + min(extra, r["reserved"]) if r["reserved"] > 0 else r["fulfillable"]

        arrivals = [(s.receiving_days, r["inb_receiving"]), (s.shipped_days, r["inb_shipped"])]
        counted = r["inb_receiving"] + r["inb_shipped"]
        if s.include_working:
            arrivals.append((s.working_days, r["inb_working"]))
            counted += r["inb_working"]
        batch_qty = 0.0
        if batch_owner.get(r["asin"]) == idx:
            for day, qty in batch_map.get(r["asin"], []):
                arrivals.append((day, qty))
                batch_qty += qty
        counted += batch_qty

        sim = simulate(on_hand, velocity, arrivals, s.horizon_days)
        # море и воздух едут с фабрики: нужен заказ в производстве (если про заказы данных нет — считаем доступными)
        avail = {"AWD": r["awd_qty"], "WAREHOUSE": r["wrh_qty"], "SEA": r["order_qty"], "AIR": r["order_qty"]}
        if sim.stockout_day is None:
            channel, in_time = None, True
        else:
            channel, in_time = pick_channel(sim.stockout_day, leads, avail)
        status = classify(velocity, sim.stockout_day, channel, in_time, s)

        gap_days = 0.0
        lead = leads[channel] if channel else min(leads.values())
        if sim.stockout_day is not None and not in_time:
            gap_days = max(lead - sim.stockout_day, 0.0)

        positive = [max(int(math.ceil(d)), 0) for d, q in arrivals if q > 0]
        next_arrival = min(positive) if positive else None

        qty_need = 0.0
        if velocity > 0 and sim.stockout_day is not None:
            # хватит ли на target_cover_days после прихода нового товара
            total_need = velocity * (lead + s.target_cover_days) - (on_hand + counted)
            # и закрываем дыру между приходом нового товара и ближайшим inbound (если он едет позже)
            bridge = 0.0
            start = max(lead, sim.stockout_day)
            if next_arrival is not None and next_arrival > start:
                bridge = velocity * (next_arrival - start)
            qty_need = float(math.ceil(max(total_need, bridge, 0.0)))

        rows.append({
            "status": status,
            "status_ru": STATUS_RU[status],
            "on_hand": on_hand,
            "inbound_counted": counted,
            "velocity": velocity,
            "velocity_source": vsrc,
            "coverage_days_now": (on_hand / velocity) if velocity > 0 else np.nan,
            "next_arrival_day": next_arrival,
            "stockout_day": sim.stockout_day,
            "stockout_date": (today + timedelta(days=sim.stockout_day)) if sim.stockout_day is not None else pd.NaT,
            "channel": channel or "",
            "channel_in_time": in_time,
            "lead_days": lead if channel else np.nan,
            "gap_days": gap_days,
            "qty_need": qty_need,
            "lost_30": sim.lost_30,
            "stock_known": bool(not (pd.isna(r["awd_qty"]) and pd.isna(r["wrh_qty"]))),
        })
    out = pd.concat([df.reset_index(drop=True), pd.DataFrame(rows)], axis=1)
    out["stockout_date"] = pd.to_datetime(out["stockout_date"])
    out["status_rank"] = out["status"].map(STATUS_RANK)
    out = out.sort_values(["status_rank", "lost_30", "units30"], ascending=[True, False, False]).reset_index(drop=True)
    return out


# ───────────────────────── сводки ─────────────────────────

def summarize_groups(report: pd.DataFrame) -> pd.DataFrame:
    """Группа → цвет → размер: сколько ASIN, что на складе, что нужно слать, самый плохой статус."""
    if report.empty:
        return report
    g = report.groupby(["parent_group", "color", "size"], as_index=False).agg(
        asins=("asin", "nunique"),
        worst_rank=("status_rank", "min"),
        on_hand=("on_hand", "sum"),
        inbound=("inbound_counted", "sum"),
        velocity=("velocity", "sum"),
        qty_need=("qty_need", "sum"),
        lost_30=("lost_30", "sum"),
        first_stockout=("stockout_date", "min"),
    )
    inv = {v: k for k, v in STATUS_RANK.items()}
    g["status"] = g["worst_rank"].map(inv)
    g["status_ru"] = g["status"].map(STATUS_RU)
    return g.sort_values(["worst_rank", "lost_30"], ascending=[True, False]).reset_index(drop=True)


def data_quality(hopted_prepared: pd.DataFrame, reference: pd.DataFrame | None) -> dict:
    """Что не так с входными данными (hopted_prepared — результат prepare_hopted)."""
    q = {
        "hopted_asin_store_rows": int(len(hopted_prepared)),
        "hopted_unique_asins": int(hopted_prepared["asin"].nunique()),
        "not_real_asin": hopted_prepared.loc[~hopted_prepared["real_asin"], "asin"].tolist(),
        "duplicated_rows": int((hopted_prepared["n_rows"] > 1).sum()),
        "not_in_reference": [],
        "reference_active_missing_in_hopted": [],
        "reference_without_color_or_size": 0,
        "no_abc_planner": 0,
    }
    if reference is not None:
        ref = prepare_reference(reference)
        known = set(ref["asin"])
        q["not_in_reference"] = sorted(set(hopted_prepared["asin"]) - known)
        active = ref[ref["active_us"]]
        q["reference_active_missing_in_hopted"] = sorted(set(active["asin"]) - set(hopted_prepared["asin"]))
        q["reference_without_color_or_size"] = int(((active["color"] == "") | (active["size"] == "")).sum())
        bad = ~active["abcd_planner"].str.match(r"^[ABCD]", na=False)
        q["no_abc_planner"] = int(bad.sum())
    return q
