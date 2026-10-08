from datetime import date

import pandas as pd

import usage


def _logins(rows):
    df = pd.DataFrame(rows, columns=["email", "logged_in_at"])
    df["logged_in_at"] = pd.to_datetime(df["logged_in_at"]).dt.tz_localize(usage.KYIV)
    return df


def test_regularity_matches_bsr_radar_screenshot():
    # неделя пн 28.09 – вс 04.10: a.borulko 3 рабочих дня (15 входов), v.tereshyn 4 дня (6 входов) → 70%
    rows = [("a.borulko@x", f"2026-09-28 10:{i:02d}") for i in range(5)]
    rows += [("a.borulko@x", f"2026-09-29 10:{i:02d}") for i in range(5)]
    rows += [("a.borulko@x", f"2026-09-30 10:{i:02d}") for i in range(5)]
    rows += [("v.tereshyn@x", f"2026-09-{d} 11:00") for d in (28, 29, 30)] + [("v.tereshyn@x", "2026-10-01 11:00")] * 3
    r = usage.regularity(_logins(rows), date(2026, 10, 4), 7)
    assert r["workdays"] == 5 and r["came"] == 2 and r["base"] == 2
    assert round(r["pct"]) == 70 and abs(r["avg_days"] - 3.5) < 1e-9


def test_base_includes_everyone_who_ever_logged_in():
    rows = [("old@x", "2026-08-01 10:00"), ("new@x", "2026-10-01 10:00")]
    r = usage.regularity(_logins(rows), date(2026, 10, 4), 7)
    assert r["base"] == 2 and r["came"] == 1
    assert round(r["pct"]) == 10  # (0 + 1/5) / 2


def test_weekend_logins_do_not_count_as_workdays_but_count_as_logins():
    r = usage.regularity(_logins([("a@x", "2026-10-03 10:00")]), date(2026, 10, 4), 7)  # суббота
    assert r["pct"] == 0 and r["came"] == 1


def test_empty_logs():
    r = usage.regularity(pd.DataFrame(columns=["email", "logged_in_at"]), date(2026, 10, 4), 7)
    assert r["pct"] == 0 and r["base"] == 0


def test_weekly_scorecard_has_rows_and_utc_to_kyiv(monkeypatch):
    df = pd.DataFrame({"email": ["a@x"], "logged_in_at": pd.to_datetime(["2026-10-05 22:30"], utc=True)})
    df["logged_in_at"] = df["logged_in_at"].dt.tz_convert(usage.KYIV)  # 23:30 UTC+... → уже 06.10 по Киеву
    assert df["logged_in_at"].dt.date.iloc[0] == date(2026, 10, 6)
    w = usage.weekly_scorecard(df, date(2026, 10, 8), weeks=3)
    assert len(w) == 3 and {"Неделя", "Регулярность, %", "Зашли"} <= set(w.columns)


def test_logging_never_raises_and_remembers_error(monkeypatch):
    monkeypatch.setattr(usage, "_client", lambda: (_ for _ in ()).throw(PermissionError("нет прав")))
    usage._write("SELECT 1", {"email": "a@x"})
    assert "PermissionError" in usage.LAST_ERROR["write"]


def test_logging_off_or_no_email_does_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(usage, "_write", lambda *a, **k: called.append(1))
    monkeypatch.setenv("USAGE_LOG", "off")
    usage.log_login("a@x")
    monkeypatch.setenv("USAGE_LOG", "on")
    usage.log_login("")
    assert called == []
