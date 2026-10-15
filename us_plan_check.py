"""Сверка US: demand_2m из BigQuery (plan_sales) с вкладкой Orders Нины (Stock Amazon Merino) -> state/us_plan_check.json."""
from __future__ import annotations
import json, re, time
from pathlib import Path
import pandas as pd
import loaders, market_stock, nina_sheet

BOOK = "1acpHlfBiJjFm96VmFksRmKTnktULTgmu7iESYFDLUZw"

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
    header, rows = retry(lambda: nina_sheet.read_tab(book, "Orders"))
    h = [" ".join(str(c).split()) for c in header]
    c_av = next(i for i, x in enumerate(h) if x.lower() == "available")
    nina_hdr = h[c_av:c_av + 4]
    nina_hdr_next = [h[c_av + 1], h[c_av + 2]]
    c10 = next(i for i, x in enumerate(h) if x.lower().replace(" ", "") == "plan10'26")
    c11 = next(i for i, x in enumerate(h) if x.lower().replace(" ", "") == "plan11'26")
    nina_hdr = [h[c10], h[c11]]
    def num(v):
        if isinstance(v, (int, float)):
            return float(v)
        t = re.sub(r"[^0-9.,-]", "", str(v)).replace(",", ".")
        try:
            return float(t) if t not in ("", "-", ".") else 0.0
        except ValueError:
            return 0.0
    recs = {}
    text_cells = 0
    for r in rows:
        a = str(r[0]).strip().upper() if r else ""
        if not nina_sheet.ASIN_RE.fullmatch(a):
            continue
        v10 = r[c10] if c10 < len(r) else 0
        v11 = r[c11] if c11 < len(r) else 0
        text_cells += isinstance(v10, str) and bool(str(v10).strip()) or isinstance(v11, str) and bool(str(v11).strip())
        recs[a] = max(recs.get(a, 0.0), num(v10) + num(v11))
    nina = pd.DataFrame({"demand_2m": pd.Series(recs)})
    nina.index.name = "asin"
    us = market_stock.load_markets(("US",))["US"].drop_duplicates("asin").set_index("asin")
    both = us.join(nina[["demand_2m"]], how="outer", lsuffix="_bq", rsuffix="_nina").fillna({"demand_2m_bq": 0, "demand_2m_nina": 0})
    both["diff"] = both["demand_2m_bq"] - both["demand_2m_nina"]
    tot = {"asins_bq_with_plan": int((both["demand_2m_bq"] > 0).sum()), "asins_nina_with_plan": int((both["demand_2m_nina"] > 0).sum()),
           "sum_bq": float(both["demand_2m_bq"].sum()), "sum_nina": float(both["demand_2m_nina"].sum()),
           "same(±1)": int(((both["diff"].abs() <= 1) & ((both["demand_2m_bq"] > 0) | (both["demand_2m_nina"] > 0))).sum()),
           "only_bq": int(((both["demand_2m_bq"] > 0) & (both["demand_2m_nina"] == 0)).sum()),
           "only_nina": int(((both["demand_2m_nina"] > 0) & (both["demand_2m_bq"] == 0)).sum())}
    worst = both.reindex(both["diff"].abs().sort_values(ascending=False).index).head(8)[["demand_2m_bq", "demand_2m_nina", "diff"]]
    out = {"nina_cols": nina_hdr, "text_plan_cells": int(text_cells), "totals": tot, "worst": worst.reset_index().to_dict("records"),
           "sample": both[(both["demand_2m_bq"] > 0) & (both["demand_2m_nina"] > 0)].sample(5, random_state=1)[["demand_2m_bq", "demand_2m_nina"]].reset_index().to_dict("records")}
    Path("state").mkdir(exist_ok=True)
    Path("state/us_plan_check.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print("ok")

if __name__ == "__main__":
    main()
