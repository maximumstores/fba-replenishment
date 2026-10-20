"""Тест «Поиск ASIN»: вставляет ASIN в A5.., читает результат формулы, чистит ввод -> state/index_lookup.json."""
from __future__ import annotations
import json, os, sys, time
from pathlib import Path
import restock_job

def main(asins):
    book = restock_job.open_book(os.environ["RESTOCK_REPORT_SHEET"])
    ws = book.worksheet("Поиск ASIN")
    ws.update([[a] for a in asins], "A5")
    time.sleep(8)
    vals = ws.get("C3:I80", value_render_option="FORMATTED_VALUE")
    ws.batch_clear([f"A5:A{4 + len(asins)}"])
    Path("state").mkdir(exist_ok=True)
    Path("state/index_lookup.json").write_text(json.dumps(vals, ensure_ascii=False, indent=0))
    print(len(vals))

if __name__ == "__main__":
    main(sys.argv[1:])
