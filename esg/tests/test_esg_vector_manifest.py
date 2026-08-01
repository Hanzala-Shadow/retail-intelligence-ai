from __future__ import annotations

import build_esg_vector_manifest as manifest
import config


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


def test_noise_tier_chunk_is_excluded_from_manifest_eligibility() -> None:
    # The chunker's navigation detector only inspects sections at or below
    # NAVIGATION_TRACE_MAX_TOKENS, so a large table of contents arrives here
    # unflagged. The enriched tier is what keeps it out of the index.
    decision, reason = manifest.eligibility_for_chunk(
        _chunk(), _policy(), quality_tier="noise"
    )
    assert decision == "excluded"
    assert "chunk_quality_tier=noise" in reason


def test_non_noise_tier_is_not_excluded_by_the_tier_gate() -> None:
    for tier in ("narrative", "layout_sensitive", "", "pending_text"):
        _, reason = manifest.eligibility_for_chunk(
            _chunk(), _policy(), quality_tier=tier
        )
        assert "chunk_quality_tier=noise" not in reason, tier


def test_noise_tier_chunk_reports_excluded_noise_retrieval_state() -> None:
    row = manifest.manifest_row(
        _chunk(),
        {},
        None,
        None,
        {"chunk-1": "noise"},
    )
    assert row["retrieval_state"] == "excluded_noise"


def test_missing_enriched_index_yields_no_tier_signal() -> None:
    # The manifest must still build when enrichment has not run yet, so an
    # absent enriched index degrades to "no tier information", never an error.
    assert manifest.load_quality_tiers(None) == {}
    assert manifest.load_quality_tiers(config.REPO_ROOT / "does_not_exist.csv") == {}


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


def test_missing_page_range_with_parsed_text_is_fatal() -> None:
    # A chunk that carries parsed text but has no page range is a genuine
    # integrity gap and must keep the fail-closed reason.
    chunk = _chunk(page_start="", page_end="")
    status, reason = manifest.layout_policy_for_chunk(chunk, {})
    assert status == "auto_hold"
    assert reason == "layout_missing_chunk_page_range"
    fatal = "layout_missing_chunk_page_range" in reason
    assert fatal


def test_missing_page_range_without_parsed_text_holds_non_fatally() -> None:
    # No parsed text means the chunk was never positioned; hold it without a
    # fatal reason so the manifest still builds (it is excluded anyway).
    chunk = _chunk(page_start="", page_end="", parsed_text_sha256="")
    status, reason = manifest.layout_policy_for_chunk(chunk, {})
    assert status == "auto_hold"
    assert reason == "layout_no_page_range_uncitable"
    fatal_markers = ("layout_audit_missing", "layout_audit_stale", "layout_missing_chunk_page_range")
    assert not any(marker in reason for marker in fatal_markers)
    # validate_fail_closed must not abort on such a held, excluded chunk.
    row = manifest.manifest_row(chunk, {})
    assert row["eligibility_decision"] == "excluded"
    manifest.validate_fail_closed([row])


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
