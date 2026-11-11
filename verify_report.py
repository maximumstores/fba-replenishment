"""Полная проверка листа отчёта (все рынки, обе вкладки): состав строк, каждая цифра против свежих источников, арифметика.
Итог -> state/verify_report.json. Запуск: python verify_report.py (env RESTOCK_REPORT_SHEET, GOOGLE_SERVICE_ACCOUNT_JSON)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import market_stock
import restock_job
import restock_report as rr

MARKETS = ("US", "CA", "DE", "UK")
SRC = {"Available": "available", "Inbound": "inbound", "Demand 2 мес": "demand_2m",
       "AWD": "awd", "Склад 3PL + inbound": "wrh", "В заказе": "order_qty"}


def _num(x) -> float:
    try:
        return float(str(x).replace(" ", "").replace("\xa0", "").replace(",", ".") or 0)
    except ValueError:
        return 0.0


def _retry(fn, tries=6):
    for i in range(tries):
        try:
            return fn()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(5 * (i + 1))


def check_tab(vals: list[list], fresh, expected: set[str]) -> dict:
    hi = next(i for i, r in enumerate(vals) if "ASIN" in r)
    head = vals[hi]
    rows = [dict(zip(head, r)) for r in vals[hi + 1:] if any(r)]
    asins = [r["ASIN"] for r in rows]
    need_col = next(c for c in head if c.startswith("Срочно слать"))
    col = lambda p: next((c for c in head if c.startswith(p)), None)
    can, wait, none = col("Можно слать"), col("Ждёт заказа"), col("Нечем пополнить")
    src_bad, math_bad = [], []
    for r in rows:
        a = r["ASIN"]
        if a in fresh.index:
            f = fresh.loc[a]
            diff = {k: [_num(r[k]), float(f[v])] for k, v in SRC.items() if k in r and abs(_num(r[k]) - float(f[v])) > 0.5}
            if diff:
                src_bad.append({"asin": a, **diff})
        av, inb, d = _num(r["Available"]), _num(r["Inbound"]), _num(r["Demand 2 мес"])
        need = max(d - (av + inb), 0.0)
        errs = {}
        if abs(need - _num(r[need_col])) > 0.6:
            errs["need"] = [_num(r[need_col]), round(need, 1)]
        if can and wait and none:
            n = _num(r[need_col])
            c = min(n, max(_num(r.get("AWD", 0)) + _num(r.get("Склад 3PL + inbound", 0)), 0))
            w = min(n - c, max(_num(r.get("В заказе", 0)), 0))
            for name, got, exp in ((can, _num(r[can]), c), (wait, _num(r[wait]), w), (none, _num(r[none]), n - c - w)):
                if abs(got - exp) > 0.6:
                    errs[name] = [got, round(exp, 1)]
        if errs:
            math_bad.append({"asin": a, **errs})
    got = set(asins)
    return {"rows": len(rows), "duplicate_asins": len(asins) - len(got),
            "missing_in_sheet": len(expected - got), "extra_in_sheet": len(got - expected),
            "missing_examples": sorted(expected - got)[:5], "extra_examples": sorted(got - expected)[:5],
            "source_mismatch": len(src_bad), "source_examples": src_bad[:5],
            "math_mismatch": len(math_bad), "math_examples": math_bad[:5],
            "has_source_cols": bool(can and wait and none)}


def main() -> int:
    book = _retry(lambda: restock_job.open_book(os.environ["RESTOCK_REPORT_SHEET"]))
    mk = market_stock.load_markets(MARKETS)
    out: dict = {"checked_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "tabs": {}}
    for m in MARKETS:
        df = mk[m].drop_duplicates("asin")
        fresh = df.set_index("asin")
        for title, fn in ((f"{m} · Аут сейчас", rr.out_now), (f"{m} · Рест на 2 мес", rr.need_2m)):
            try:
                vals = _retry(lambda t=title: book.worksheet(t).get_all_values())
                out["tabs"][title] = check_tab(vals, fresh, set(fn(df)["asin"]))
            except Exception as exc:
                out["tabs"][title] = {"error": f"{type(exc).__name__}: {exc}"}
    out["ok"] = all(not t.get("error") and not (t["duplicate_asins"] or t["missing_in_sheet"] or t["extra_in_sheet"]
                    or t["source_mismatch"] or t["math_mismatch"]) for t in out["tabs"].values())
    Path("state").mkdir(exist_ok=True)
    Path("state/verify_report.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(json.dumps(out, ensure_ascii=False)[:3000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
