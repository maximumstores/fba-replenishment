import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import restock_job as rj


def test_all_fresh_gives_empty_line():
    d = date(2026, 11, 18)
    assert rj.freshness_line({t: d for t in rj.FRESH_TABLES}, d) == ""


def test_stale_reports_oldest_date():
    today = date(2026, 11, 18)
    dates = {t: today for t in rj.FRESH_TABLES}
    dates["hopted_us_native"] = date(2026, 11, 17)
    dates["amazon_starting_balance_native"] = date(2026, 11, 16)
    assert rj.freshness_line(dates, today) == "остатки от 16.11, ночное обновление не прошло"
