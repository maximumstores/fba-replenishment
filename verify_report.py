"""Проверка листа отчёта: каждая строка US-вкладок сверяется со свежими данными Hopted.
Итог пишется в state/verify_report.json. Запуск: python verify_report.py  (env RESTOCK_REPORT_SHEET, GOOGLE_SERVICE_ACCOUNT_JSON)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import market_stock
import restock_job

# цифры Нины (Stock orders USA) по ASIN со скриншота: (available, inbound)
NINA = {"B09C4K26BS": (0, 45), "B0H6SW4BX8": (40, 71), "B09NW9V6HC": (81, 186),
        "B0BDZGH33K": (199, 198), "B0FHQLVP1Q": (143, 138), "B09TPKZBZN": (410, 66)}
TABS = ["US · Аут сейчас", "US · Рест на 2 мес", "CA · Аут сейчас", "CA · Рест на 2 мес"]


def _num(x) -> float:
    try:
        return float(str(x).replace(" ", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return 0.0


def main() -> int:
    book = restock_job.open_book(os.environ["RESTOCK_REPORT_SHEET"])
    mk = market_stock.load_markets(("US", "CA"))
    fresh_by = {m: mk[m].drop_duplicates("asin").set_index("asin") for m in mk}
    out: dict = {"tabs": {}}
    for tab in TABS:
        fresh = fresh_by[tab[:2]]
        vals = book.worksheet(tab).get_all_values()
        hi = next(i for i, r in enumerate(vals) if "ASIN" in r)
        head = vals[hi]
        rows = [dict(zip(head, r)) for r in vals[hi + 1:] if any(r)]
        asins = [r["ASIN"] for r in rows]
        bad = []
        for r in rows:
            a = r["ASIN"]
            if a not in fresh.index:
                bad.append({"asin": a, "why": "нет в свежих данных"})
                continue
            f = fresh.loc[a]
            if abs(_num(r["Available"]) - f["available"]) > 0.5 or abs(_num(r["Inbound"]) - f["inbound"]) > 0.5:
                bad.append({"asin": a, "sheet": [r["Available"], r["Inbound"]], "fresh": [f["available"], f["inbound"]]})
        out["tabs"][tab] = {"rows": len(rows), "duplicate_asins": len(asins) - len(set(asins)),
                            "mismatch_vs_fresh": len(bad), "mismatch_examples": bad[:10], "head": rows[:3],
                            "nina": {a: {"sheet": [r["Available"], r["Inbound"]], "nina": NINA[a]}
                                     for r in rows for a in [r["ASIN"]] if a in NINA}}
    Path("state").mkdir(exist_ok=True)
    Path("state/verify_report.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(json.dumps(out, ensure_ascii=False)[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
