from __future__ import annotations

import argparse
import csv
import json
import os
import uuid
from collections import Counter
from pathlib import Path

import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)
import config  # noqa: E402
import esg_year  # noqa: E402


MANIFEST_FIELDS = [
    "chunk_id",
    "logical_source_id",
    "source_id",
    "source_version_id",
    "extraction_artifact_id",
    "ticker",
    "canonical_ticker",
    "pdf_stem",
    "inferred_year",
    "report_year_span",
    "section_label",
    "section_instance_id",
    "chunk_index",
    "doc_type",
    "source_type",
    "source_scope",
    "retrieval_tier",
    "include_in_esg_index",
    "doc_quality_status",
    "rag_action",
    "quality_flags",
    "chunk_type",
    "token_count",
    "citation_ready",
    "citation_validation_status",
    "page_start",
    "page_end",
    "chunk_file",
    "layout_qa_status",
    "layout_qa_reason",
    "eligibility_decision",
    "eligibility_reason",
    "retrieval_state",
    "vlm_model",
    "vlm_prompt_version",
    "source_page_sha256",
    "vlm_output_sha256",
    "vlm_verification_state",
]

VERIFIED_CITATION_STATUSES = {"verified_exact", "verified_whitespace_normalized"}
LAYOUT_AUDIT_DEFAULT = str(config.ESG_PAGE_LAYOUT_QA_CSV)
# A navigation page is excluded for a different reason than a held page -- its
# text may be perfectly readable, it simply must not be retrievable. It shares
# the hold path rather than getting its own layout status so that there is a
# single place where a chunk can be kept out of the index; a second status would
# have to be repeated at every downstream gate, and one miss silently indexes
# navigation. The reason string below keeps the two distinguishable.
LAYOUT_NAVIGATION_DECISIONS = {"auto_exclude_navigation"}
LAYOUT_HOLD_DECISIONS = {"auto_hold", "audit_error"} | LAYOUT_NAVIGATION_DECISIONS
# Must equal esg_layout_qa.AUDIT_VERSION. It is duplicated rather than imported
# so this script stays free of the parser's pdfplumber/pypdfium dependencies;
# tests/test_esg_vector_manifest.py fails if the two ever drift. Any audit row
# carrying a different version is treated as stale and holds its chunk, so a
# mismatch here silently quarantines the entire corpus.
LAYOUT_AUDIT_VERSION = "layout_v8"


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def parse_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return (value or "").strip().lower() in {"true", "1", "yes", "y"}


def infer_year(pdf_stem: str) -> str:
    """Canonical report year as a string; "" when the stem carries no year.

    Delegates to src/esg_year.py. This function used to hold its own regex that
    took the *first* year token, which disagreed with the scoping driver
    (scripts/run_pdf_parser_by_year.py) and the enrichment stage on every
    multi-year stem -- VFC-VF CORP-2023-2024 was labelled 2023 here while both
    other stages called it 2024. One rule, one place.
    """
    year = esg_year.report_year(pdf_stem)
    return "" if year is None else str(year)


def infer_year_span(pdf_stem: str) -> str:
    """Full covered span for a stem, e.g. "2021-2022"; "" when unresolved.

    Carried alongside the scalar so a question about the earlier year of a
    multi-year report can still match: the scalar routes, the span recalls.
    """
    return esg_year.extract_report_year(pdf_stem)[2]


def load_source_registry(path: Path) -> dict[tuple[str, str], dict]:
    registry: dict[tuple[str, str], dict] = {}
    for row in read_csv(path):
        ticker = (row.get("observed_ticker") or row.get("ticker") or "").strip().upper()
        pdf_stem = (row.get("pdf_stem") or "").strip()
        if ticker and pdf_stem:
            registry[(ticker, pdf_stem)] = row
    return registry


def parse_int(value: str | int | None) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def load_layout_audit(
    path: str | Path | None,
    *,
    require: bool,
) -> dict[tuple[str, str], dict[int, dict]] | None:
    if path is None:
        if require:
            raise ValueError("layout audit is required but no path was supplied")
        return None

    audit_path = Path(path)
    if not audit_path.exists():
        if require:
            raise ValueError(f"required layout audit is missing: {audit_path}")
        return None

    audit: dict[tuple[str, str], dict[int, dict]] = {}
    for row in read_csv(audit_path):
        ticker = (row.get("ticker") or "").strip().upper()
        pdf_stem = (row.get("pdf_stem") or "").strip()
        page = parse_int(row.get("page"))
        if not ticker or not pdf_stem or page is None or page < 1:
            continue
        audit.setdefault((ticker, pdf_stem), {})[page] = row
    return audit


def layout_policy_for_chunk(
    chunk: dict,
    layout_audit: dict[tuple[str, str], dict[int, dict]] | None,
) -> tuple[str, str]:
    """Return the automatic layout gate for a citation-range chunk.

    The vector manifest owns retrieval eligibility. A missing, stale, or held
    page is treated as a fail-closed exclusion while the cited chunk itself
    remains stored in the corpus.
    """

    if layout_audit is None:
        return "not_run", "layout_audit_not_run"

    ticker = (chunk.get("ticker") or "").strip().upper()
    pdf_stem = (chunk.get("pdf_stem") or "").strip()
    page_start = parse_int(chunk.get("page_start"))
    page_end = parse_int(chunk.get("page_end"))
    if page_start is None or page_end is None or page_start < 1 or page_end < page_start:
        # A missing page range is only an integrity failure when the chunk
        # actually carries parsed text that should have been located onto pages.
        # Chunks with no parsed text (missing_parsed_text / needs_review) were
        # never positioned, are already excluded as non-citation-ready, and must
        # hold without a fatal reason so the manifest can still be built.
        if not (chunk.get("parsed_text_sha256") or "").strip():
            return "auto_hold", "layout_no_page_range_uncitable"
        return "auto_hold", "layout_missing_chunk_page_range"

    document_audit = layout_audit.get((ticker, pdf_stem))
    if not document_audit:
        return "auto_hold", "layout_audit_missing_document"

    page_rows = []
    for page in range(page_start, page_end + 1):
        row = document_audit.get(page)
        if row is None:
            return "auto_hold", f"layout_audit_missing_page={page}"
        if (row.get("audit_version") or "").strip() != LAYOUT_AUDIT_VERSION:
            return "auto_hold", f"layout_audit_stale_version_page={page}"
        audit_source_hash = (row.get("source_sha256") or "").strip().lower()
        parsed_hash = (chunk.get("parsed_text_sha256") or "").strip()
        audit_parsed_hash = (row.get("parsed_text_sha256") or "").strip()
        if not parsed_hash or not audit_parsed_hash or parsed_hash != audit_parsed_hash:
            return "auto_hold", f"layout_audit_stale_parse_hash_page={page}"
        # Chunk ``source_sha256`` is the section-source fingerprint in the
        # current chunk contract, not the raw-PDF hash. The canonical raw hash
        # is carried by the source-version identifier instead.
        source_version_id = (chunk.get("source_version_id") or "").strip().lower()
        source_hash_bound = (
            source_version_id.endswith("__" + audit_source_hash[:12])
            or source_version_id == "sv_" + audit_source_hash[:24]
        ) if len(audit_source_hash) == 64 and source_version_id else False
        if not source_hash_bound:
            return "auto_hold", f"layout_audit_stale_source_version_page={page}"
        page_rows.append((page, row))

    navigation_pages = [
        page
        for page, row in page_rows
        if (row.get("decision") or "").strip() in LAYOUT_NAVIGATION_DECISIONS
    ]
    if navigation_pages:
        return "auto_hold", "layout_navigation_page=" + ",".join(map(str, navigation_pages))

    held_pages = [
        page
        for page, row in page_rows
        if (row.get("decision") or "").strip() in LAYOUT_HOLD_DECISIONS
    ]
    if held_pages:
        return "auto_hold", "layout_auto_hold_page=" + ",".join(map(str, held_pages))

    decisions = {(row.get("decision") or "").strip() for _, row in page_rows}
    if decisions == {"auto_pass_pdfium_coverage"}:
        return "auto_pass_pdfium_coverage", "layout_audit_pass_pdfium_coverage"
    return "auto_pass", "layout_audit_pass"


def validate_chunk_ids(chunks: list[dict]) -> None:
    missing = [index + 1 for index, row in enumerate(chunks) if not (row.get("chunk_id") or "").strip()]
    if missing:
        preview = ", ".join(str(index) for index in missing[:10])
        raise ValueError(f"missing chunk_id in {len(missing)} row(s); first rows: {preview}")

    counts = Counter((row.get("chunk_id") or "").strip() for row in chunks)
    duplicates = sorted(chunk_id for chunk_id, count in counts.items() if count > 1)
    if duplicates:
        preview = ", ".join(duplicates[:10])
        raise ValueError(f"duplicate chunk_id in chunks index: {preview}")


def registry_overlay(chunk: dict, registry_row: dict | None) -> dict:
    if registry_row is None:
        return {
            "source_id": chunk.get("source_id", ""),
            "canonical_ticker": chunk.get("canonical_ticker") or chunk.get("ticker", ""),
            "doc_type": chunk.get("doc_type", ""),
            "source_type": chunk.get("source_type") or chunk.get("doc_type", ""),
            "source_scope": chunk.get("source_scope", ""),
            "retrieval_tier": chunk.get("retrieval_tier", ""),
            "include_in_esg_index": chunk.get("include_in_esg_index", ""),
            "duplicate_of_source_id": chunk.get("duplicate_of_source_id", ""),
        }

    return {
        "source_id": (registry_row.get("source_id") or chunk.get("source_id") or "").strip(),
        "canonical_ticker": (
            registry_row.get("canonical_ticker") or chunk.get("canonical_ticker") or chunk.get("ticker") or ""
        ).strip().upper(),
        "doc_type": (registry_row.get("source_type") or chunk.get("doc_type") or "").strip(),
        "source_type": (registry_row.get("source_type") or chunk.get("source_type") or "").strip(),
        "source_scope": (registry_row.get("source_scope") or chunk.get("source_scope") or "").strip(),
        "retrieval_tier": (registry_row.get("retrieval_tier") or chunk.get("retrieval_tier") or "").strip(),
        "include_in_esg_index": (registry_row.get("include_in_esg_index") or chunk.get("include_in_esg_index") or "").strip(),
        "duplicate_of_source_id": (registry_row.get("duplicate_of_source_id") or chunk.get("duplicate_of_source_id") or "").strip(),
    }


def load_vlm_dir(vlm_dir: str | Path | None) -> tuple[dict, list[dict]]:
    """VLM activation inputs (owner-approved integration, default OFF).

    Returns (flagged, vlm_rows): pages the pinned VLM classifier judged
    table_dominant (their parser chunks are scrambled by column-major reading and
    get excluded), and the verified VLM extraction chunk rows that replace them.
    """
    if vlm_dir is None:
        return {}, []
    vlm_dir = Path(vlm_dir)
    flagged: dict[tuple[str, str], set[int]] = {}
    for f in (vlm_dir / "classifier").glob("*.json"):
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if meta.get("verdict", {}).get("decision_class") == "table_dominant":
            key = ((meta.get("ticker") or "").strip().upper(),
                   (meta.get("pdf_stem") or "").strip())
            flagged.setdefault(key, set()).add(int(meta["page"]))
    vlm_rows = []
    for row in read_csv(vlm_dir / "vlm_chunks_index.csv"):
        verified = (row.get("verification_state") or row.get("screen_status") or "").strip().lower()
        required = [row.get("model"), row.get("prompt_version") or row.get("prompt_hash"), row.get("source_page_sha256") or row.get("source_sha256"), row.get("output_sha256")]
        if verified in {"verified", "passed", "screen_pass"} and all(str(value or "").strip() for value in required):
            vlm_rows.append(row)
    return flagged, vlm_rows


def vlm_hold_reason_for_chunk(
    chunk: dict, flagged: dict[tuple[str, str], set[int]]
) -> str | None:
    if not flagged:
        return None
    key = ((chunk.get("ticker") or "").strip().upper(),
           (chunk.get("pdf_stem") or "").strip())
    pages = flagged.get(key)
    if not pages:
        return None
    page_start = parse_int(chunk.get("page_start"))
    page_end = parse_int(chunk.get("page_end"))
    if page_start is None or page_end is None:
        return None
    hit = sorted(set(range(page_start, page_end + 1)) & pages)
    if not hit:
        return None
    return "vlm_classifier_table_dominant_page=" + ",".join(map(str, hit))


def vlm_manifest_row(v: dict, registry: dict[tuple[str, str], dict]) -> dict:
    ticker = (v.get("ticker") or "").strip().upper()
    pdf_stem = (v.get("pdf_stem") or "").strip()
    policy = registry_overlay({"ticker": ticker, "pdf_stem": pdf_stem}, registry.get((ticker, pdf_stem)))
    screen_note = (f"body_uncorroborated={v.get('body_uncorroborated_count', '')};"
                   f"graphic_only={v.get('graphic_only_count', '')}")
    # VLM rows inherit registry governance: duplicates and excluded docs stay out.
    reasons: list[str] = []
    if (policy.get("duplicate_of_source_id") or "").strip():
        reasons.append(f"duplicate_of={policy['duplicate_of_source_id']}")
    if policy.get("include_in_esg_index") and not parse_bool(policy["include_in_esg_index"]):
        reasons.append("include_in_esg_index_false")
    decision = "excluded" if reasons else "eligible"
    reason = ";".join(reasons) if reasons else f"vlm_extraction_v1;{screen_note}"
    return {
        "chunk_id": (v.get("chunk_id") or "").strip(),
        "logical_source_id": v.get("logical_source_id", ""),
        "source_id": policy["source_id"],
        "source_version_id": v.get("source_version_id", ""),
        "extraction_artifact_id": v.get("extraction_artifact_id", ""),
        "ticker": ticker,
        "canonical_ticker": policy["canonical_ticker"] or ticker,
        "pdf_stem": pdf_stem,
        "inferred_year": infer_year(pdf_stem),
        "report_year_span": infer_year_span(pdf_stem),
        "section_label": v.get("section_label", ""),
        "section_instance_id": v.get("section_instance_id", ""),
        "chunk_index": "",
        "doc_type": policy["doc_type"],
        "source_type": policy["source_type"],
        "source_scope": policy["source_scope"],
        "retrieval_tier": policy["retrieval_tier"],
        "include_in_esg_index": str(parse_bool(policy["include_in_esg_index"])).lower(),
        "doc_quality_status": "",
        "rag_action": "index_as_esg",
        "quality_flags": "",
        "chunk_type": "vlm_page_markdown",
        "token_count": "",
        "citation_ready": "true",
        "citation_validation_status": "vlm_page_provenance",
        "page_start": v.get("page", ""),
        "page_end": v.get("page", ""),
        "chunk_file": v.get("chunk_file", ""),
        "layout_qa_status": "vlm_extraction",
        "layout_qa_reason": f"{v.get('lineage', 'vlm_extraction_v1')};model={v.get('model', '')}",
        "eligibility_decision": decision,
        "eligibility_reason": reason,
        "retrieval_state": "eligible" if decision == "eligible" else "held_for_document_review",
        "vlm_model": v.get("model", ""),
        "vlm_prompt_version": v.get("prompt_version") or v.get("prompt_hash", ""),
        "source_page_sha256": v.get("source_page_sha256") or v.get("source_sha256", ""),
        "vlm_output_sha256": v.get("output_sha256", ""),
        "vlm_verification_state": v.get("verification_state") or v.get("screen_status", ""),
    }


def load_quality_tiers(path: str | Path | None) -> dict[str, str]:
    """Map chunk_id -> chunk_quality_tier from the P1 enriched index.

    Optional by design. The enriched index is produced by a later pipeline
    stage than the manifest, so a run that has not enriched yet must still
    build a manifest -- it simply gets no tier signal. Returns {} when the
    file is absent.
    """
    if not path:
        return {}
    enriched = Path(path)
    if not enriched.exists():
        return {}
    tiers: dict[str, str] = {}
    for row in read_csv(enriched):
        chunk_id = (row.get("chunk_id") or "").strip()
        if chunk_id:
            tiers[chunk_id] = (row.get("chunk_quality_tier") or "").strip()
    return tiers


def eligibility_for_chunk(
    chunk: dict,
    policy: dict,
    layout_status: str = "not_run",
    layout_reason: str = "layout_audit_not_run",
    vlm_hold_reason: str | None = None,
    quality_tier: str = "",
) -> tuple[str, str]:
    reasons: list[str] = []
    include = parse_bool(policy.get("include_in_esg_index"))
    rag_action = (chunk.get("rag_action") or "").strip()
    chunk_type = (chunk.get("chunk_type") or "").strip()
    short_action = (chunk.get("short_section_action") or "").strip()
    quality_flags = {
        flag.strip()
        for flag in (chunk.get("quality_flags") or "").split("|")
        if flag.strip()
    }
    citation_ready = parse_bool(chunk.get("citation_ready"))
    citation_status = (chunk.get("citation_validation_status") or "").strip()
    if (chunk.get("lifecycle_state") or "").strip().lower() == "superseded":
        reasons.append("lifecycle_state=superseded")

    if (
        short_action == "excluded"
        or "short_section_excluded_from_retrieval" in quality_flags
    ):
        reasons.append("navigation_trace_chunk")
    # The chunker's own navigation detector only inspects sections at or below
    # NAVIGATION_TRACE_MAX_TOKENS (150), so large tables of contents reach the
    # index unflagged. P1 enrichment already tiers those as noise; honour that
    # here rather than re-implementing the classifier a second time.
    if (quality_tier or "").strip() == "noise":
        reasons.append("chunk_quality_tier=noise")
    if not include:
        reasons.append("include_in_esg_index_false")
    if rag_action != "index_as_esg":
        reasons.append(f"rag_action={rag_action or 'blank'}")
    if not citation_ready or citation_status not in VERIFIED_CITATION_STATUSES:
        reasons.append("citation_not_ready")
    if (policy.get("duplicate_of_source_id") or "").strip():
        reasons.append(f"duplicate_of={policy['duplicate_of_source_id']}")
    if layout_status == "auto_hold":
        reasons.append(layout_reason)
    if vlm_hold_reason:
        reasons.append(vlm_hold_reason)

    if reasons:
        return "excluded", ";".join(dict.fromkeys(reasons))
    return "eligible", "eligible"


def manifest_row(
    chunk: dict,
    registry: dict[tuple[str, str], dict],
    layout_audit: dict[tuple[str, str], dict[int, dict]] | None = None,
    vlm_flagged: dict[tuple[str, str], set[int]] | None = None,
    quality_tiers: dict[str, str] | None = None,
) -> dict:
    ticker = (chunk.get("ticker") or "").strip().upper()
    pdf_stem = (chunk.get("pdf_stem") or "").strip()
    policy = registry_overlay(chunk, registry.get((ticker, pdf_stem)))
    layout_status, layout_reason = layout_policy_for_chunk(chunk, layout_audit)
    quality_tier = (quality_tiers or {}).get((chunk.get("chunk_id") or "").strip(), "")
    decision, reason = eligibility_for_chunk(
        chunk,
        policy,
        layout_status=layout_status,
        layout_reason=layout_reason,
        vlm_hold_reason=vlm_hold_reason_for_chunk(chunk, vlm_flagged or {}),
        quality_tier=quality_tier,
    )

    if decision == "eligible":
        retrieval_state = "eligible"
    elif (chunk.get("lifecycle_state") or "").strip() == "superseded":
        retrieval_state = "superseded"
    elif "duplicate_of=" in reason:
        retrieval_state = "excluded_duplicate"
    # Noise outranks the VLM hold: there is no point queueing navigation and
    # table-of-contents text for expensive page verification.
    elif "chunk_quality_tier=noise" in reason:
        retrieval_state = "excluded_noise"
    elif "vlm_" in reason:
        retrieval_state = "held_for_vlm"
    elif "held_for_ocr" in (chunk.get("quality_flags") or ""):
        retrieval_state = "held_for_ocr"
    else:
        retrieval_state = "held_for_document_review" if "rag_action=" in reason else "held_for_vlm"
    return {
        "chunk_id": (chunk.get("chunk_id") or "").strip(),
        "logical_source_id": chunk.get("logical_source_id", ""),
        "source_id": policy["source_id"],
        "source_version_id": chunk.get("source_version_id", ""),
        "extraction_artifact_id": chunk.get("extraction_artifact_id", ""),
        "ticker": ticker,
        "canonical_ticker": policy["canonical_ticker"],
        "pdf_stem": pdf_stem,
        "inferred_year": infer_year(pdf_stem),
        "report_year_span": infer_year_span(pdf_stem),
        "section_label": chunk.get("section_code", ""),
        "section_instance_id": chunk.get("section_instance_id", ""),
        "chunk_index": chunk.get("chunk_index", ""),
        "doc_type": policy["doc_type"],
        "source_type": policy["source_type"],
        "source_scope": policy["source_scope"],
        "retrieval_tier": policy["retrieval_tier"],
        "include_in_esg_index": str(parse_bool(policy["include_in_esg_index"])).lower(),
        "doc_quality_status": chunk.get("doc_quality_status", ""),
        "rag_action": chunk.get("rag_action", ""),
        "quality_flags": chunk.get("quality_flags", ""),
        "chunk_type": chunk.get("chunk_type", ""),
        "token_count": chunk.get("token_count", ""),
        "citation_ready": str(parse_bool(chunk.get("citation_ready"))).lower(),
        "citation_validation_status": chunk.get("citation_validation_status", ""),
        "page_start": chunk.get("page_start", ""),
        "page_end": chunk.get("page_end", ""),
        "chunk_file": chunk.get("chunk_file", ""),
        "layout_qa_status": layout_status,
        "layout_qa_reason": layout_reason,
        "eligibility_decision": decision,
        "eligibility_reason": reason,
        "retrieval_state": retrieval_state,
        "vlm_model": "", "vlm_prompt_version": "", "source_page_sha256": "",
        "vlm_output_sha256": "", "vlm_verification_state": "",
    }


def validate_fail_closed(rows: list[dict]) -> None:
    fatal_layout = [row for row in rows if any(marker in (row.get("layout_qa_reason") or "") for marker in ("layout_audit_missing", "layout_audit_stale", "layout_missing_chunk_page_range"))]
    if fatal_layout:
        raise ValueError(f"missing or stale layout QA for {len(fatal_layout)} chunk(s)")
    bad_superseded = [row for row in rows if row.get("retrieval_state") == "superseded" and row.get("eligibility_decision") == "eligible"]
    if bad_superseded:
        raise ValueError("a superseded chunk is eligible")
    eligible_by_page: dict[tuple[str, str, str], set[str]] = {}
    for row in rows:
        if row.get("eligibility_decision") != "eligible":
            continue
        for page in range(int(row.get("page_start") or 0), int(row.get("page_end") or 0) + 1):
            eligible_by_page.setdefault((row.get("ticker", ""), row.get("pdf_stem", ""), str(page)), set()).add("vlm" if row.get("chunk_type") == "vlm_page_markdown" else "parser")
    if any(kinds == {"parser", "vlm"} for kinds in eligible_by_page.values()):
        raise ValueError("an approved VLM replacement and parser chunk are both eligible")


def atomic_write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def verify_written_manifest(
    path: Path, chunks: list[dict], extra_expected_ids: set[str] | None = None
) -> dict[str, int]:
    chunk_ids = {(row.get("chunk_id") or "").strip() for row in chunks}
    if extra_expected_ids:
        chunk_ids |= extra_expected_ids
    manifest_rows = read_csv(path)
    manifest_ids = [(row.get("chunk_id") or "").strip() for row in manifest_rows]
    manifest_counts = Counter(manifest_ids)
    duplicate_manifest_ids = [chunk_id for chunk_id, count in manifest_counts.items() if count > 1]
    if duplicate_manifest_ids:
        raise ValueError(f"duplicate chunk_id in manifest: {', '.join(duplicate_manifest_ids[:10])}")

    missing = sorted(chunk_ids - set(manifest_ids))
    obsolete = sorted(set(manifest_ids) - chunk_ids)
    if missing or obsolete:
        raise ValueError(
            f"manifest/chunk ID mismatch: missing={len(missing)} obsolete={len(obsolete)}"
        )
    return {
        "chunks": len(chunks),
        "manifest_rows": len(manifest_rows),
        "missing_ids": 0,
        "obsolete_ids": 0,
    }


def build_manifest(
    chunks_index: str | Path,
    source_registry: str | Path,
    out: str | Path,
    layout_audit_path: str | Path | None = None,
    require_layout_audit: bool = True,
    vlm_dir: str | Path | None = None,
    ticker: str | None = None,
    pdf_stem: str | None = None,
    enriched_index: str | Path | None = None,
) -> list[dict]:
    chunks = read_csv(Path(chunks_index))
    validate_chunk_ids(chunks)
    selected_ticker = ticker.strip().upper() if ticker else None
    selected_pdf_stem = Path(pdf_stem).stem if pdf_stem else None

    def selected(row: dict) -> bool:
        return (
            (not selected_ticker or (row.get("ticker") or "").strip().upper() == selected_ticker)
            and (not selected_pdf_stem or (row.get("pdf_stem") or "").strip() == selected_pdf_stem)
        )

    target_chunks = [row for row in chunks if selected(row)]
    registry = load_source_registry(Path(source_registry))
    layout_audit = load_layout_audit(
        layout_audit_path,
        require=require_layout_audit,
    )
    vlm_flagged, vlm_rows = load_vlm_dir(vlm_dir)
    target_vlm_rows = [row for row in vlm_rows if selected(row)]
    quality_tiers = load_quality_tiers(enriched_index)
    rows = [
        manifest_row(chunk, registry, layout_audit, vlm_flagged, quality_tiers)
        for chunk in target_chunks
    ]
    rows.extend(vlm_manifest_row(v, registry) for v in target_vlm_rows)
    if selected_ticker or selected_pdf_stem:
        rows.extend(row for row in read_csv(Path(out)) if not selected(row))
    rows.sort(key=lambda row: row["chunk_id"])
    validate_chunk_ids(rows)
    validate_fail_closed(rows)
    atomic_write_csv(Path(out), rows)
    verify_written_manifest(
        Path(out), chunks,
        extra_expected_ids={(v.get("chunk_id") or "").strip() for v in vlm_rows},
    )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Build deterministic ESG vector manifest from chunk metadata.")
    parser.add_argument("--chunks-index", default=str(config.ESG_CHUNKS_INDEX_CSV))
    parser.add_argument("--source-registry", default=str(config.ESG_SOURCE_REGISTRY_CSV))
    parser.add_argument("--out", default=str(config.VECTOR_INDEX_MANIFEST_CSV))
    parser.add_argument("--layout-audit", default=LAYOUT_AUDIT_DEFAULT)
    parser.add_argument(
        "--allow-missing-layout-audit",
        action="store_true",
        help="Bypass the automatic page-layout gate only for isolated legacy diagnostics.",
    )
    parser.add_argument(
        "--vlm-dir",
        default=None,
        help="VLM activation (default OFF): exclude parser chunks of classifier-flagged "
             "pages and append verified VLM extraction chunks (e.g. data/04_vlm).",
    )
    parser.add_argument(
        "--enriched-index",
        default=str(config.ESG_CHUNKS_INDEX_ENRICHED_CSV),
        help="P1 enriched chunk index, read only for chunk_quality_tier. Chunks "
             "tiered 'noise' are excluded from the index. Optional: if the file "
             "is absent the manifest is built without the tier signal.",
    )
    parser.add_argument("--ticker", default=None)
    parser.add_argument("--pdf-file", default=None)
    parser.add_argument("--pdf-stem", default=None)
    args = parser.parse_args()
    if args.pdf_file and args.pdf_stem:
        parser.error("use only one of --pdf-file and --pdf-stem")

    rows = build_manifest(
        chunks_index=args.chunks_index,
        source_registry=args.source_registry,
        out=args.out,
        layout_audit_path=args.layout_audit,
        require_layout_audit=not args.allow_missing_layout_audit,
        vlm_dir=args.vlm_dir,
        ticker=args.ticker,
        pdf_stem=args.pdf_stem or (Path(args.pdf_file).stem if args.pdf_file else None),
        enriched_index=args.enriched_index,
    )
    counts = Counter(row["eligibility_decision"] for row in rows)
    _, vlm_rows = load_vlm_dir(args.vlm_dir)
    verification = verify_written_manifest(
        Path(args.out), read_csv(Path(args.chunks_index)),
        extra_expected_ids={(v.get("chunk_id") or "").strip() for v in vlm_rows},
    )
    print(f"Vector manifest written: {args.out}")
    print(f"rows: {verification['manifest_rows']}")
    print(f"missing_ids: {verification['missing_ids']}")
    print(f"obsolete_ids: {verification['obsolete_ids']}")
    for decision, count in sorted(counts.items()):
        print(f"{decision}: {count}")


if __name__ == "__main__":
    main()
