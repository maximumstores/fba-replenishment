import math
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import calc
from config import Settings

TODAY = date(2026, 10, 7)
S = Settings()  # AWD 7, склад 14, воздух 10, приход: receiving 3 / shipped 14


def hopted_row(asin="B0AAAAAAA1", store="S1", fulfil=0, recv=0, shipped=0, working=0, u7=0, u30=0, **extra):
    row = {
        "ASIN": asin, "Store name": store, "SKU": "sku", "Product name": "p",
        "FBA fulfillable quantity": fulfil, "FBA inbound receiving quantity": recv,
        "FBA inbound shipped quantity": shipped, "FBA inbound working quantity": working,
        "Units shipped last 7 days": u7, "Units shipped last 30 days": u30,
    }
    row.update(extra)
    return row


def report(rows, reference=None, sources=None, batches=None, settings=S):
    return calc.build_report(pd.DataFrame(rows), reference, sources, batches, settings, TODAY)


# ───── simulate ─────

def test_no_sales_never_stocks_out():
    r = calc.simulate(0, 0, [], 90)
    assert r.stockout_day is None and r.lost_30 == 0


def test_stock_lasts_exactly_n_days():
    # 100 шт, 10/день: последний полный день продаж — день 9, на день 10 не хватает
    r = calc.simulate(100, 10, [], 90)
    assert r.stockout_day == 10


def test_zero_stock_is_out_today():
    assert calc.simulate(0, 5, [], 90).stockout_day == 0


def test_inbound_prevents_stockout():
    # 30 шт. хватает на 3 дня, на 2-й день приходит тысяча: 1030 шт. на ~103 дня, дальше горизонта 90
    r = calc.simulate(30, 10, [(2, 1000)], 90)
    assert r.stockout_day is None and r.lost_horizon == 0
    # а с горизонтом 120 дней обнуление видно, и оно позже 50-го дня
    assert calc.simulate(30, 10, [(2, 1000)], 120).stockout_day > 50


def test_inbound_arrives_too_late_creates_gap():
    # 20 шт, 10/день → кончится на 2-й день; приход на 10-й день
    r = calc.simulate(20, 10, [(10, 500)], 90)
    assert r.stockout_day == 2
    assert r.lost_30 == pytest.approx(8 * 10)  # дни 2..9 без товара


def test_lost_units_respect_horizon():
    r = calc.simulate(0, 10, [], 90)
    assert r.lost_30 == 300 and r.lost_horizon == 900


# ───── каналы и статусы ─────

def test_channel_cheapest_in_time():
    ch, ok = calc.pick_channel(20, S.leads, {"AWD": None, "WAREHOUSE": None, "AIR": None})
    assert (ch, ok) == ("AWD", True)


def test_channel_skips_unavailable_awd():
    ch, ok = calc.pick_channel(20, S.leads, {"AWD": 0.0, "WAREHOUSE": 500.0, "AIR": None})
    # AWD пустой; склад (14 дн.) успевает до 20-го дня и он дешевле воздуха → берём склад
    assert (ch, ok) == ("WAREHOUSE", True)


def test_channel_only_air_in_time():
    ch, ok = calc.pick_channel(11, S.leads, {"AWD": 0.0, "WAREHOUSE": 0.0, "AIR": None})
    assert (ch, ok) == ("AIR", True)


def test_nothing_in_time_picks_fastest():
    ch, ok = calc.pick_channel(3, S.leads, {"AWD": None, "WAREHOUSE": None, "AIR": None})
    assert ch == "AWD" and ok is False  # самый быстрый по срокам (7 дн), но не успевает


def test_status_matrix():
    cls = lambda v, d, ch, ok: calc.classify(v, d, ch, ok, S)
    assert cls(0, None, None, True) == "NO_SALES"
    assert cls(5, None, None, True) == "OK"
    assert cls(5, 0, "AWD", False) == "OUT"
    assert cls(5, 3, "AWD", False) == "CRITICAL"
    assert cls(5, 11, "AIR", True) == "URGENT"
    assert cls(5, 15, "WAREHOUSE", True) == "ACTION"
    assert cls(5, 15, "AWD", True) == "PLAN"
    assert cls(5, 60, "AWD", True) == "OK"


# ───── сквозной расчёт ─────

def test_report_out_and_ok():
    rep = report([
        hopted_row("B0OUT00001", fulfil=0, u7=14, u30=60),
        hopted_row("B0FINE0001", fulfil=3000, u7=7, u30=30),
        hopted_row("B0DEAD0001", fulfil=0, u7=0, u30=0),
    ])
    st = dict(zip(rep["asin"], rep["status"]))
    assert st["B0OUT00001"] == "OUT"
    assert st["B0FINE0001"] == "OK"
    assert st["B0DEAD0001"] == "NO_SALES"
    assert rep.iloc[0]["asin"] == "B0OUT00001"  # самое срочное сверху


def test_inbound_shipped_counts_with_delay():
    # 2/день, на складе 10 → кончится на 5-й день; shipped приходит на 14-й день → дыра
    rep = report([hopted_row(fulfil=10, shipped=500, u7=14, u30=60)])
    r = rep.iloc[0]
    assert r["stockout_day"] == 5
    assert r["status"] in ("CRITICAL", "URGENT", "ACTION", "PLAN")
    assert r["lost_30"] > 0 and r["next_arrival_day"] == 14


def test_working_inbound_excluded_by_default_and_included_on_request():
    base = hopted_row(fulfil=10, working=1000, u7=14, u30=60)
    assert report([base]).iloc[0]["inbound_counted"] == 0
    s2 = Settings(include_working=True)
    assert report([base], settings=s2).iloc[0]["inbound_counted"] == 1000


def test_qty_need_covers_gap_when_inbound_is_late():
    # нет товара, inbound на 358 шт. приедет через 14 дней и сам хватит надолго.
    # Но дыра до 14-го дня есть: заказ по воздуху (10 дн) должен покрыть дни 10..14
    rep = report([hopted_row(fulfil=0, shipped=358, u7=14, u30=60)])
    r = rep.iloc[0]
    assert r["status"] == "OUT"
    assert r["qty_need"] > 0


def test_qty_need_zero_when_ok():
    rep = report([hopted_row(fulfil=5000, u7=7, u30=30)])
    assert rep.iloc[0]["qty_need"] == 0


def test_availability_forces_channel():
    # стока хватит на ~20 дней; AWD пустой, склад есть → ACTION через склад
    src = pd.DataFrame({"asin": ["B0AAAAAAA1"], "awd_qty": [0], "wrh_qty": [500]})
    rep = report([hopted_row(fulfil=20, u7=7, u30=30)], sources=src)  # 1/день
    r = rep.iloc[0]
    assert r["stockout_day"] == 20
    assert r["channel"] == "WAREHOUSE" and r["status"] == "ACTION"


def test_batches_get_delay_and_are_not_double_counted():
    # партия 300 шт. с ETA через 5 дней: реальный приход = 5 + 14 дней запаса = 19-й день
    batches = pd.DataFrame({"asin": ["B0AAAAAAA1"], "qty": [300], "eta": ["12.10.2026"], "destination": ["AMZ"]})
    rows = [hopted_row(store="S1", fulfil=5, u7=14, u30=60), hopted_row(store="S2", fulfil=5, u7=7, u30=30)]
    rep = report(rows, batches=batches)
    assert rep["inbound_counted"].sum() == 300  # партия учтена один раз
    owner = rep.loc[rep["inbound_counted"] > 0].iloc[0]
    assert owner["store"] == "S1"  # у магазина с большими продажами
    assert owner["next_arrival_day"] == 19


def test_batches_other_destinations_ignored():
    batches = pd.DataFrame({"asin": ["B0AAAAAAA1"], "qty": [300], "eta": ["12.10.2026"], "destination": ["WRH"]})
    rep = report([hopted_row(fulfil=5, u7=14, u30=60)], batches=batches)
    assert rep.iloc[0]["inbound_counted"] == 0


def test_plan_velocity_not_duplicated_across_stores():
    ref = pd.DataFrame({"asin": ["B0AAAAAAA1"], "parent_group": ["G"], "color": ["c"], "size": ["M"],
                        "active_us": ["true"], "plan_units_month": ["90"]})
    rows = [hopted_row(store="S1", fulfil=0), hopted_row(store="S2", fulfil=0)]
    rep = report(rows, reference=ref, settings=Settings(plan_mode="fallback"))
    assert (rep["velocity"] > 0).sum() == 1 and rep["velocity"].sum() == pytest.approx(3.0)


def test_plan_fallback_for_dead_stock_with_plan():
    ref = pd.DataFrame({"asin": ["B0AAAAAAA1"], "parent_group": ["G"], "color": ["c"], "size": ["M"],
                        "active_us": ["true"], "plan_units_month": ["90"]})
    rep = report([hopted_row(fulfil=0, u7=0, u30=0)], reference=ref, settings=Settings(plan_mode="fallback"))
    r = rep.iloc[0]
    assert r["velocity_source"] == "план" and r["velocity"] == pytest.approx(3.0)
    assert r["status"] == "OUT"


def test_plan_off_keeps_no_sales():
    ref = pd.DataFrame({"asin": ["B0AAAAAAA1"], "parent_group": ["G"], "color": ["c"], "size": ["M"],
                        "active_us": ["true"], "plan_units_month": ["90"]})
    rep = report([hopted_row(fulfil=0)], reference=ref, settings=Settings(plan_mode="off"))
    assert rep.iloc[0]["status"] == "NO_SALES"


def test_reserved_transfer_counts_only_up_to_reserved():
    row = hopted_row(fulfil=10, u7=7, u30=30, **{"Reserved FC Transfer": 5, "Reserved FC Processing": 5, "FBA reserved quantity": 4})
    assert report([row]).iloc[0]["on_hand"] == 14
    row2 = hopted_row(fulfil=10, u7=7, u30=30, **{"Reserved FC Transfer": 5, "FBA reserved quantity": 0})
    assert report([row2]).iloc[0]["on_hand"] == 10


# ───── грязные данные ─────

def test_prepare_hopted_cleans_numbers_and_merges_duplicates():
    raw = pd.DataFrame([
        hopted_row("b0aaaaaaa1", fulfil="1,234", u30="  60 ", u7=""),
        hopted_row("B0AAAAAAA1", fulfil="6", u30="0", u7="7"),
        hopted_row("", fulfil=99),
        hopted_row("W-SS-SX-LW-00228", fulfil=1),
    ])
    h = calc.prepare_hopted(raw)
    a = h[h["asin"] == "B0AAAAAAA1"].iloc[0]
    assert a["fulfillable"] == 1240 and a["units30"] == 60 and a["units7"] == 7 and a["n_rows"] == 2
    assert len(h) == 2
    assert h.set_index("asin").loc["W-SS-SX-LW-00228", "real_asin"] == False  # noqa: E712


def test_prepare_hopted_missing_columns_is_clear_error():
    with pytest.raises(ValueError, match="FBA fulfillable quantity"):
        calc.prepare_hopted(pd.DataFrame({"ASIN": ["B0AAAAAAA1"]}))


def test_parse_dates_text_and_serial():
    s = pd.Series(["04.10.2026", "46299", "", "мусор"])
    out = calc.parse_dates(s)
    assert out.iloc[0] == pd.Timestamp(2026, 10, 4)
    assert out.iloc[1] == pd.Timestamp("2026-10-04")  # серийный номер Google Sheets (46299)
    assert pd.isna(out.iloc[2]) and pd.isna(out.iloc[3])


def test_reference_filters_inactive_and_flags_unknown():
    ref = pd.DataFrame({
        "asin": ["B0AAAAAAA1", "B0INACTIVE"], "parent_group": ["G", "G"], "color": ["c", "c"],
        "size": ["M", "M"], "active_us": ["true", "false"],
    })
    rows = [hopted_row("B0AAAAAAA1", u7=7, u30=30), hopted_row("B0INACTIVE", u7=7, u30=30),
            hopted_row("B0UNKNOWN1", u7=7, u30=30)]
    rep = report(rows, reference=ref)
    assert set(rep["asin"]) == {"B0AAAAAAA1", "B0UNKNOWN1"}
    assert rep.set_index("asin").loc["B0UNKNOWN1", "parent_group"] == "(нет в справочнике)"


def test_empty_after_filter_returns_empty_report():
    ref = pd.DataFrame({"asin": ["B0AAAAAAA1"], "parent_group": ["G"], "color": ["c"], "size": ["M"], "active_us": ["false"]})
    rep = report([hopted_row("B0AAAAAAA1", u7=7, u30=30)], reference=ref)
    assert rep.empty and "status" in rep.columns
    assert calc.summarize_groups(rep).empty


# ───── ABC и сводки ─────

def test_abc_split():
    df = pd.DataFrame({"asin": list("ABCDE"), "units30": [800, 100, 50, 30, 0]})
    out = calc.assign_abc(df, S)
    assert out.tolist() == ["A", "B", "C", "C", "D"] or out.tolist() == ["A", "B", "B", "C", "D"]
    assert out.iloc[0] == "A" and out.iloc[-1] == "D"


def test_abc_single_asin_is_a():
    df = pd.DataFrame({"asin": ["A"], "units30": [10]})
    assert calc.assign_abc(df, S).iloc[0] == "A"


def test_group_summary_takes_worst_status():
    ref = pd.DataFrame({"asin": ["B0AAAAAAA1", "B0AAAAAAA2"], "parent_group": ["G", "G"], "color": ["c", "c"],
                        "size": ["M", "M"], "active_us": ["true", "true"]})
    rows = [hopted_row("B0AAAAAAA1", fulfil=5000, u7=7, u30=30), hopted_row("B0AAAAAAA2", fulfil=0, u7=7, u30=30)]
    g = calc.summarize_groups(report(rows, reference=ref))
    assert len(g) == 1 and g.iloc[0]["status"] == "OUT" and g.iloc[0]["asins"] == 2


def test_data_quality_counts():
    ref = pd.DataFrame({"asin": ["B0AAAAAAA1", "B0MISSING1", "W-SS-00001"], "parent_group": ["G", "G", "G"],
                        "color": ["c", "", "c"], "size": ["M", "M", "M"], "active_us": ["true", "true", "true"],
                        "abcd_class": ["#N/A", "AAA", ""]})
    h = calc.prepare_hopted(pd.DataFrame([hopted_row("B0AAAAAAA1"), hopted_row("B0ZZZZZZZ9")]))
    q = calc.data_quality(h, ref)
    assert q["not_in_reference"] == ["B0ZZZZZZZ9"]
    assert q["reference_active_missing_in_hopted"] == ["B0MISSING1", "W-SS-00001"]
    assert q["reference_without_color_or_size"] == 1
    assert q["no_abc_planner"] == 2


# ───── море / воздух и заказы в производстве ─────

def _src(awd=0, wrh=0, order=100):
    return pd.DataFrame({"asin": ["B0AAAAAAA1"], "awd_qty": [awd], "wrh_qty": [wrh], "order_qty": [order]})


def test_sea_chosen_when_stockout_is_far_and_nothing_in_us():
    # 10 шт. при 0.2/день → запас на 50 дней: море (30) успевает, воздух не нужен
    rep = report([hopted_row(fulfil=10, u7=1, u30=6)], sources=_src())
    r = rep.iloc[0]
    assert r["channel"] == "SEA" and r["channel_in_time"] and r["status"] in ("PLAN", "OK")


def test_air_when_sea_is_too_slow():
    rep = report([hopted_row(fulfil=10, u7=7, u30=30)], sources=_src())  # 1/день → аут через 10 дней
    r = rep.iloc[0]
    assert r["channel"] == "AIR" or r["status"] == "CRITICAL"


def test_no_channel_when_nothing_ordered_and_nothing_in_us():
    rep = report([hopted_row(fulfil=0, u7=7, u30=30)], sources=_src(order=0))
    r = rep.iloc[0]
    assert r["channel"] == "" and r["status"] in ("OUT", "CRITICAL")
