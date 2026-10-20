"""Список вкладок US-файла Нины (есть ли «Поиск ASIN») и права сервис-аккаунта -> state/tab_titles.json."""
from __future__ import annotations
import json, time
from pathlib import Path
import loaders

BOOK = "1acpHlfBiJjFm96VmFksRmKTnktULTgmu7iESYFDLUZw"

def retry(fn, tries=6):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            if i == tries - 1:
                raise
            time.sleep(5 * (i + 1))

def main():
    book = retry(lambda: loaders._gspread_book("sheet:" + BOOK))
    titles = [w.title for w in retry(book.worksheets)]
    out = {"titles": titles, "search_tabs": [t for t in titles if "поиск" in t.lower() or "search" in t.lower() or "индекс" in t.lower()]}
    try:
        r = book.client.http_client.request("get", f"https://www.googleapis.com/drive/v3/files/{BOOK}",
                                            params={"fields": "capabilities(canEdit,canAddChildren),name", "supportsAllDrives": "true"})
        out["drive"] = r.json()
    except Exception as e:
        out["drive_error"] = str(e)[:200]
    Path("state").mkdir(exist_ok=True)
    Path("state/tab_titles.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print("ok")

if __name__ == "__main__":
    main()
