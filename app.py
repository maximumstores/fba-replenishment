"""Дашборд «Что пополнять на FBA (США)».

    streamlit run app.py
"""
from __future__ import annotations

import os
from dataclasses import asdict, replace
from datetime import date

import pandas as pd
import streamlit as st

import calc
import loaders
import transit
from config import CHANNEL_RU, Settings

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

LIVE_DEFAULTS = {
    "HOPTED_SOURCE": "bq", "SOURCES_SOURCE": "bq", "INCOMING_SOURCE": "bq", "REFERENCE_SOURCE": "bq",
    "SHIPMENTS_SOURCE": "sheet:128q44-RfvoJSLOHMHT-RaYwJMMJKleCvuQxI9uLgGfk",
    "BQ_PROJECT": "reorder-497714", "USE_PLANNER_INCOMING": "false", "PLAN_MODE": "off",
}
SECRETS_STATUS = {"found": False, "keys": 0, "sa": False}


def _bridge_secrets() -> None:
    """Streamlit Cloud: значения из Secrets → переменные окружения (если не заданы), ключ SA → GOOGLE_SERVICE_ACCOUNT_JSON."""
    try:
        secrets = dict(st.secrets)
    except Exception:  # secrets.toml нет (локальный запуск) — читаем только .env
        return
    SECRETS_STATUS["found"] = bool(secrets)
    sa = secrets.pop("gcp_service_account", None)
    SECRETS_STATUS["sa"] = sa is not None
    if sa is not None:  # источники — не секрет: если есть ключ, читаем боевые данные по умолчанию
        for k, v in LIVE_DEFAULTS.items():
            os.environ.setdefault(k, v)
    if sa is not None and not os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"):
        import json
        os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = json.dumps(dict(sa))
    for k, v in secrets.items():
        if isinstance(v, (str, int, float, bool)) and k not in os.environ:
            os.environ[k] = str(v)
    SECRETS_STATUS["keys"] = len(secrets)


_bridge_secrets()
st.set_page_config(page_title="FBA США: пополнение", layout="wide")
def _require_login() -> None:
    """Вход через Google только для сотрудников домена. Включается, когда в Secrets есть раздел [auth]."""
    try:
        enabled = "auth" in st.secrets
    except Exception:
        enabled = False
    if not enabled:
        return
    if not st.user.is_logged_in:
        st.title("FBA США: что пополнять")
        st.info("Вход только для сотрудников компании (аккаунт Google).")
        st.button("Войти через Google", on_click=st.login, type="primary")
        st.stop()
    domain = os.getenv("ALLOWED_EMAIL_DOMAIN", "maximumstores.online").strip().lower()
    email = str(st.user.get("email", "")).strip().lower()
    if not (email.endswith("@" + domain) and st.user.get("email_verified", True)):
        st.error(f"Доступ только для адресов @{domain}. Вы вошли как {email or 'неизвестный аккаунт'}.")
        st.button("Выйти", on_click=st.logout)
        st.stop()
    st.sidebar.caption(f"Вы вошли: {email}")
    st.sidebar.button("Выйти", on_click=st.logout)


_require_login()
if not os.getenv("HOPTED_SOURCE"):
    st.warning(
        "Сейчас показаны ДЕМО-данные: переменная HOPTED_SOURCE не задана. "
        f"Secrets найдены: {'да' if SECRETS_STATUS['found'] else 'нет'}, "
        f"параметров: {SECRETS_STATUS['keys']}, ключ сервисного аккаунта: {'да' if SECRETS_STATUS['sa'] else 'нет'}. "
        "Проверьте Manage app → Settings → Secrets (должен быть сохранён блок с HOPTED_SOURCE = \"bq\").")

STATUS_COLORS = {
    "OUT": "#e5484d", "CRITICAL": "#e5484d", "URGENT": "#f76b15", "ACTION": "#f5a524",
    "PLAN": "#3e63dd", "OK": "#30a46c", "NO_SALES": "#8b8d98",
}
RU_COLUMNS = {
    "status_ru": "Статус", "abc": "ABC", "parent_group": "Группа", "color": "Цвет", "size": "Размер",
    "asin": "ASIN", "store": "Магазин", "on_hand": "Сток FBA", "inbound_counted": "Едет на FBA",
    "velocity": "Продаж/дн", "coverage_days_now": "Дней запаса", "stockout_date": "Кончится",
    "channel_ru": "Канал", "gap_days": "Дыра, дн", "qty_need": "Слать, шт", "lost_30": "Потери 30 дн, шт",
}


@st.cache_data(ttl=900, show_spinner="Читаю данные…")
def get_inputs() -> dict:
    return loaders.load_all()


@st.cache_data(show_spinner="Считаю…")
def get_report(loaded_at, settings_key: tuple, today: str, use_incoming: bool, _data: dict) -> pd.DataFrame:
    """Кэш по (время загрузки, допущения, дата): фильтры не пересчитывают модель. _data не хэшируется."""
    batches = _data["batches"]
    if batches is None and use_incoming:
        batches = _data["incoming"]
    return calc.build_report(_data["hopted"], _data["reference"], _data["sources"], batches,
                             Settings(**dict(settings_key)))


def settings_sidebar(data: dict) -> tuple[Settings, bool]:
    base = transit.apply_fact_leads(Settings.from_env(), data.get("transit"), os.getenv("LEAD_SEA"))
    st.sidebar.header("Допущения")
    st.sidebar.caption("Срок «Море» взят по фактическим отправкам, остальные сроки — допущения (см. вкладку «Источники»).")
    lead_awd = st.sidebar.number_input("AWD → FBA, дней (допущение)", 1.0, 90.0, base.lead_awd, 1.0)
    lead_wh = st.sidebar.number_input("Склад → FBA, дней (допущение)", 1.0, 90.0, base.lead_warehouse, 1.0)
    lead_sea = st.sidebar.number_input(
        "Море, дней" + (" (факт, медиана)" if data.get("transit") else " (допущение)"), 1.0, 120.0, base.lead_sea, 1.0)
    lead_air = st.sidebar.number_input("Воздух, дней (допущение, ещё не возили)", 1.0, 90.0, base.lead_air, 1.0)
    shipped = st.sidebar.number_input("Едущий на FBA (shipped) придёт через, дней", 0.0, 90.0, base.shipped_days, 1.0)
    target = st.sidebar.number_input("Пополнять на, дней продаж", 7.0, 180.0, base.target_cover_days, 1.0)
    w7 = st.sidebar.slider("Вес продаж за 7 дн (остальное — за 30)", 0.0, 1.0, base.weight_7d, 0.05)
    working = st.sidebar.checkbox("Считать поставки в статусе working (не отправлены)", base.include_working)
    plan_mode = st.sidebar.selectbox(
        "Скорость из прогноза планера", ["fallback", "off", "floor"],
        index=["fallback", "off", "floor"].index(base.plan_mode if base.plan_mode in ("fallback", "off", "floor") else "off"),
        help="fallback: только если продаж нет (товар в ауте); floor: не ниже плана; off: не использовать",
    )
    use_incoming = False
    if data.get("incoming") is not None and data.get("batches") is None:
        use_incoming = st.sidebar.checkbox(
            "Учитывать приходы из планировщика", os.getenv("USE_PLANNER_INCOMING", "").lower() in ("1", "true", "yes"),
            help="Приходы по месяцам, сразу по всем направлениям (AWD, склад, FBA), поэтому оценка оптимистична")
    return replace(base, lead_awd=lead_awd, lead_warehouse=lead_wh, lead_sea=lead_sea, lead_air=lead_air,
                   shipped_days=shipped, target_cover_days=target, weight_7d=w7, include_working=working,
                   plan_mode=plan_mode), use_incoming


def style_status(df: pd.DataFrame):
    def color(v):
        key = next((k for k, ru in calc.STATUS_RU.items() if ru == v), None)
        return f"background-color: {STATUS_COLORS[key]}22; color: {STATUS_COLORS[key]}; font-weight: 600" if key else ""
    return df.style.map(color, subset=["Статус"] if "Статус" in df.columns else [])


def show_table(df: pd.DataFrame, columns: list[str], key: str):
    view = df.assign(channel_ru=df["channel"].map(CHANNEL_RU).fillna("—") if "channel" in df else "—")
    view = view[[c for c in columns if c in view.columns]].rename(columns=RU_COLUMNS)
    st.dataframe(
        style_status(view), width="stretch", hide_index=True, key=key,
        column_config={
            "Продаж/дн": st.column_config.NumberColumn(format="%.1f"),
            "Дней запаса": st.column_config.NumberColumn(format="%.0f"),
            "Кончится": st.column_config.DateColumn(format="DD.MM.YYYY"),
            "Сток FBA": st.column_config.NumberColumn(format="%d"),
            "Едет на FBA": st.column_config.NumberColumn(format="%d"),
            "Слать, шт": st.column_config.NumberColumn(format="%d"),
            "Потери 30 дн, шт": st.column_config.NumberColumn(format="%d"),
            "Дыра, дн": st.column_config.NumberColumn(format="%d"),
        },
    )
    return view


def csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False, sep=";", date_format="%d.%m.%Y").encode("utf-8-sig")  # ; и BOM — чтобы Excel открыл


def show_sources(data: dict, settings: Settings, use_incoming: bool) -> None:
    st.subheader("Откуда данные")
    meta = pd.DataFrame(data["meta"])
    st.dataframe(meta, hide_index=True, width="stretch", key="tbl_sources")

    st.subheader("Сроки по каналам: что факт, что допущение")
    stats = data.get("transit")
    sea_basis = (f"факт: медиана ETD → доставка по {stats['n']} доставленным отправкам США" if stats
                 else "допущение (файл отправок не подключён)")
    leads = pd.DataFrame([
        ("AWD → FBA", settings.lead_awd, "допущение (данных нет)"),
        ("Склад США → FBA", settings.lead_warehouse, "допущение (данных нет)"),
        ("Море (с фабрики)", settings.lead_sea, sea_basis),
        ("Воздух (с фабрики)", settings.lead_air, "допущение: воздухом ещё не возили, данных нет"),
    ], columns=["Канал", "Срок, дн", "Основание"])
    st.dataframe(leads, hide_index=True, width="stretch", key="tbl_leads")

    if stats:
        st.subheader(f"Фактические сроки доставки, {stats['market']}")
        c1, c2, c3 = st.columns(3)
        c1.metric("Медиана ETD → доставка, дн", f"{stats['median']:.0f}")
        c2.metric("Доставка позже ETA, дн (медиана)", f"{stats['late_median']:.0f}")
        c3.metric("В пути сейчас / уже позже ETA", f"{stats['in_transit']} / {stats['overdue']}")
        st.dataframe(stats["table"], hide_index=True, width="stretch", key="tbl_transit")
        if stats["overdue"]:
            st.caption(f"Из отправок в пути {stats['overdue']} уже позже ETA, в среднем на {stats['overdue_median_days']:.0f} дн. "
                       "Расчёт этого не знает: он ждёт inbound по сроку «shipped через N дней».")

    st.subheader("Чего не хватает")
    missing = []
    if data["batches"] is None:
        missing.append("Точные даты прихода партий по ASIN (Orders-Stock). Сейчас есть только inbound из Hopted"
                       + (" и помесячные приходы планировщика." if use_incoming else "."))
    missing += [
        "Срок AWD → FBA и склад → FBA: в данных нет, стоят допущения.",
        "Готовность заказов в производстве: известен только объём, статуса и даты нет, поэтому море и воздух считаются доступными, если заказ есть.",
        "Цена и условия воздушной доставки: воздухом не возили.",
    ]
    for item in missing:
        st.write(f"• {item}")


def main() -> None:
    st.title("FBA США: что пополнять")

    try:
        data = get_inputs()
    except Exception as exc:  # понятная ошибка вместо трейсбека
        import traceback
        st.error(f"Не удалось прочитать данные: {type(exc).__name__}: {exc}")
        with st.expander("Подробности ошибки (без секретов)"):
            st.code("".join(traceback.format_exception(exc))[-3000:])
        st.info("Проверьте HOPTED_SOURCE / REFERENCE_SOURCE в .env и доступ сервисного аккаунта к файлам (см. README).")
        st.stop()

    for w in data.get("warnings", []):
        st.warning(w)
    settings, use_incoming = settings_sidebar(data)
    if st.sidebar.button("Обновить данные"):
        get_inputs.clear()
        st.rerun()

    try:
        report = get_report(data["loaded_at"], tuple(asdict(settings).items()), date.today().isoformat(),
                            use_incoming, data)
        prepared = calc.prepare_hopted(data["hopted"])
    except ValueError as exc:
        st.error(str(exc))
        st.stop()

    snap = prepared["snapshot"].max()
    snap_txt = "" if pd.isna(snap) else f" · дата снимка возраста запасов (макс.): {snap:%d.%m.%Y}"
    st.caption(
        f"Данные загружены {data['loaded_at']:%d.%m.%Y %H:%M} · Hopted: {data['hopted_spec']} · "
        f"справочник: {data['reference_spec'] or 'нет'}{snap_txt}"
    )
    if data["sources"] is None:
        st.warning("Нет данных об остатках на AWD и складе: канал выбирается только по срокам, наличие товара не проверено.")
    if data["batches"] is None and not use_incoming:
        st.info("Даты прихода партий не подключены: учитывается только inbound из Hopted (см. вкладку «Источники»).")

    # ── фильтры ──
    f = st.sidebar
    f.header("Фильтры")
    statuses = [s for s in calc.STATUS_RANK]
    default_status = ["OUT", "CRITICAL", "URGENT", "ACTION", "PLAN"]
    sel_status = f.multiselect("Статус", statuses, default_status, format_func=lambda s: calc.STATUS_RU[s])
    sel_abc = f.multiselect("ABC (по продажам 30 дн)", ["A", "B", "C", "D"], ["A", "B", "C", "D"])
    groups = sorted(report["parent_group"].dropna().unique())
    sel_group = f.multiselect("Группа", groups)
    colors = sorted(c for c in report["color"].dropna().unique() if c)
    sel_color = f.multiselect("Цвет", colors)
    sizes = sorted(s for s in report["size"].dropna().unique() if s)
    sel_size = f.multiselect("Размер", sizes)
    stores = sorted(report["store"].dropna().unique())
    sel_store = f.multiselect("Магазин", stores)
    sel_channel = f.multiselect("Канал", list(CHANNEL_RU), format_func=lambda c: CHANNEL_RU[c])
    search = f.text_input("ASIN содержит")
    min_lost = f.number_input("Не показывать, если потери за 30 дн меньше, шт", 0.0, 10000.0, 0.0, 1.0,
                              help="Прячет вялые позиции с парой продаж в месяц")

    m = report["status"].isin(sel_status) & report["abc"].isin(sel_abc) & (report["lost_30"] >= min_lost)
    for col, sel in (("parent_group", sel_group), ("color", sel_color), ("size", sel_size), ("store", sel_store), ("channel", sel_channel)):
        if sel:
            m &= report[col].isin(sel)
    if search.strip():
        m &= report["asin"].str.contains(search.strip().upper(), regex=False)
    flt = report[m]

    # ── KPI ──
    cols = st.columns(6)
    cnt = report["status"].value_counts()
    for col, key in zip(cols[:4], ("OUT", "CRITICAL", "URGENT", "ACTION")):
        col.metric(calc.STATUS_RU[key], int(cnt.get(key, 0)))
    cols[4].metric("Слать всего, шт", f"{int(flt['qty_need'].sum()):,}".replace(",", " "))
    cols[5].metric("Потери за 30 дн без действий, шт", f"{int(flt['lost_30'].sum()):,}".replace(",", " "))

    tab_asin, tab_group, tab_sources, tab_quality = st.tabs(
        ["Что пополнять (ASIN)", "По группам, цветам, размерам", "Источники", "Качество данных"])

    with tab_asin:
        st.caption(f"Показано {len(flt)} из {len(report)} строк (ASIN × магазин). Сверху самое срочное.")
        columns = ["status_ru", "abc", "parent_group", "color", "size", "asin", "store", "on_hand", "inbound_counted",
                   "velocity", "coverage_days_now", "stockout_date", "channel_ru", "gap_days", "qty_need", "lost_30"]
        view = show_table(flt, columns, "tbl_asin")
        st.download_button("Скачать CSV", csv_bytes(view), "fba_replenishment_us.csv", "text/csv")

    with tab_group:
        groups_df = calc.summarize_groups(flt)
        if groups_df.empty:
            st.write("Нет данных под выбранные фильтры.")
        else:
            gview = groups_df.rename(columns={
                "status_ru": "Статус", "parent_group": "Группа", "color": "Цвет", "size": "Размер", "asins": "ASIN, шт",
                "on_hand": "Сток FBA", "inbound": "Едет на FBA", "velocity": "Продаж/дн", "qty_need": "Слать, шт",
                "lost_30": "Потери 30 дн, шт", "first_stockout": "Первое обнуление",
            })[["Статус", "Группа", "Цвет", "Размер", "ASIN, шт", "Сток FBA", "Едет на FBA", "Продаж/дн",
                "Первое обнуление", "Слать, шт", "Потери 30 дн, шт"]]
            st.dataframe(style_status(gview), width="stretch", hide_index=True, key="tbl_group", column_config={
                "Первое обнуление": st.column_config.DateColumn(format="DD.MM.YYYY"),
                "Продаж/дн": st.column_config.NumberColumn(format="%.1f"),
                "Сток FBA": st.column_config.NumberColumn(format="%d"),
                "Едет на FBA": st.column_config.NumberColumn(format="%d"),
                "Слать, шт": st.column_config.NumberColumn(format="%d"),
                "Потери 30 дн, шт": st.column_config.NumberColumn(format="%d"),
            })
            st.download_button("Скачать CSV (по группам)", csv_bytes(gview), "fba_replenishment_us_groups.csv", "text/csv")
            by_group = flt.groupby("parent_group")["qty_need"].sum().sort_values(ascending=False)
            st.bar_chart(by_group, y_label="Слать, шт")

    with tab_sources:
        show_sources(data, settings, use_incoming)

    with tab_quality:
        q = calc.data_quality(prepared, data["reference"])
        c1, c2, c3 = st.columns(3)
        c1.metric("Строк ASIN × магазин в Hopted", q["hopted_asin_store_rows"])
        c2.metric("Уникальных ASIN", q["hopted_unique_asins"])
        c3.metric("Строк с дублями (несколько SKU)", q["duplicated_rows"])
        issues = [
            ("В Hopted есть, в справочнике нет", q["not_in_reference"]),
            ("В справочнике активны (US), в Hopted нет", q["reference_active_missing_in_hopted"]),
            ("В Hopted вместо ASIN внутренний код", q["not_real_asin"]),
        ]
        for title, items in issues:
            with st.expander(f"{title}: {len(items)}"):
                if items:
                    st.dataframe(pd.DataFrame({"ASIN / код": items}), hide_index=True, width="stretch")
                else:
                    st.write("Всё в порядке.")
        st.write(f"Активных в справочнике без цвета или размера: **{q['reference_without_color_or_size']}**")
        st.write(f"Класс ABCD у планера пустой или `#N/A`: **{q['no_abc_planner']}**. Поэтому ABC здесь считается по продажам за 30 дней.")
        st.caption(
            "Дата партий в Orders-Stock — крайний срок приёмки, задержки в ней не видны, поэтому к ETA партии добавляется запас "
            f"({settings.batch_delay_days:.0f} дн.). Поставки в статусе working по умолчанию не считаются приходом."
        )


main()
