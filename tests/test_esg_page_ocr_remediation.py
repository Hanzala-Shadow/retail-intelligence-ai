from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pipeline_ocr_remediation_stage as remediation


def test_replacement_character_anywhere_in_page_is_detected() -> None:
    text = ("clean sustainability evidence " * 300) + "refinersï¿½standards"
    assert "replacement_character" in remediation.detect_page_quality(text)


def test_changed_page_regenerates_document_and_page_map_once() -> None:
    pages = {1: "clean opening page", 2: "(cid:12) broken", 3: "clean final page"}
    calls: list[int] = []
    final, outcomes = remediation.remediate_page_texts(
        pages, lambda page: calls.append(page) or ("Verified readable climate progress and emissions evidence." * 3)
    )
    text, page_map = remediation.regenerate_document(final)
    assert calls == [2]
    assert outcomes[0]["action"] == "approved_page_override"
    assert len(page_map) == 3
    assert text[page_map[1]["char_start"] : page_map[1]["char_end"]] == final[2].strip()


def test_failed_ocr_preserves_old_text_and_holds_for_review() -> None:
    pages = {1: "(cid:2) broken source"}
    def fail(_: int) -> str:
        raise RuntimeError("engine unavailable")
    final, outcomes = remediation.remediate_page_texts(pages, fail)
    assert final == pages
    assert outcomes[0]["action"] == "manual_review_hold"
    assert outcomes[0]["verification"] == "ocr_failed"


def test_page_override_never_duplicates_whole_page_across_chunks() -> None:
    pages = {1: "(cid:2) broken", 2: "clean second page"}
    final, _ = remediation.remediate_page_texts(
        pages, lambda _: "Unique verified first-page evidence about Scope 1 emissions." * 3
    )
    text, page_map = remediation.regenerate_document(final)
    first = text[page_map[0]["char_start"] : page_map[0]["char_end"]]
    second = text[page_map[1]["char_start"] : page_map[1]["char_end"]]
    assert first != second
    assert "Unique verified" not in second


def test_failed_retry_log_is_idempotent() -> None:
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as temp:
        path = Path(temp) / "remediation.csv"
        row = {
            "timestamp_utc": "first",
            "logical_source_id": "ls-1",
            "source_version_id": "sv-1",
            "pdf_stem": "report",
            "pages": "2",
            "reason": "cid_artifact",
            "action": "manual_review_hold",
            "before_hash": "before",
            "after_hash": "after",
            "verification_result": "ocr_failed",
            "note": "engine unavailable",
        }
        remediation._append_log(path, [row])
        retry = dict(row, timestamp_utc="second")
        remediation._append_log(path, [retry])
        assert len(remediation._read_csv(path)) == 1
