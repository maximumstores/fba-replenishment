"""Индекс ASIN: где по каждому ASIN лежит остаток (не 0) во вкладках файлов Нины -> вкладка «Индекс ASIN» + «Поиск ASIN» в отчёте.

Колонки ищем по названию шапки (не по букве). Какие вкладки/колонки не нашлись — пишем в state/asin_index.json (поле missing).
Запуск: python asin_index.py [--dry-run]   (env GOOGLE_SERVICE_ACCOUNT_JSON, RESTOCK_REPORT_SHEET)
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import loaders
import nina_sheet

BOOKS = {"US": ("Stock Amazon Merino (US)", "1acpHlfBiJjFm96VmFksRmKTnktULTgmu7iESYFDLUZw"),
         "CA": ("Stock CA", "1FRP8eWrJRNraSeua1kJn_rki9H4XshUdiKRMw9w8QnA"),
         "UKDE": ("Stock UK-DE", "1XKUoiKUa8aF0DT7pNgXRqEvlejVu_FYVpMRPyGcOlLI")}
ASIN_RE = re.compile(r"B0[A-Z0-9]{8}")

# rows: каждая строка вкладки -> показатели (alias-ы названий колонок). split — колонка, разделяющая строки (аккаунт).
# boxes: каждая строка = коробка; сворачиваем по ASIN: коробок, штук, на складе, отправлено, шипменты.
SOURCES = [
    # ---- US ----
    dict(book="US", market="US", tab="stock Amazon", kind="rows", asin=["asin"], split="Account Title",
         metrics={"Всего на Amazon": ["Total Units"], "Inbound": ["Inbound"], "Available": ["Available"],
                  "FC Transfer": ["FC Transfer"], "FC Processing": ["FC Processing"], "Unfulfillable": ["Unfulfillable"]}),
    dict(book="US", market="US", tab="AWD-inventory-reports", kind="rows", asin=["asin"],
         metrics={"AWD: доступно": ["Available in AWD (units)"], "AWD: едет на AWD": ["Inbound to AWD (units)"],
                  "AWD: резерв": ["Reserved in AWD (units)"]}),
    dict(book="US", market="US", tab="Orders", kind="rows", asin=["asin"],
         metrics={"Склад (WRH QTY)": ["WRH QTY"], "AWD доступно (Orders)": ["Available in AWD (units)"],
                  "AWD → FBA (Outbound)": ["Outbound to FBA (units)"], "План 10'26": ["Plan 10'26"],
                  "План 11'26": ["Plan 11'26"], "План 12'26": ["Plan 12'26"]}),
] + [dict(book="US", market="US", tab=t, kind="boxes", asin=["asin"], units=["QTY/CTN"], stock=["Stock"], sent=["Sent to amazon"],
          name=["Shipment name"], ctn=["CTN NO"]) for t in ("Fulfillment-BOX TX", "Fulfillment-BOX FL", "Fulfillment-BOX CA", "Fulfillment-BOX DE")] + [
    # ---- CA ----
    dict(book="CA", market="CA", tab="stock Amazon", kind="rows", asin=["asin"], split="Account Title",
         metrics={"Всего на Amazon": ["Total CA"], "Inbound": ["Inbound CA"], "Available": ["in stock CA"],
                  "FC Transfer": ["FC Transfers CA"], "FC Processing": ["For FC Processing"]}),
    dict(book="CA", market="CA", tab="Orders", kind="rows", asin=["asin", "ASIN"], metrics={"Склад (WRH)": ["WRH QTY", "WRH q-ty"]}),
    dict(book="CA", market="CA", tab="FFBOX-CA EFusion", kind="boxes", asin=["asin"], units=["QTY"], name=["Shipment name"], ctn=["Boxes"]),
    dict(book="CA", market="CA", tab="All stock at warehouses", kind="rows", asin=["asin"], metrics={"Склад (All stock)": ["SUM of Stock"]}),
    # ---- UK / DE ----
    dict(book="UKDE", market="UK", tab="Amazon UK", kind="rows", asin=["asin"], split="Store name",
         metrics={"Всего на Amazon": ["SUM of Total Quantity"], "Inbound": ["SUM of Inbound"], "Available": ["SUM of Available qty"]}),
    dict(book="UKDE", market="UK", tab="Warehouse UK", kind="boxes", asin=["asin"], units=["Unit qty"], name=["Shipment name"], ctn=["product id"]),
    dict(book="UKDE", market="UK", tab="Orders UK", kind="rows", asin=["asin", "UK"], metrics={"Склад (WRH)": ["WRH QTY", "WRH q-ty"]}),
    dict(book="UKDE", market="DE", tab="Order DE", kind="rows", asin=["asin"], metrics={"Склад (WRH)": ["WRH QTY", "WRH q-ty"], "AWD доступно": ["Available in AWD", "Available in AWD (units)"]}),
] + [dict(book="UKDE", market="DE", tab=t, kind="boxes", asin=["asin"], units=["Unit qty"], name=["Shipment name"], ctn=["product id"])
     for t in ("FF BOX DE Bremen Maximumstores", "FF BOX DE Bremen Eglobetrade", "FF BOX Kodersdorf")]


def _norm(s) -> str:
    return " ".join(str(s).replace("\xa0", " ").replace("\n", " ").split()).lower()


def retry(fn, tries=6):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            if i == tries - 1:
                raise
            time.sleep(6 * (i + 1))
            print("retry", i + 1, str(e)[:80])


def _find(h: list[str], names: list[str]) -> int | None:
    for n in names:
        n = _norm(n)
        for i, x in enumerate(h):
            if x == n:
                return i
    return None


def _cell(r, i):
    return r[i] if i is not None and i < len(r) else ""


def read_source(ws, spec: dict, missing: list):
    vals = retry(lambda: ws.get("A1:AZ", value_render_option="UNFORMATTED_VALUE"))
    for hi in range(min(25, len(vals))):
        h = [_norm(c) for c in vals[hi]]
        ia = _find(h, spec["asin"])
        if ia is not None and sum(1 for c in h if c) >= 4:
            break
    else:
        missing.append(f"{spec['tab']}: нет колонки ASIN в первых 25 строках")
        return []
    rows = vals[hi + 1:]
    out = []
    if spec["kind"] == "rows":
        cols = {}
        for label, aliases in spec["metrics"].items():
            i = _find(h, aliases)
            if i is None:
                missing.append(f"{spec['tab']}: нет колонки {aliases}")
            else:
                cols[label] = i
        isplit = _find(h, [spec["split"]]) if spec.get("split") else None
        acc = defaultdict(float)
        for r in rows:
            a = str(_cell(r, ia)).strip().upper()
            if not ASIN_RE.fullmatch(a):
                continue
            d = str(_cell(r, isplit)).strip() if isplit is not None else ""
            for label, i in cols.items():
                v = nina_sheet._num_text(_cell(r, i))
                if v:
                    acc[(a, label, d)] += v
        out = [(a, spec["market"], spec["tab"], label, v, d) for (a, label, d), v in acc.items() if abs(v) > 1e-9]
    else:
        ci = {k: _find(h, spec[k]) for k in ("units", "stock", "sent", "name", "ctn") if k in spec}
        if ci.get("units") is None and ci.get("stock") is None:
            missing.append(f"{spec['tab']}: нет колонок штук {spec.get('units')}")
        g = defaultdict(lambda: {"boxes": 0, "units": 0.0, "stock": 0.0, "sent": 0.0, "names": []})
        for r in rows:
            a = str(_cell(r, ia)).strip().upper()
            if not ASIN_RE.fullmatch(a):
                continue
            x = g[a]
            x["boxes"] += 1
            x["units"] += nina_sheet._num_text(_cell(r, ci.get("units")))
            x["stock"] += nina_sheet._num_text(_cell(r, ci.get("stock")))
            x["sent"] += nina_sheet._num_text(_cell(r, ci.get("sent")))
            nm = str(_cell(r, ci.get("name"))).strip()
            if nm and nm not in x["names"]:
                x["names"].append(nm)
        for a, x in g.items():
            det = "; ".join(x["names"][:6]) + (f" (+{len(x['names']) - 6})" if len(x["names"]) > 6 else "")
            if x["units"]:
                out.append((a, spec["market"], spec["tab"], "Коробки: штук всего", x["units"], f"{x['boxes']} кор.; {det}"))
            if x["stock"]:
                out.append((a, spec["market"], spec["tab"], "Коробки: на складе (не отправлено)", x["stock"], det))
            if x["sent"]:
                out.append((a, spec["market"], spec["tab"], "Коробки: отправлено на Amazon", x["sent"], det))
    return out


def build() -> tuple[list[list], dict]:
    books, rows, missing, tabs = {}, [], [], {}
    for spec in SOURCES:
        try:
            if spec["book"] not in books:
                books[spec["book"]] = retry(lambda: loaders._gspread_book("sheet:" + BOOKS[spec["book"]][1]))
                tabs[spec["book"]] = {_norm(w.title): w for w in retry(books[spec["book"]].worksheets)}
            ws = tabs[spec["book"]].get(_norm(spec["tab"]))
            if ws is None:
                missing.append(f"{spec['tab']}: вкладки нет в файле"); continue
            part = read_source(ws, spec, missing)
            print(spec["tab"], len(part))
            for a, m, t, label, v, d in part:
                rows.append([a, m, BOOKS[spec["book"]][0], t, label, round(v, 2), d])
        except Exception as e:
            missing.append(f"{spec['tab']}: ошибка {str(e)[:100]}")
    rows.sort(key=lambda r: (r[0], r[1], r[3], r[4]))
    stats = {"rows": len(rows), "asins": len({r[0] for r in rows}), "missing": missing,
             "by_tab": {t: sum(1 for r in rows if r[3] == t) for t in sorted({r[3] for r in rows})}}
    return rows, stats


HEAD = ["ASIN", "Рынок", "Файл", "Вкладка", "Показатель", "Значение", "Деталь (аккаунт / шипменты)"]
SEARCH_TAB, INDEX_TAB = "Поиск ASIN", "Индекс ASIN"


def publish(book, rows: list[list], stamp: str):
    try:
        idx = book.worksheet(INDEX_TAB)
        idx.clear()
    except Exception:
        idx = book.add_worksheet(INDEX_TAB, rows=max(len(rows) + 10, 100), cols=len(HEAD))
    idx.resize(rows=max(len(rows) + 5, 100), cols=len(HEAD))
    idx.update([HEAD], "A1")
    for i in range(0, len(rows), 15000):
        idx.update(rows[i:i + 15000], f"A{2 + i}", value_input_option="RAW")
    idx.freeze(rows=1)
    note = f"Обновлено {stamp}. Значения не 0 из вкладок файлов Нины. Таб. «Поиск ASIN» читает отсюда."
    try:
        sr = book.worksheet(SEARCH_TAB)
        sr.update_acell("A2", note)
    except Exception:
        sr = book.add_worksheet(SEARCH_TAB, rows=500, cols=9)
        sr.update([["Поиск по ASIN: вставьте ASIN (можно списком) в столбец A, с A4 вниз, до 200 штук. Таблица справа заполнится сама."], [note],
                   [], ["ASIN (вставьте сюда)"]], "A1")
        sr.update_acell("C4", f"=IFERROR(FILTER('{INDEX_TAB}'!A2:G; ISNUMBER(MATCH('{INDEX_TAB}'!A2:A; $A$5:$A$204; 0)));"
                              f"\"Нет остатка не 0 по этим ASIN (или ASIN не введён)\")")
        sr.update([HEAD], "C3")
        sr.freeze(rows=4)
        sr.format("A4", {"textFormat": {"bold": True}})
    return idx


def main(argv=None) -> int:
    dry = "--dry-run" in (argv or sys.argv[1:])
    rows, stats = build()
    Path("state").mkdir(exist_ok=True)
    Path("state/asin_index.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1))
    print(json.dumps({k: v for k, v in stats.items() if k != "by_tab"}, ensure_ascii=False)[:1500])
    sid = os.getenv("RESTOCK_REPORT_SHEET", "")
    if dry or not sid:
        return 0
    import restock_job
    publish(restock_job.open_book(sid), rows, datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%d.%m.%Y %H:%M"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
