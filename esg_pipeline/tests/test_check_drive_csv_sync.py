from apply_drive_truth_sync import sync_catalog
from check_drive_csv_sync import Audit, check_derived_index, filename_from_tracker, key
from config import RAW_SUSTAINABILITY_DIR, as_repo_relative


def test_keys_are_case_insensitive_for_windows_pdf_names():
    assert key("bby", "Report.PDF") == ("BBY", "report.pdf")


def test_tracker_filename_removes_span_note():
    row = {"notes": "GES-GUESS INC-2020-2021.pdf (filename spans 2020-2021)"}
    assert filename_from_tracker(row) == "GES-GUESS INC-2020-2021.pdf"


def test_derived_index_flags_documents_outside_drive_manifest():
    audit = Audit()
    manifest = {("AAP", "AAP-REPORT.PDF"): {}}
    rows = [
        {"ticker": "AAP", "pdf_stem": "AAP-REPORT"},
        {"ticker": "OLD", "pdf_stem": "OLD-REPORT"},
    ]

    check_derived_index(audit, "sections", rows, manifest)

    assert [issue.code for issue in audit.errors] == ["sections_stale"]


def test_catalog_replacement_keeps_old_row_and_adds_new_unlinked_row(tmp_path):
    item = ("BBY", "BBY-REPORT-2016.pdf")
    existing = [
        {
            "logical_source_id": "ls_old",
            "source_version_id": "sv_old",
            "file_alias_id": "fa_old",
            "extraction_artifact_id": "ea_old",
            "canonical_ticker": "BBY",
            "observed_ticker": "BBY",
            "pdf_file": item[1],
            "file_path": as_repo_relative(RAW_SUSTAINABILITY_DIR / "BBY" / item[1]),
            "drive_id": "old-drive-id",
            "sha256": "old-sha",
            "size_bytes": "10",
            "active": "true",
            "processing_state": "eligible_candidate",
            "cataloged_at": "old-time",
        }
    ]
    manifest = {
        key(*item): {
            "ticker": "BBY",
            "drive_file_name": item[1],
            "drive_file_id": "new-drive-id",
            "local_file": str(tmp_path / "BBY-REPORT-2016.pdf"),
            "local_size_bytes": "20",
        }
    }

    rows, _updated, _missing, lineage_review, replacements = sync_catalog(
        existing,
        manifest,
        {key(*item): "new-sha"},
        tmp_path,
        False,
        "new-time",
    )

    assert replacements == 1
    assert lineage_review == 1
    assert len(rows) == 2
    old, current = rows
    assert old["active"] == "false"
    assert old["source_version_id"] == "sv_old"
    assert old["processing_state"] == "drive_replaced_history"
    assert current["active"] == "true"
    assert current["drive_id"] == "new-drive-id"
    assert current["sha256"] == "new-sha"
    assert current["source_version_id"] == ""
    assert current["processing_state"] == "drive_replaced_needs_lineage_rebuild"
