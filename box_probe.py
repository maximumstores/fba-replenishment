"""Смотрит вкладку Fulfillment-BOX TX в Stock Amazon Merino (US): шапка, примеры, статистика по Shipment ID -> state/box_probe.json."""
from __future__ import annotations
import json, re, time
from collections import Counter
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
    ws = next(w for w in retry(book.worksheets) if w.title.strip().lower() == "fulfillment-box tx")
    vals = retry(lambda: ws.get("A1:AZ6000", value_render_option="FORMATTED_VALUE"))
    head = [" ".join(str(c).split()) for c in vals[0]]
    idx = {h.lower(): i for i, h in enumerate(head) if h}
    def col(name):
        return idx.get(name.lower())
    c_asin, c_ship, c_name, c_qty, c_ctn = col("asin"), col("Shipment ID"), col("Shipment name"), col("QTY /CTN"), col("CTN NO")
    rows = [r for r in vals[1:] if r and len(r) > (c_asin or 0) and re.fullmatch(r"B0[A-Z0-9]{8}", str(r[c_asin]).strip())]
    get = lambda r, c: str(r[c]).strip() if c is not None and c < len(r) else ""
    no_ship = [r for r in rows if not get(r, c_ship)]
    names = Counter(get(r, c_name) for r in rows)
    ships = Counter(get(r, c_ship) for r in rows if get(r, c_ship))
    qty = lambda rs: sum(float(re.sub(r"[^0-9.]", "", get(r, c_qty)) or 0) for r in rs)
    out = {"tab_rows": ws.row_count, "header": [f"{i}:{h}" for i, h in enumerate(head) if h],
           "box_rows": len(rows), "distinct_asins": len({get(r, c_asin) for r in rows}),
           "boxes_without_shipment_id": len(no_ship), "units_without_shipment_id": qty(no_ship), "units_total": qty(rows),
           "distinct_shipment_ids": len(ships), "distinct_shipment_names": len(names),
           "top_names": names.most_common(12), "sample_no_ship": [[get(r, 0), get(r, c_asin), get(r, c_qty), get(r, c_name)] for r in no_ship[:5]]}
    Path("state").mkdir(exist_ok=True)
    Path("state/box_probe.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print("ok")

if __name__ == "__main__":
    main()
