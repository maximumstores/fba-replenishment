import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import calc
import loaders
import transit
from config import Settings

TODAY = date(2026, 10, 8)


def serial(d: str) -> int:
    return (pd.Timestamp(d) - pd.Timestamp("1899-12-30")).days


def ship_rows():
    return pd.DataFrame({
        "Маркет": ["US", "US", "US", "US", "DE"],
        "Тип доставки": ["FAST SEA", "FAST SEA", "Regular / Standart SEA", "FAST SEA", "DE SEA DDP"],
        "Статус": ["Доставлено", "Доставлено", "Доставлено", "В пути", "Доставлено"],
        "ETD": [serial("2026-06-01"), serial("2026-06-10"), serial("2026-06-01"), serial("2026-09-01"), serial("2026-06-01")],
        "ETA": [serial("2026-06-15"), serial("2026-06-24"), serial("2026-06-27"), serial("2026-09-20"), serial("2026-06-10")],
        "Дата доставки": [serial("2026-07-03"), serial("2026-07-11"), serial("2026-07-10"), "", serial("2026-06-12")],
    }).astype(str)


def test_transit_stats_medians_and_overdue():
    st = transit.transit_stats(ship_rows(), "US", TODAY)
    assert st["n"] == 3 and st["median"] == 32  # 32, 31, 39 → медиана 32
    assert st["in_transit"] == 1 and st["overdue"] == 1  # ETA 20.09 уже прошла
    assert st["overdue_median_days"] == (pd.Timestamp(TODAY) - pd.Timestamp("2026-09-20")).days
    assert set(st["table"]["Способ"]) == {"FAST SEA", "Regular / Standart SEA"}


def test_transit_stats_empty_and_missing_columns():
    assert transit.transit_stats(None) is None
    with pytest.raises(ValueError):
        transit.transit_stats(pd.DataFrame({"Маркет": ["US"]}))


def test_sea_lead_from_facts_unless_set_manually():
    st = transit.transit_stats(ship_rows(), "US", TODAY)
    assert transit.apply_fact_leads(Settings(), st, None).lead_sea == 32
    assert transit.apply_fact_leads(Settings(), st, "45").lead_sea == Settings().lead_sea
    assert transit.apply_fact_leads(Settings(), None, None).lead_sea == Settings().lead_sea


def test_clean_header_makes_names_unique():
    assert loaders._clean_header(["ASIN", "", None, "ASIN", "Qty"]) == ["ASIN", "__1", "__2", "ASIN__3", "Qty"]


def test_hopted_from_bigquery_finds_header_row(monkeypatch):
    frame = pd.DataFrame([
        ["junk", None, None], ["Hopted ID", "ASIN", "FBA fulfillable quantity"], ["hpt_1", "B0AAAAAAA1", "5"],
    ], columns=["string_field_0", "string_field_1", "string_field_2"])
    monkeypatch.setattr(loaders, "_bq_query", lambda sql: frame)
    df = loaders.load_hopted("bq")
    assert df.columns.tolist() == ["Hopted ID", "ASIN", "FBA fulfillable quantity"] and df.iloc[0]["ASIN"] == "B0AAAAAAA1"


def test_sources_from_bigquery_reads_columns_by_header_text(monkeypatch):
    header = ["ASIN", "Old ASIN", "Stock+In transit US", "AWD US", "WRH US+ Inbound", "Order US"]
    frame = pd.DataFrame([
        ["01.10.2026", "", "", "", "", ""], header, ["", "", "", "10", "20", "30"],
        ["B0AAAAAAA1", "", "7", "3", "4", "50"], ["W-CN-00038", "", "1", "1", "1", "1"],
    ], columns=[f"col_{c}" for c in "ABCDEF"])
    monkeypatch.setattr(loaders, "_bq_query", lambda sql: frame)
    df = loaders.load_sources("bq")
    assert df["asin"].tolist() == ["B0AAAAAAA1"]  # итоговая строка и внутренние коды отброшены
    prep = calc.prepare_sources(df)
    assert prep.iloc[0]["awd_qty"] == 3 and prep.iloc[0]["wrh_qty"] == 4 and prep.iloc[0]["order_qty"] == 50


def test_sources_from_bigquery_fails_clearly_when_layout_changed(monkeypatch):
    frame = pd.DataFrame([["ASIN", "x", "y"]], columns=["col_A", "col_B", "col_C"])
    monkeypatch.setattr(loaders, "_bq_query", lambda sql: frame)
    with pytest.raises(ValueError, match="AWD US"):
        loaders.load_sources("bq")


def test_sources_csv_accepts_manual_export_names(tmp_path):
    p = tmp_path / "s.csv"
    p.write_text("asin,fba_stock_transit,awd_us,wrh_us_inbound,order_us\nB0AAAAAAA1,5,2,3,40\n")
    prep = calc.prepare_sources(loaders.load_sources(str(p)))
    assert (prep.iloc[0]["awd_qty"], prep.iloc[0]["wrh_qty"], prep.iloc[0]["order_qty"]) == (2, 3, 40)


def test_incoming_becomes_month_end_batches(monkeypatch):
    raw = pd.DataFrame({"asin": ["B0AAAAAAA1"], "month": ["2026-10-01"], "incoming": ["120"]})
    monkeypatch.setattr(loaders, "_bq_query", lambda sql: raw)
    b = loaders.load_incoming("bq")
    assert b.iloc[0]["eta"] == "31.10.2026" and b.iloc[0]["destination"] == "AMZ"
    assert calc.prepare_batches(b, TODAY, Settings())["B0AAAAAAA1"][0][1] == 120


def test_load_all_builds_meta_for_demo_data(monkeypatch):
    monkeypatch.chdir(ROOT)
    data = loaders.load_all({})
    names = [m["Источник"] for m in data["meta"]]
    assert len(names) == 6 and any("Hopted" in n for n in names)
    status = {m["Источник"]: m["Статус"] for m in data["meta"]}
    assert status["Приходы из планировщика (по месяцам)"] == "нет"


def test_dashboard_renders_sources_tab(monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.chdir(ROOT)
    monkeypatch.delenv("LEAD_SEA", raising=False)
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()
    assert not at.exception
    subheaders = [s.value for s in at.subheader]
    assert "Откуда данные" in subheaders and "Сроки по каналам: что факт, что допущение" in subheaders


def test_shipments_failure_does_not_break_load_all(monkeypatch):
    env = {"HOPTED_SOURCE": "demo/hopted_us.csv", "REFERENCE_SOURCE": "demo/reference.csv",
           "SOURCES_SOURCE": "", "SHIPMENTS_SOURCE": "sheet:X"}

    def boom(*a, **k):
        raise PermissionError

    monkeypatch.setattr(loaders, "load_shipments", boom)
    data = loaders.load_all(env)
    assert data["shipments"] is None and data["transit"] is None
    assert "PermissionError" in data["warnings"][0]


def test_awd_live_report_replaces_snapshot_and_zeroes_missing():
    import loaders
    rows = [["Product Name", "", "ASIN", "Available in AWD"], [], ["Product name", "SKU", "ASIN", "On-hand quantity"],
            ["x", "s1", "B0AAAAAAA1", 100], ["y", "s2", "B0AAAAAAA1", 20], ["z", "s3", "B0AAAAAAA3", "7"], ["junk", "", "n/a", 5]]
    live = loaders.awd_from_rows(rows)
    assert live.set_index("asin")["awd_live"].to_dict() == {"B0AAAAAAA1": 120.0, "B0AAAAAAA3": 7.0}
    snap = pd.DataFrame({"asin": ["B0AAAAAAA1", "B0AAAAAAA2"], "awd_qty": [100, 50],
                         "wrh_qty": [5, 6], "order_qty": [1, 2], "fba_stock_transit": [0, 0]})
    out, note = loaders.apply_live_awd(snap, live)
    got = out.set_index("asin")
    assert got.loc["B0AAAAAAA1", "awd_qty"] == 120 and got.loc["B0AAAAAAA2", "awd_qty"] == 0
    assert got.loc["B0AAAAAAA3", "awd_qty"] == 7 and got.loc["B0AAAAAAA2", "wrh_qty"] == 6 and note.startswith("AWD взят")


def test_awd_live_rejected_when_sum_is_absurd():
    import loaders
    live = pd.DataFrame({"asin": ["B0AAAAAAA1"], "awd_live": [1.0]})
    snap = pd.DataFrame({"asin": ["B0AAAAAAA1"], "awd_qty": [1000], "wrh_qty": [0], "order_qty": [0], "fba_stock_transit": [0]})
    out, note = loaders.apply_live_awd(snap, live)
    assert out is snap and "не применён" in note


def test_live_warehouse_total_minus_germany_and_applied():
    import loaders
    total = [["", "SUM of 74558"], ["Grand Total", 999]] + [[f"B0{i:08d}", 10 + i] for i in range(60)]
    blocks = [["FFbox CA", "", "", "FFBox DE", "", "", "FF box FL"], ["asin", "SUM of Stock", "", "asin", "SUM of Stock"],
              ["B000000001", 5, "", "B000000001", 4], ["B000000002", 7, "", "B000000002", 100]]
    live = loaders.warehouse_live_from_rows(total, blocks, ("DE",)).set_index("asin")["wrh_live"]
    assert live["B000000001"] == 11 - 4 and live["B000000002"] == 0 and live["B000000003"] == 13 and len(live) == 60
    snap = pd.DataFrame({"asin": ["B000000001", "B0ZZZZZZZZ"], "awd_qty": [1, 2], "wrh_qty": [900, 800],
                         "order_qty": [3, 4], "fba_stock_transit": [0, 0]})
    out, note = loaders.apply_live_wrh(snap, live.reset_index())
    got = out.set_index("asin")
    assert got.loc["B000000001", "wrh_qty"] == 7 and got.loc["B0ZZZZZZZZ", "wrh_qty"] == 0 and got.loc["B0ZZZZZZZZ", "awd_qty"] == 2
    assert note.startswith("Склад взят")


def test_live_warehouse_empty_is_not_applied():
    import loaders
    snap = pd.DataFrame({"asin": ["B000000001"], "awd_qty": [1], "wrh_qty": [900], "order_qty": [0], "fba_stock_transit": [0]})
    out, note = loaders.apply_live_wrh(snap, pd.DataFrame({"asin": [], "wrh_live": []}))
    assert out is snap and "не применён" in note
