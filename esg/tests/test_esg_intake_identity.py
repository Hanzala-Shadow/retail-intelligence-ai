from __future__ import annotations

import csv
import hashlib
import sys
import tempfile
from pathlib import Path


import esg_intake_catalog as intake
import pdf_parser


def _pdf(root: Path, ticker: str, name: str, content: bytes) -> Path:
    path = root / ticker / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _approval(original: Path, ocr: Path, **changes: str) -> dict:
    original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    ocr_hash = hashlib.sha256(ocr.read_bytes()).hexdigest()
    row = {
        "logical_source_id": intake.logical_source_id(f"sha256:{original_hash}"),
        "original_source_version_id": intake.source_version_id(original_hash),
        "original_sha256": original_hash,
        "ocr_artifact_id": intake.extraction_artifact_id("ocr_derivative", ocr_hash),
        "ocr_artifact_sha256": ocr_hash,
        "ocr_path": str(ocr.resolve()),
        "ocr_drive_id": "",
        "approval_status": "approved",
        "reviewer": "reviewer@example.com",
        "approval_date": "2026-07-23",
        "reason": "verified better page text",
        "state": "active",
    }
    row.update(changes)
    return row


def test_renamed_exact_duplicate_is_one_version_and_one_alias_is_excluded() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        first = _pdf(root, "AAA", "first.pdf", b"same report")
        second = _pdf(root, "AAA", "renamed.pdf", b"same report")
        rows = intake.build_catalog(
            [
                intake.IntakeFile(first, "AAA", "original"),
                intake.IntakeFile(second, "AAA", "original"),
            ],
            cataloged_at="fixed",
        )

    assert len({row["logical_source_id"] for row in rows}) == 1
    assert len({row["source_version_id"] for row in rows}) == 1
    assert len({row["file_alias_id"] for row in rows}) == 2
    assert sorted(row["processing_state"] for row in rows) == [
        "eligible_candidate",
        "excluded_duplicate",
    ]


def test_exact_duplicate_under_another_ticker_requires_ownership_review() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        first = _pdf(root, "AAA", "report.pdf", b"same report")
        second = _pdf(root, "BBB", "report-copy.pdf", b"same report")
        rows = intake.build_catalog(
            [
                intake.IntakeFile(first, "AAA", "original"),
                intake.IntakeFile(second, "BBB", "original"),
            ],
            cataloged_at="fixed",
        )

    duplicate = next(row for row in rows if row["processing_state"] == "excluded_duplicate")
    assert duplicate["ownership_review_required"] == "true"
    assert duplicate["review_reason"] == "exact_duplicate_under_another_ticker"


def test_ocr_staging_is_cataloged_only_as_derivative() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        ocr = _pdf(root, "AAA", "report.pdf", b"ocr")
        row = intake.build_catalog(
            [intake.IntakeFile(ocr, "AAA", "ocr_derivative")],
            cataloged_at="fixed",
        )[0]
    assert row["artifact_role"] == "ocr_derivative"
    assert row["processing_state"] == "held_for_approval"
    assert row["canonical_alias"] == "false"


def test_unapproved_ocr_filename_match_is_ignored() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        raw = _pdf(root / "raw", "AAA", "report.pdf", b"raw")
        _pdf(root / "ocr", "AAA", "report.pdf", b"ocr")
        raw_hash = hashlib.sha256(raw.read_bytes()).hexdigest()
        selected = pdf_parser.select_parse_source(
            raw, "AAA", root / "ocr", source_sha256=raw_hash, approvals=[]
        )
    assert selected.path == raw
    assert selected.reason == "unapproved_ocr_ignored"


def test_approved_ocr_is_selected_and_changed_hash_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        raw = _pdf(root / "raw", "AAA", "report.pdf", b"raw")
        ocr = _pdf(root / "ocr", "AAA", "report.pdf", b"good ocr")
        approval = _approval(raw, ocr)
        raw_hash = hashlib.sha256(raw.read_bytes()).hexdigest()
        selected = pdf_parser.select_parse_source(
            raw, "AAA", root / "ocr", source_sha256=raw_hash, approvals=[approval]
        )
        assert selected.path == ocr
        assert selected.approval_status == "approved"

        ocr.write_bytes(b"changed ocr")
        stale = pdf_parser.select_parse_source(
            raw, "AAA", root / "ocr", source_sha256=raw_hash, approvals=[approval]
        )
    assert stale.path == raw
    assert stale.reason == "approved_ocr_hash_mismatch"


def test_approved_ocr_can_use_a_different_filename_and_stale_original_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        raw = _pdf(root / "raw", "AAA", "report.pdf", b"raw")
        ocr = _pdf(root / "ocr", "AAA", "searchable-copy-with-new-name.pdf", b"good ocr")
        approval = _approval(raw, ocr)
        raw_hash = hashlib.sha256(raw.read_bytes()).hexdigest()
        selected = pdf_parser.select_parse_source(
            raw, "AAA", root / "ocr", source_sha256=raw_hash, approvals=[approval]
        )
        assert selected.path == ocr
        assert selected.kind == "ocr"

        stale = pdf_parser.select_parse_source(
            raw, "AAA", root / "ocr", source_sha256="f" * 64, approvals=[approval]
        )
    assert stale.path == raw
    assert stale.reason == "approved_ocr_original_hash_mismatch"


def test_catalog_upsert_is_idempotent() -> None:
    row = {field: "" for field in intake.CATALOG_FIELDS}
    row.update({"file_alias_id": "fa_1", "file_path": "a.pdf"})
    assert intake.merge_catalog([row], [row]) == [row]
