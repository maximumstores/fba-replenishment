"""Сверка листа отчёта (US · Рест на 2 мес) с планом Нины (Orders, Plan M'YY): Demand 2 мес по каждому ASIN -> state/us_sheet_check.json."""
from __future__ import annotations
import json, os, time
from pathlib import Path
import nina_sheet, restock_job

def retry(fn, tries=6):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            if i == tries - 1:
                raise
            time.sleep(5 * (i + 1))

def num(x):
    try:
        return float(str(x).replace(" ", "").replace("\xa0", "").replace(",", "."))
    except ValueError:
        return 0.0

def main():
    book = restock_job.open_book(os.environ["RESTOCK_REPORT_SHEET"])
    vals = retry(lambda: book.worksheet("US · Рест на 2 мес").get_all_values())
    hi = next(i for i, r in enumerate(vals) if "ASIN" in r)
    head = vals[hi]
    rows = [dict(zip(head, r)) for r in vals[hi + 1:] if any(r)]
    nina = retry(nina_sheet.load_us_plan).drop_duplicates("asin").set_index("asin")["demand_2m"]
    bad, ok = [], 0
    for r in rows:
        a, d = r["ASIN"], num(r["Demand 2 мес"])
        n = float(nina.get(a, 0.0))
        if abs(d - n) > 0.5:
            bad.append({"asin": a, "report": d, "nina": n})
        else:
            ok += 1
    out = {"rows": len(rows), "ok": ok, "bad": len(bad), "bad_sample": bad[:10],
           "sample": [{"asin": r["ASIN"], "report": num(r["Demand 2 мес"]), "nina": float(nina.get(r["ASIN"], 0))} for r in rows[:5]]}
    Path("state").mkdir(exist_ok=True)
    Path("state/us_sheet_check.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print("ok")

if __name__ == "__main__":
    main()
