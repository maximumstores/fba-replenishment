"""Разовая проверка: (1) сколько строк отчёта убирает правило «без продаж не показываем» по рынкам;
(2) ячейки плана US (вкладка Orders Нины), где число записано текстом. -> state/drop_probe.json"""
from __future__ import annotations
import json, time
from pathlib import Path
import loaders, market_stock, nina_sheet
import restock_report as rr

def retry(fn, tries=6):
    for i in range(tries):
        try:
            return fn()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(5 * (i + 1))

def col_letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s

def counts(mk):
    return {m: {"rows": len(df), "out_now": len(rr.out_now(df.drop_duplicates("asin"))),
                "need_2m": len(rr.need_2m(df.drop_duplicates("asin")))} for m, df in mk.items()}

def main():
    out = {}
    real = market_stock.drop_never_sold
    market_stock.drop_never_sold = lambda df, sold, dim: df
    without = counts(retry(market_stock.load_markets))
    market_stock.drop_never_sold = real
    with_rule = counts(retry(market_stock.load_markets))
    out["no_sales_rule"] = {m: {"without_rule": without[m], "with_rule": with_rule[m]} for m in with_rule}

    book = retry(lambda: loaders._gspread_book("sheet:" + nina_sheet.US_SHEET_DEFAULT))
    header, rows = retry(lambda: nina_sheet.read_tab(book, "Orders"))
    h = [" ".join(str(c).split()) for c in header]
    plan_cols = [i for i, x in enumerate(h) if x.lower().replace(" ", "").startswith("plan") and "'" in x]
    cells = []
    for n, r in enumerate(rows, start=2):
        a = str(r[0]).strip().upper() if r else ""
        if not nina_sheet.ASIN_RE.fullmatch(a):
            continue
        for c in plan_cols:
            v = r[c] if c < len(r) else ""
            if isinstance(v, str) and v.strip():
                cells.append({"cell": f"{col_letter(c)}{n}", "column": h[c], "asin": a, "value": v})
    out["us_text_plan_cells"] = {"plan_columns": [h[c] for c in plan_cols], "count": len(cells), "cells": cells[:200]}
    Path("state").mkdir(exist_ok=True)
    Path("state/drop_probe.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(json.dumps(out, ensure_ascii=False)[:3000])

if __name__ == "__main__":
    main()
