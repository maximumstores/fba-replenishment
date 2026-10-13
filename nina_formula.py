"""Достаёт формулы строки ASIN во вкладке Orders файла Нины (Stock Amazon Merino) -> state/nina_formula.json.
Запуск: python nina_formula.py B0BDZGH33K  (env GOOGLE_SERVICE_ACCOUNT_JSON)."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import loaders

BOOK = "1acpHlfBiJjFm96VmFksRmKTnktULTgmu7iESYFDLUZw"


def retry(fn, tries=6):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # 503 и прочее от Google
            if i == tries - 1:
                raise
            time.sleep(5 * (i + 1))
            print("retry", i + 1, str(e)[:80])


def main(asin: str) -> int:
    book = retry(lambda: loaders._gspread_book("sheet:" + BOOK))
    ws = next(w for w in retry(book.worksheets) if loaders._norm_tab(w.title) == loaders._norm_tab("Orders"))
    col_a = retry(lambda: ws.col_values(1))
    rows = [i + 1 for i, v in enumerate(col_a) if str(v).strip().upper() == asin]
    header = retry(lambda: ws.get("A1:AZ1", value_render_option="FORMATTED_VALUE"))[0]
    out = {"asin": asin, "rows": rows, "tab": ws.title, "cells": []}
    for r in rows:
        f = retry(lambda: ws.get(f"A{r}:AZ{r}", value_render_option="FORMULA"))[0]
        v = retry(lambda: ws.get(f"A{r}:AZ{r}", value_render_option="UNFORMATTED_VALUE"))[0]
        out["cells"].append({"row": r, "cols": [{"h": header[i] if i < len(header) else "", "formula": f[i],
                                                 "value": v[i] if i < len(v) else ""} for i in range(len(f)) if f[i] != ""]})
    # формула в первой строке данных для сравнения
    Path("state").mkdir(exist_ok=True)
    Path("state/nina_formula.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(json.dumps(out, ensure_ascii=False)[:1500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1].upper()))
