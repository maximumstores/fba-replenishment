"""Смотрит вкладки Margin в US-файле Нины: заголовки и пара строк -> state/margin_probe.json."""
from __future__ import annotations
import json, time
from pathlib import Path
import loaders

BOOK = "1b3RWGwKP3_g_bA8JCuc6-XHCTRw3KiPlW8ROIUFH9qs"

def retry(fn, tries=6):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            if i == tries - 1:
                raise
            time.sleep(5 * (i + 1)); print("retry", i + 1, str(e)[:80])

def main():
    book = retry(lambda: loaders._gspread_book("sheet:" + BOOK))
    out = {}
    for ws in retry(book.worksheets):
        if ws.title.strip().lower() not in ("margin",):
            continue
        hdr = retry(lambda: ws.get("A1:BA1", value_render_option="FORMATTED_VALUE"))[0]
        rows = retry(lambda: ws.get("A2:T8", value_render_option="UNFORMATTED_VALUE"))
        col_a = retry(lambda: ws.col_values(1))
        out[repr(ws.title)] = {"rows": ws.row_count, "asins": sum(1 for v in col_a if str(v).startswith("B0")),
                               "header": [f"{i}:{h}" for i, h in enumerate(hdr) if h], "sample": rows[:3]}
    Path("state").mkdir(exist_ok=True)
    Path("state/margin_probe.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print("ok")

if __name__ == "__main__":
    main()
