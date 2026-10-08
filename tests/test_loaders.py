import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import alert
import calc
import loaders
from config import Settings

TODAY = date(2026, 10, 7)

# Настоящая шапка листа US V2 (A:AG) и три настоящие строки из него: с дублями «…1», пустыми колонками и формулами справа.
REAL_HEADER = (
    "Hopted ID,ASIN,SKU,FNSKU,Product name,Store name,Inventory age snapshot date,FBA fulfillable quantity,"
    "FBA inbound receiving quantity,FBA inbound shipped quantity,Reserved FC Transfer,Reserved FC Processing,"
    "Units shipped last 7 days,Units shipped last 30 days,Days of supply,Sellable inventory age 271-365 days,"
    "Sellable inventory age 365+ days,FBA inbound working quantity,FBA reserved quantity,FBA total quantity,"
    "FNSKU1,ASIN1,FBA total quantity1,FBA inbound receiving quantity1,FBA inbound shipped quantity1,"
    "FBA inbound working quantity1,FBA reserved quantity1,FBA fulfillable quantity1,Inbound NEW,"
    '"UNIQUE(SORT(FILTER({D:D\\B:B};T:T<>0);2;FALSE()))",,,"UNIQUE(SORT(FILTER({D:D\\B:B\\T:T};T:T<>0);2;ЛОЖЬ()))"'
)
REAL_ROWS = [
    "hpt_00025,B0CKHXQ6XR,A8-ZLZK-Y20E,B0CKHXQ6XR,Boxer Briefs XXL,eLeaf,,0,0,0,0,0,0,2,366,,,0,0,0,B0HCC32615,B0HCC32615,15,0,15,0,0,0,0,,,,",
    "hpt_00043,B0CKHWH7FP,H5-QOC5-N9F8,B0CKHWH7FP,Boxer Briefs Small,eLeaf,04.10.2026,227,0,0,1,4,6,45,366,0,,0,4,231,B0HCC2VVRB,B0HCC2VVRB,34,0,34,0,0,0,0,,,,",
    "hpt_00049,B0BC1G9DF7,K8-BSGF-M4RL,B0BC1G9DF7,Hiking Socks,eLeaf,,0,0,0,0,0,0,0,,,,0,0,0,B0HCC1NQ87,B0HCC1NQ87,28,0,28,0,0,0,0,,,,",
]


@pytest.fixture
def real_csv(tmp_path):
    p = tmp_path / "us_v2.csv"
    p.write_text("\n".join([REAL_HEADER, *REAL_ROWS]) + "\n", encoding="utf-8")
    return p


def test_real_sheet_layout_is_understood(real_csv):
    raw = loaders.load_hopted(str(real_csv))
    rep = calc.build_report(raw, None, None, None, Settings(), TODAY).set_index("asin")
    assert len(rep) == 3
    # 227 доступно + min(1+4, reserved 4) = 231; продажи 0.5·(6/7) + 0.5·(45/30) ≈ 1.18/дн → ~196 дней запаса
    r = rep.loc["B0CKHWH7FP"]
    assert r["on_hand"] == 231 and r["status"] == "OK"
    assert r["coverage_days_now"] == pytest.approx(231 / (0.5 * 6 / 7 + 0.5 * 45 / 30))
    assert rep.loc["B0BC1G9DF7", "status"] == "NO_SALES"
    out = rep.loc["B0CKHXQ6XR"]  # 0 в стоке, 2 продажи в месяц: формально аут, но потери ничтожные
    assert out["status"] == "OUT" and out["lost_30"] < 2


def test_slow_mover_is_filtered_out_of_alert_by_threshold(real_csv):
    rep = calc.build_report(loaders.load_hopted(str(real_csv)), None, None, None, Settings(), TODAY)
    problem, new, _ = alert.select_alerts(rep, {}, "URGENT", min_lost=0)
    assert len(problem) == 1  # без порога вялый ASIN попадает в алерт
    problem, new, _ = alert.select_alerts(rep, {}, "URGENT", min_lost=5)
    assert problem.empty and new.empty


def test_rows_to_df_pads_ragged_rows():
    values = [["ASIN", "A", "B"], ["x"], ["y", 1], ["z", 1, 2, 3]]
    df = loaders._rows_to_df(values)
    assert df.shape == (3, 3)
    assert df.iloc[0].tolist() == ["x", "", ""] and df.iloc[2].tolist() == ["z", 1, 2]
    assert loaders._rows_to_df([]).empty


def test_google_sheet_source_reads_tab_and_range(monkeypatch):
    calls = {}

    class WS:
        def get(self, rng, value_render_option=None):
            calls["range"], calls["render"] = rng, value_render_option
            return [["ASIN", "FBA fulfillable quantity"], ["B0AAAAAAA1", 5]]

    class SH:
        def worksheet(self, title):
            calls["tab"] = title
            return WS()

    class GC:
        def open_by_key(self, key):
            calls["key"] = key
            return SH()

    import gspread
    monkeypatch.setattr(gspread, "service_account", lambda filename: (calls.setdefault("creds", filename), GC())[1])
    df = loaders.load_hopted("sheet:FILE_ID", credentials_file="sa.json")
    assert calls == {"creds": "sa.json", "key": "FILE_ID", "tab": "US V2", "range": "A:T", "render": "UNFORMATTED_VALUE"}
    assert df.iloc[0]["ASIN"] == "B0AAAAAAA1"


def test_google_sheet_source_needs_credentials(monkeypatch):
    monkeypatch.delenv("GOOGLE_CREDENTIALS_FILE", raising=False)
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    with pytest.raises(RuntimeError, match="GOOGLE_CREDENTIALS_FILE"):
        loaders.load_hopted("sheet:FILE_ID")


def test_reference_none_and_optional_files(tmp_path):
    assert loaders.load_reference("") is None
    assert loaders._optional_csv(None) is None
    assert loaders._optional_csv(str(tmp_path / "missing.csv")) is None


def test_bigquery_reference_query_mentions_needed_columns():
    sql = loaders.REFERENCE_SQL.format(project="p")
    for col in ("asin", "parent_group", "color", "size", "active_us", "plan_units_month"):
        assert col in sql
    assert "`p.forecast.dim_product`" in sql


SA = {"type": "service_account", "private_key": "k", "client_email": "x@y"}


def test_service_account_info_from_json_and_base64(monkeypatch):
    import base64, json
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    assert loaders.service_account_info() is None
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", json.dumps(SA))
    assert loaders.service_account_info() == SA
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", base64.b64encode(json.dumps(SA).encode()).decode())
    assert loaders.service_account_info() == SA
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", json.dumps({"type": "authorized_user"}))
    with pytest.raises(RuntimeError, match="сервисного аккаунта"):
        loaders.service_account_info()


def test_gspread_uses_service_account_from_env(monkeypatch):
    import json, gspread
    monkeypatch.delenv("GOOGLE_CREDENTIALS_FILE", raising=False)
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", json.dumps(SA))
    seen = {}

    class WS:
        def get(self, *a, **k):
            return [["ASIN"], ["B0AAAAAAA1"]]

    class SH:
        def worksheet(self, tab):
            return WS()

    class GC:
        def open_by_key(self, key):
            seen["key"] = key
            return SH()

    monkeypatch.setattr(gspread, "service_account_from_dict",
                        lambda info, scopes=None: (seen.setdefault("info", info), GC())[1])
    df = loaders.load_hopted("sheet:FILE_ID")
    assert seen["info"] == SA and seen["key"] == "FILE_ID" and df.iloc[0]["ASIN"] == "B0AAAAAAA1"
