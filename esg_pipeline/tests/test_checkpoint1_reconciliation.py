import csv
import importlib.util
import sys
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "esg"
    / "scripts"
    / "esg_database_tiers_2"
    / "checkpoint1_reconciliation.py"
)
SPEC = importlib.util.spec_from_file_location("checkpoint1_reconciliation", SCRIPT)
checkpoint1 = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = checkpoint1
SPEC.loader.exec_module(checkpoint1)


def test_q9_excludes_drive_missing_rows_from_downloaded_losses(tmp_path, monkeypatch):
    tracker_path = tmp_path / "tracker.csv"
    with tracker_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["ticker", "report_year", "status", "notes", "drive_file_link"],
        )
        writer.writeheader()
        writer.writerows([
            {
                "ticker": "BBY",
                "report_year": "2021",
                "status": "downloaded",
                "notes": "BBY-2021.pdf",
                "drive_file_link": "downloaded-link",
            },
            {
                "ticker": "ETSY",
                "report_year": "2020",
                "status": "drive_missing",
                "notes": "ETSY-2020.pdf",
                "drive_file_link": "missing-link",
            },
        ])

    manifest = [{
        "doc_id": 1,
        "filename": "BBY-2021.pdf",
        "ticker": "BBY",
        "report_year": 2021,
        "parse_status": "parsed",
        "doc_quality_status": "usable",
        "page_count": 10,
        "byte_size": 100,
        "chunk_count": 3,
        "eligible_chunk_count": 3,
    }]
    monkeypatch.setattr(checkpoint1.config, "SUSTAINABILITY_TRACKER_CSV", tracker_path)

    result = checkpoint1.check_q9(manifest, tmp_path, examples_wanted=5)

    assert result.status == "PASS"
    assert result.stats["tracker_rows_marked_downloaded"] == 1
    assert result.stats["tracker_rows_not_marked_downloaded"] == 1
    assert result.stats["by_loss_class"]["no_document_row"] == 0
    unavailable = list(csv.DictReader((tmp_path / "unavailable_source_history.csv").open()))
    assert unavailable[0]["tracker_status"] == "drive_missing"
    assert unavailable[0]["ticker"] == "ETSY"
