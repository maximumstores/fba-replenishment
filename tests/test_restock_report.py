import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import restock_report as rr


def raw():
    return pd.DataFrame({
        "A/B category+color for winter'25-'26": ["A", "A", "B", "B", "A"],
        "Category": ["Mens LS 250", "Mens LS 250", "Pants", "Pants", "Mens LS 250"],
        "Color": ["250 Black", "250 Black", "Grey", "Grey", "250 Navy"],
        "Size": ["L", "M", "S", "M", "L"],
        "ASIN": ["B0AAAAAAA1", "B0AAAAAAA2", "B0AAAAAAA3", "B0AAAAAAA4", "not-an-asin"],
        "SUM из Available": ["0", "10", "0", "50", "1"],
        "SUM из inbound+FC Transfer+FC Processing": ["5", "0", "0", "10", "0"],
        "Demand forecast next 2 month": ["100", "40", "30", "50", "9"],
        "SUM из Lost Sale OCT": ["€1 200,00", "0", "0", "0", "0"],
        "Price": ["10", "20", "5", "8", "1"],
    })


def test_normalize_maps_aliases_numbers_and_drops_non_asin():
    df = rr.normalize(raw(), market="US")
    assert list(df["asin"]) == ["B0AAAAAAA1", "B0AAAAAAA2", "B0AAAAAAA3", "B0AAAAAAA4"]
    assert df.loc[0, "lost_eur"] == 1200.0 and df.loc[0, "inbound"] == 5 and (df["market"] == "US").all()
    assert df.loc[0, "ab"] == "A" and df.loc[0, "category"] == "Mens LS 250"


def test_out_now_is_zero_available_sorted_by_lost_money():
    df = rr.normalize(raw(), "US")
    r = rr.out_now(df)
    assert list(r["asin"]) == ["B0AAAAAAA1", "B0AAAAAAA3"]  # потери 1200 € у первого, 30*5=150 € у второго
    assert r.loc[1, "lost_eur"] == 30 * 5


def test_need_2m_is_demand_minus_available_plus_inbound():
    df = rr.normalize(raw(), "US")
    r = rr.need_2m(df).set_index("asin")
    assert r.loc["B0AAAAAAA1", "need_units"] == 100 - (0 + 5)
    assert r.loc["B0AAAAAAA2", "need_units"] == 40 - 10
    assert "B0AAAAAAA4" not in r.index  # 50 − (50+10) < 0 → не нужен
    assert r.loc["B0AAAAAAA2", "lost_eur"] == 30 * 20  # цены хватает, готового Lost Sale нет


def test_grouped_like_nina_sorted_by_money():
    df = rr.normalize(raw(), "US")
    g = rr.grouped(rr.need_2m(df))
    assert list(g.columns[:3]) == ["ab", "category", "color"]
    assert g.iloc[0]["lost_eur"] >= g.iloc[-1]["lost_eur"]
    assert g["asins"].sum() == 3


def test_diff_and_state_roundtrip(tmp_path):
    added, removed = rr.diff_lists({"B0A", "B0B"}, {"B0B", "B0C"})
    assert added == ["B0C"] and removed == ["B0A"]
    rr.save_state(tmp_path / "s.json", {"US:now": ["B0B"]})
    assert rr.load_state(tmp_path / "s.json") == {"US:now": ["B0B"]}
    assert rr.load_state(tmp_path / "missing.json") == {}


def test_render_text_has_both_reports_and_top_groups():
    df = rr.normalize(raw(), "US")
    now, plan = rr.out_now(df), rr.need_2m(df)
    t = rr.render_text("US", now, plan, ["B0AAAAAAA1"], [], [], ["B0ZZZZZZZZ"], url="https://x")
    assert "Аут сейчас: 2 ASIN (+1 / −0)" in t and "Срочно к рестоку" in t and "Топ групп" in t and t.endswith("https://x")
