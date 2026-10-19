import pandas as pd

import market_stock as ms


def test_category_parent_map_and_apply():
    dim = pd.DataFrame({"asin": ["B0AAAAAAA1", "B0AAAAAAA2"], "old_asin": ["B0OLDOLD01", None],
                        "group_key": ["Mens Sets 320", "W Pants 250"], "category": ["Mens Sets", "W Pants"]})
    cp = ms.category_parent_map(dim)
    df = pd.DataFrame({"asin": ["B0AAAAAAA1", "B0OLDOLD01", "B0ZZZZZZZ9"], "category": ["Mens Sets", "Mens Sets", "Socks"]})
    out = ms.apply_category_parent(df, cp)
    assert out["category"].tolist() == ["Mens Sets 320", "Mens Sets 320", "Socks"]
