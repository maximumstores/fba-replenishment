import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import nina_sheet as ns

H = ["#REF!", "", "Category", "Color", "Size", "Reorder", "Account", "A/B category+color for winter'25-'26", "Order Total", "MAX days",
     "K", "L", "Total q-ty", "Inbound+FC Transfer+FC Processing", "Available", "2026-10-01", "2026-11-01", "2026-12-01", "PLAN REST Q4",
     "PLAN REST OCT-NOV", "Price"]


def row(asin, avail, inb, rest, price, months=(9, 12, 11)):
    r = [""] * len(H)
    r[0], r[2], r[3], r[4], r[7] = asin, "Mens LS", "Black", "L", "A"
    r[13], r[14] = inb, avail
    r[15], r[16], r[17] = months
    r[H.index("PLAN REST OCT-NOV")], r[H.index("Price")] = rest, price
    return r


ROWS = [row("B08X1G126C", 0, 2, 17, 57.99), row("junk", 0, 0, 5, 1), row("B08X1G126D", 5, 0, 0, 10),
        row("B08X1G126C", 0, 2, 3, 57.99)]


def test_de_uses_plan_rest_and_price_and_drops_junk_and_noplan():
    d = ns.parse_orders(H, ROWS, "DE")
    assert list(d["asin"]) == ["B08X1G126C"] and d.loc[0, "demand_2m"] == 17 and d.loc[0, "price_eur"] == 57.99


def test_uk_plan_is_two_months_after_available():
    d = ns.parse_orders(H, ROWS, "UK", plan="months").set_index("asin")
    assert d.loc["B08X1G126D", "demand_2m"] == 21


def test_text_numbers_with_comma_and_euro():
    r = row("B08X1G126C", "0", "2", "17,5", "€57,99")
    assert ns.parse_orders(H, [r], "DE").loc[0, "demand_2m"] == 17.5


def test_missing_columns_raise():
    with pytest.raises(ValueError):
        ns.parse_orders(["a", "b"], [], "DE")
