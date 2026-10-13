import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import market_stock as ms
import restock_report as rr

HDR = ["Hopted ID", "Product name", "Store name", "Marketplace Country Code", "SKU", "FNSKU", "ASIN", "FBA total quantity",
       "FBA inbound receiving quantity", "FBA inbound shipped quantity", "FBA inbound working quantity", "FBA reserved quantity",
       "FBA fulfillable quantity", "Days of supply"]


def rows():
    return [["junk"], HDR,
            ["h1", "p", "s", "DE", "sku", "B0AAAAAAA1", "B0AAAAAAA1", "0", "1", "2", "3", "0", "0", "5"],
            ["h2", "p", "s", "DE", "sku", "B0AAAAAAA1", "B0AAAAAAA1", "0", "0", "0", "0", "0", "4", "7"],  # второй магазин: суммируем
            ["h3", "p", "s", "GB", "sku", "B0BBBBBBB2", "B0BBBBBBB2", "0", "0", "0", "0", "0", "9", "1"],
            ["h4", "p", "s", "DE", "sku", "x", "ASIN", "0", "0", "0", "0", "0", "9", "1"]]


def test_parse_filters_country_sums_stores_and_inbound():
    d = ms.parse_hopted(rows(), {"DE"}).set_index("asin")
    assert list(d.index) == ["B0AAAAAAA1"] and d.loc["B0AAAAAAA1", "available"] == 4 and d.loc["B0AAAAAAA1", "inbound"] == 6


def test_build_market_and_out_now_sorted_by_money():
    stock = pd.DataFrame({"asin": ["B0AAAAAAA1", "B0BBBBBBB2", "B0CCCCCCC3"], "available": [0, 0, 5], "inbound": [0, 0, 0], "days_supply": [0, 0, 3]})
    dim = pd.DataFrame({"asin": ["B0AAAAAAA1", "B0BBBBBBB2"], "ab": ["A", "B"], "category": ["Mens", "Pants"], "color": ["Black", "Grey"],
                        "size": ["L", "M"], "cogs": [10.0, 5.0], "active_us": [True, True]})
    demand = pd.DataFrame({"asin": ["B0AAAAAAA1", "B0BBBBBBB2"], "demand_2m": [100.0, 100.0]})
    df = ms.build_market("US", stock, dim, demand)
    r = rr.out_now(df)
    assert list(r["asin"]) == ["B0AAAAAAA1", "B0BBBBBBB2"] and r.loc[0, "lost_eur"] == 1000
    assert "B0CCCCCCC3" not in set(df["asin"])  # нет в справочнике → не активный, в отчёт не попадает


def test_run_market_diff_and_text(tmp_path):
    import restock_job as rj
    stock = pd.DataFrame({"asin": ["B0AAAAAAA1"], "available": [0], "inbound": [0], "days_supply": [0]})
    df = ms.build_market("CA", stock, pd.DataFrame({"asin": ["B0AAAAAAA1"], "ab": ["A"], "category": ["Mens"], "color": ["Black"], "size": ["L"], "cogs": [10.0]}), None)
    state = {}
    t, ch, now, plan = rj.run_market("CA", df, state, "x")
    assert ch and "аут сейчас — 1 ASIN (+1 / −0)" in t and plan.empty
    t2, ch2, *_ = rj.run_market("CA", df, state, "x")
    assert not ch2


def test_inactive_asins_are_dropped():
    stock = pd.DataFrame({"asin": ["B0AAAAAAA1", "B0BBBBBBB2"], "available": [0, 0], "inbound": [0, 0], "days_supply": [0, 0]})
    dim = pd.DataFrame({"asin": ["B0AAAAAAA1", "B0BBBBBBB2"], "ab": ["A", "A"], "category": ["x", "x"], "color": ["c", "c"], "size": ["L", "L"],
                        "cogs": [1.0, 1.0], "active_de": [True, False]})
    assert list(ms.build_market("DE", stock, dim, None)["asin"]) == ["B0AAAAAAA1"]


def test_parse_hopted_dedupes_same_fnsku_and_counts_fc_reserved():
    hdr = ["Hopted ID", "ASIN", "SKU", "FNSKU", "FBA fulfillable quantity", "FBA inbound receiving quantity",
           "FBA inbound shipped quantity", "FBA inbound working quantity", "Reserved FC Transfer", "Reserved FC Processing"]
    rows = [hdr,
            ["h1", "B09C4K26BS", "S-M", "B09C4K26BS", "0", "1", "40", "0", "2", "2"],
            ["h2", "B09C4K26BS", "S-M-2", "B09C4K26BS", "0", "1", "40", "0", "2", "2"]]
    out = ms.parse_hopted(rows)
    assert out.loc[0, "inbound"] == 45 and out.loc[0, "available"] == 0


def test_parse_hopted_sums_stores_but_dedupes_inside_store():
    hdr = ["Hopted ID", "ASIN", "SKU", "FNSKU", "Store name", "FBA fulfillable quantity"]
    rows = [hdr,
            ["1", "B09C4K26BS", "a", "X1", "MERINO-TECH", "10"],
            ["2", "B09C4K26BS", "b", "X1", "MERINO-TECH", "10"],
            ["3", "B09C4K26BS", "c", "X1", "SPORTACUS", "5"]]
    out = ms.parse_hopted(rows)
    assert out.loc[0, "available"] == 15
