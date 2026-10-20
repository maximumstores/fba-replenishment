"""Список вкладок и шапки в файлах Нины (US, CA, UK-DE): где в них лежит ASIN и какие остатки -> state/all_tabs.json."""
from __future__ import annotations
import json, re, time
from pathlib import Path
import loaders

BOOKS = {"US": "1acpHlfBiJjFm96VmFksRmKTnktULTgmu7iESYFDLUZw", "CA": "1FRP8eWrJRNraSeua1kJn_rki9H4XshUdiKRMw9w8QnA",
         "UKDE": "1XKUoiKUa8aF0DT7pNgXRqEvlejVu_FYVpMRPyGcOlLI"}
ASIN = re.compile(r"B0[A-Z0-9]{8}")

def retry(fn, tries=6):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            if i == tries - 1:
                raise
            time.sleep(5 * (i + 1))

def main():
    out = {}
    for key, bid in BOOKS.items():
        book = retry(lambda: loaders._gspread_book("sheet:" + bid))
        tabs = {}
        for ws in retry(book.worksheets):
            try:
                v = retry(lambda: ws.get("A1:BA40", value_render_option="FORMATTED_VALUE"))
            except Exception as e:
                tabs[ws.title] = {"error": str(e)[:80]}; continue
            hdr_i = max(range(len(v)), key=lambda i: sum(1 for c in v[i] if str(c).strip()), default=0) if v else 0
            hdr = v[hdr_i] if v else []
            asin_cols = sorted({j for r in v[hdr_i + 1:] for j, c in enumerate(r) if ASIN.fullmatch(str(c).strip())})
            tabs[ws.title] = {"rows": ws.row_count, "hdr_row": hdr_i + 1, "asin_cols": asin_cols,
                              "header": [f"{j}:{' '.join(str(h).split())}" for j, h in enumerate(hdr) if str(h).strip()][:60]}
        out[key] = tabs
    Path("state").mkdir(exist_ok=True)
    Path("state/all_tabs.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print("ok")

if __name__ == "__main__":
    main()
