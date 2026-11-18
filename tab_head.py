"""Разово: шапка и первые строки вкладки Нины -> state/tab_head.json. env TAB, BOOK."""
import json, os
from pathlib import Path
import loaders, nina_sheet
book = loaders._gspread_book("sheet:" + os.environ["BOOK"])
header, rows = nina_sheet.read_tab(book, os.environ["TAB"])
c = [r for r in rows if r and nina_sheet.ASIN_RE.fullmatch(str(r[0]).strip().upper())]
def f(v):
    try: return float(v)
    except Exception: return 0.0
sums = {str(header[i]): sum(f(r[i]) for r in c if i < len(r)) for i in range(len(header))}
nz = {str(header[i]): sum(1 for r in c if i < len(r) and f(r[i]) != 0) for i in range(len(header))}
out = {"sums": sums, "nonzero": nz, "tab": os.environ["TAB"], "header": header, "asin_rows": len(c), "sample": c[:3]}
try:
    df = nina_sheet.parse_orders(header, rows, "UK", plan="months")
    out["parsed_rows"] = len(df)
except Exception as e:
    out["parse_error"] = str(e)
Path("state").mkdir(exist_ok=True)
Path("state/tab_head.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str))
