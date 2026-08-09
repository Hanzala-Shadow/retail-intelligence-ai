from __future__ import annotations

import csv
import importlib.util
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPANIES = ROOT / "data_v2" / "00_reference" / "approved_companies.csv"


def rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_approved_companies_contract() -> None:
    data = rows(COMPANIES)
    assert len(data) == 190
    assert len({row["company_id"] for row in data}) == 190
    assert len({row["ticker"] for row in data}) == 190
    assert len({row["cik"] for row in data}) == 190
    assert {"APC", "ARKO", "XOM", "TBHC"}.isdisjoint(
        {row["ticker"] for row in data}
    )
    assert sum(row["ticker"] == "YSWY" for row in data) == 1
    assert Counter(row["fiscal_year_end_status"] for row in data) == Counter(
        {"SOURCE_VERIFIED": 186, "MANUAL_VERIFIED": 4}
    )


def test_fiscal_dates_are_valid() -> None:
    import calendar

    for row in rows(COMPANIES):
        month = int(row["fiscal_year_end_month"])
        day = int(row["fiscal_year_end_day"])
        assert 1 <= month <= 12
        assert 1 <= day <= calendar.monthrange(2024, month)[1]


def test_v2_config_rejects_v1_database(monkeypatch) -> None:
    path = ROOT / "src" / "config_v2.py"
    monkeypatch.setenv("RETAIL_V2_DB_URL", "postgresql://localhost/retail_pipeline")
    spec = importlib.util.spec_from_file_location("config_v2_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    try:
        module.validate_v2_database_url()
    except RuntimeError as exc:
        assert "retail_pipeline_fy2325_v2" in str(exc)
    else:
        raise AssertionError("v1 database URL was not rejected")
