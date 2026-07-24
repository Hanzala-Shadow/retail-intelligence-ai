from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_esg_vector_manifest as manifest


def _chunk(**changes: str) -> dict:
    row = {
        "chunk_id": "chunk-1",
        "ticker": "AAA",
        "pdf_stem": "report-2024",
        "page_start": "1",
        "page_end": "1",
        "parsed_text_sha256": "p" * 64,
        "source_version_id": "source__" + "s" * 12,
        "lifecycle_state": "active",
        "include_in_esg_index": "true",
        "rag_action": "index_as_esg",
        "citation_ready": "true",
        "citation_validation_status": "validated",
        "quality_flags": "",
        "short_section_action": "",
        "duplicate_of_source_id": "",
    }
    row.update(changes)
    return row


def _policy() -> dict:
    return {"include_in_esg_index": True, "duplicate_of_source_id": ""}


def test_superseded_chunk_is_excluded_from_manifest_eligibility() -> None:
    decision, reason = manifest.eligibility_for_chunk(
        _chunk(lifecycle_state="superseded"), _policy()
    )
    assert decision == "excluded"
    assert "lifecycle_state=superseded" in reason


def test_missing_layout_hashes_fail_closed() -> None:
    chunk = _chunk()
    audit = {
        ("AAA", "report-2024"): {
            1: {
                "audit_version": manifest.LAYOUT_AUDIT_VERSION,
                "source_sha256": "s" * 64,
                "parsed_text_sha256": "",
                "decision": "auto_pass",
            }
        }
    }
    status, reason = manifest.layout_policy_for_chunk(chunk, audit)
    assert status == "auto_hold"
    assert reason == "layout_audit_stale_parse_hash_page=1"


def test_stale_source_hash_fails_closed() -> None:
    chunk = _chunk()
    audit = {
        ("AAA", "report-2024"): {
            1: {
                "audit_version": manifest.LAYOUT_AUDIT_VERSION,
                "source_sha256": "x" * 64,
                "parsed_text_sha256": "p" * 64,
                "decision": "auto_pass",
            }
        }
    }
    status, reason = manifest.layout_policy_for_chunk(chunk, audit)
    assert status == "auto_hold"
    assert reason == "layout_audit_stale_source_version_page=1"
