"""Синтетические данные в формате реальных источников — чтобы проверить расчёт, дашборд и алерт
без доступа к Hopted и BigQuery. Это НЕ реальные цифры.

    python make_demo_data.py
"""
from __future__ import annotations

import string
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path("demo")
STORES = ["SPORTACUS", "SPO MERINO-TECH", "eLeaf", "MR EQUIPP", "World Sports Fanatics"]
GROUPS = {
    "W SS+Sox": ["Black", "Navy", "Grey", "Burgundy"],
    "Mens SS": ["Black", "Navy", "Olive"],
    "Underwear Mens": ["Black", "Grey"],
    "W Pants": ["Black", "Charcoal"],
    "Wool Hiking socks Nicole": ["Grey", "Green"],
}
SIZES = ["XS", "S", "M", "L", "XL"]


def fake_asin(rng: np.random.Generator) -> str:
    alphabet = list(string.ascii_uppercase + string.digits)
    return "B0" + "".join(rng.choice(alphabet, 8))


def main(seed: int = 7) -> None:
    rng = np.random.default_rng(seed)
    OUT.mkdir(exist_ok=True)
    today = date.today()

    ref_rows, hop_rows = [], []
    n = 0
    for group, colors in GROUPS.items():
        for color in colors:
            for size in SIZES:
                n += 1
                asin = fake_asin(rng)
                active = rng.random() > 0.05
                ref_rows.append({
                    "asin": asin, "old_asin": "", "group_key": f"{group}|{color}|{size}",
                    "parent_group": group, "category": group.split()[0], "color": color, "size": size,
                    "abcd_class": rng.choice(["#N/A", "#N/A", "AAA", "", "not sales"]),
                    "active_us": "true" if active else "false",
                    "plan_units_month": float(rng.integers(0, 90)),
                })
                scenario = rng.choice(["ok", "ok", "ok", "low", "out", "inbound", "dead"])
                v30 = int(rng.integers(5, 120))
                v7 = max(int(v30 / 30 * 7 * rng.uniform(0.6, 1.5)), 0)
                if scenario == "dead":
                    v30 = v7 = 0
                fulfil = {"ok": int(v30 * 3), "low": int(v30 / 30 * rng.integers(3, 20)), "out": 0,
                          "inbound": int(v30 / 30 * 2), "dead": 0}[scenario]
                ship = int(v30 * 1.5) if scenario == "inbound" else 0
                hop_rows.append({
                    "Hopted ID": f"hpt_{n:05d}", "ASIN": asin, "SKU": f"SKU-{n}", "FNSKU": asin,
                    "Product name": f"{group} {color} {size}", "Store name": rng.choice(STORES),
                    "Inventory age snapshot date": (today - timedelta(days=int(rng.integers(0, 3)))).strftime("%d.%m.%Y"),
                    "FBA fulfillable quantity": fulfil, "FBA inbound receiving quantity": 0,
                    "FBA inbound shipped quantity": ship, "Reserved FC Transfer": 0, "Reserved FC Processing": 0,
                    "Units shipped last 7 days": v7, "Units shipped last 30 days": v30,
                    "Days of supply": 366, "FBA inbound working quantity": 0,
                    "FBA reserved quantity": 0, "FBA total quantity": fulfil + ship,
                })
    # внутренние коды вместо ASIN (как у 590 позиций в реальном справочнике) — в Hopted их быть не должно
    for i in range(6):
        ref_rows.append({"asin": f"W-SS-SX-LW-{228 + i:05d}", "old_asin": "", "group_key": "x", "parent_group": "W SS+Sox",
                         "category": "W", "color": "Black", "size": "M", "abcd_class": "", "active_us": "true",
                         "plan_units_month": 0.0})
    # дубль строки ASIN × магазин (несколько SKU на один ASIN) и строка без ASIN
    hop_rows.append({**hop_rows[0], "Hopted ID": "hpt_dup", "SKU": "SKU-dup"})
    hop_rows.append({**hop_rows[1], "Hopted ID": "hpt_blank", "ASIN": ""})

    pd.DataFrame(hop_rows).to_csv(OUT / "hopted_us.csv", index=False)
    pd.DataFrame(ref_rows).to_csv(OUT / "reference.csv", index=False)

    # остатки каналов: часть ASIN без AWD, часть без склада
    srcs = []
    for r in ref_rows[: len(ref_rows) - 6]:
        srcs.append({"asin": r["asin"], "awd_qty": int(rng.choice([0, 0, 50, 300])),
                     "wrh_qty": int(rng.choice([0, 100, 500]))})
    pd.DataFrame(srcs).to_csv(OUT / "sources.csv", index=False)

    # партии на FBA из Orders-Stock
    batches = []
    for r in ref_rows[: len(ref_rows) - 6][::9]:
        batches.append({"asin": r["asin"], "qty": int(rng.integers(100, 800)),
                        "eta": (today + timedelta(days=int(rng.integers(-5, 40)))).strftime("%d.%m.%Y"),
                        "destination": "AMZ"})
    pd.DataFrame(batches).to_csv(OUT / "batches.csv", index=False)
    print(f"demo: {len(hop_rows)} строк Hopted, {len(ref_rows)} в справочнике, {len(srcs)} остатков каналов, {len(batches)} партий → {OUT}/")


if __name__ == "__main__":
    main()
