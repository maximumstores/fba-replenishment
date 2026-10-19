import pandas as pd

import market_stock as ms


def test_category_parent_map_and_apply():
    dim = pd.DataFrame({"asin": ["B0AAAAAAA1", "B0AAAAAAA2"], "old_asin": ["B0OLDOLD01", None],
                        "group_key": ["Mens Sets 320", "W Pants 250"], "category": ["Mens Sets", "W Pants"]})
    cp = ms.category_parent_map(dim)
    df = pd.DataFrame({"asin": ["B0AAAAAAA1", "B0OLDOLD01", "B0ZZZZZZZ9"], "category": ["Mens Sets", "Mens Sets", "Socks"]})
    out = ms.apply_category_parent(df, cp)
    assert out["category"].tolist() == ["Mens Sets 320", "Mens Sets 320", "Socks"]


def test_drop_never_sold_keeps_relisted():
    dim = pd.DataFrame({"asin": ["B0NEWNEW01", "B0NEWNEW02"], "old_asin": ["B0OLDOLD01", None]})
    df = pd.DataFrame({"asin": ["B0NEWNEW01", "B0NEWNEW02", "B0SOLDSOL1"]})
    out = ms.drop_never_sold(df, {"B0OLDOLD01", "B0SOLDSOL1"}, dim)
    assert out["asin"].tolist() == ["B0NEWNEW01", "B0SOLDSOL1"]


def test_parse_sources_and_split():
    import restock_report as rr
    rows = [["01.10.2026"], ["ASIN", "Old ASIN", "AWD US", "WRH US+ Inbound", "Order US", "WRH DE+ Inbound", "Order DE", "AWD DE"],
            ["B0AAAAAAA1", "", "50", "30", "100", "7", "0", "1"]]
    src = ms.parse_sources(rows)
    assert src["US"].iloc[0][["awd", "wrh", "order_qty"]].tolist() == [50, 30, 100]
    assert src["DE"].iloc[0][["awd", "wrh", "order_qty"]].tolist() == [1, 7, 0]
    df = pd.DataFrame({"market": "US", "ab": "A", "category": "X", "color": "c", "size": "M", "asin": ["B0AAAAAAA1"],
                       "available": [0.0], "inbound": [0.0], "demand_2m": [200.0], "lost_eur": [0.0], "price_eur": [1.0]})
    r = rr.out_now(ms.add_sources(df, src["US"]))
    assert r.iloc[0][["need_units", "can_send", "from_order", "no_source"]].tolist() == [200, 80, 100, 20]
