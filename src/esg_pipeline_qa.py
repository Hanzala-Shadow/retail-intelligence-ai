from __future__ import annotations

import argparse
import csv
import os
import re
from collections import defaultdict
from pathlib import Path


QA_FIELDS = [
    "company_id",
    "ticker",
    "company_name",
    "tracker_status",
    "report_year",
    "format",
    "drive_file_link",
    "pdf_file",
    "pdf_count",
    "parsed_count",
    "ocr_required_count",
    "failed_parse_count",
    "doc_quality_status",
    "rag_action",
    "quality_flag_count",
    "wrong_doc_type_count",
    "garbled_text_count",
    "low_text_quality_count",
    "layout_audit_status",
    "layout_checked_page_count",
    "layout_auto_hold_page_count",
    "layout_held_chunk_count",
    "section_count",
    "chunk_count",
    "short_evidence_chunk_count",
    "citation_ready_chunk_count",
    "missing_citation_metadata_count",
    "min_chunk_tokens",
    "max_chunk_tokens",
    "status",
    "notes",
]

VALID_PARSE_STATUSES = {"parsed", "ocr_required", "failed"}
CITATION_VALIDATION_VERSION = "semantic_v1"
MIN_CHUNK_TOKENS = 100
MAX_CHUNK_TOKENS = 600
SHORT_EVIDENCE_MIN_TOKENS = 25
CHUNK_TYPE_NORMAL = "normal"
CHUNK_TYPE_SHORT_EVIDENCE = "short_evidence"
VERIFIED_CITATION_STATUSES = {
    "verified_exact",
    "verified_whitespace_normalized",
}
LAYOUT_AUDIT_VERSION = "layout_v7"
LAYOUT_HOLD_DECISIONS = {"auto_hold", "audit_error"}


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=QA_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp_path, path)


def resolve_path(raw_path: str | None) -> Path | None:
    if not raw_path:
        return None
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return Path.cwd() / path


def normalize_status(status: str | None) -> str:
    raw = (status or "").strip().lower().replace("-", "_").replace(" ", "_")
    if raw == "downloaded":
        return "downloaded"
    if raw in {"not_found", "notfound"}:
        return "not_found"
    return raw


def is_bad_drive_link(value: str | None, ticker: str) -> bool:
    raw = (value or "").strip()
    if not raw:
        return True
    if raw.upper() == ticker.upper():
        return True
    if raw.startswith(("http://", "https://")):
        return False
    # Accept plausible Drive IDs, but flag short ticker-like placeholders.
    return not bool(re.fullmatch(r"[A-Za-z0-9_-]{20,}", raw))


def count_local_pdfs(raw_root: Path) -> dict[str, list[Path]]:
    pdfs: dict[str, list[Path]] = defaultdict(list)
    if not raw_root.exists():
        return pdfs
    for pdf_file in raw_root.glob("*/*.pdf"):
        pdfs[pdf_file.parent.name.upper()].append(pdf_file)
    return pdfs


def group_by(rows: list[dict], key: str) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[(row.get(key) or "").strip().upper()].append(row)
    return grouped


def parse_int(value: str | int | None) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def chunk_token_counts(rows: list[dict]) -> list[int]:
    counts: list[int] = []
    for row in rows:
        value = parse_int(row.get("token_count"))
        if value is not None:
            counts.append(value)
    return counts


def valid_chunk_token_count(row: dict) -> bool:
    token_count = parse_int(row.get("token_count"))
    chunk_type = (row.get("chunk_type") or CHUNK_TYPE_NORMAL).strip()
    if token_count is None:
        return False
    if chunk_type == CHUNK_TYPE_SHORT_EVIDENCE:
        return SHORT_EVIDENCE_MIN_TOKENS <= token_count < MIN_CHUNK_TOKENS
    return MIN_CHUNK_TOKENS <= token_count <= MAX_CHUNK_TOKENS


def parse_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return (value or "").strip().lower() in {"true", "1", "yes", "y"}


def is_semantically_citation_ready(row: dict) -> bool:
    """Require both the ready flag and evidence from the semantic validator."""
    return (
        parse_bool(row.get("citation_ready"))
        and str(row.get("citation_validation_version") or "").strip()
        == CITATION_VALIDATION_VERSION
        and str(row.get("citation_validation_status") or "").strip()
        in VERIFIED_CITATION_STATUSES
    )


def row_quality_flags(row: dict) -> set[str]:
    raw_flags = (row.get("quality_flags") or "").strip()
    return {flag for flag in raw_flags.split("|") if flag}


def extract_years(raw_year: str | None) -> list[int]:
    return sorted({int(y) for y in re.findall(r"\b(?:20|19)\d{2}\b", raw_year or "")})


def row_years(row: dict, fields: tuple[str, ...]) -> set[int]:
    text = " ".join(row.get(field, "") or "" for field in fields)
    return set(extract_years(text))


def pdf_stem_from_parse_row(row: dict) -> str:
    source_pdf = row.get("source_pdf") or row.get("pdf_file") or ""
    return Path(source_pdf).stem if source_pdf else Path(row.get("pdf_file") or "").stem


def filter_parse_rows_for_tracker(tracker: dict, parse_rows: list[dict]) -> list[dict]:
    years = set(extract_years(tracker.get("report_year")))
    if not years:
        return parse_rows
    return [
        row
        for row in parse_rows
        if row_years(row, ("pdf_file", "source_pdf", "parsed_text_file")) & years
    ]


def filter_paths_for_tracker(tracker: dict, paths: list[Path]) -> list[Path]:
    years = set(extract_years(tracker.get("report_year")))
    if not years:
        return paths
    return [path for path in paths if set(extract_years(path.name)) & years]


def group_by_doc(rows: list[dict]) -> dict[tuple[str, str], list[dict]]:
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        ticker = (row.get("ticker") or "").strip().upper()
        pdf_stem = (row.get("pdf_stem") or "").strip()
        if ticker and pdf_stem:
            grouped[(ticker, pdf_stem)].append(row)
    return grouped


def quality_status_for_parse_rows(parse_rows: list[dict]) -> str:
    if not parse_rows:
        return ""
    if any(parse_bool(row.get("possible_wrong_doc_type")) for row in parse_rows):
        return "needs_review"
    for row in parse_rows:
        flags = row_quality_flags(row)
        if row.get("status") != "parsed" or flags & {
            "garbled_text",
            "low_readable_word_ratio",
            "low_text_per_page",
        }:
            return "needs_review"
    return "ok"


def rag_action_for_quality_status(quality_status: str, tracker_status: str) -> str:
    if tracker_status == "not_found":
        return "not_applicable"
    if quality_status == "exclude_from_esg_rag":
        return "exclude_from_esg_index"
    if quality_status == "needs_review":
        return "manual_review_before_indexing"
    if quality_status == "ok":
        return "index_as_esg"
    return "manual_review_before_indexing"


def missing_index_files(rows: list[dict], path_field: str) -> int:
    missing = 0
    for row in rows:
        path = resolve_path(row.get(path_field))
        if path is None or not path.exists():
            missing += 1
    return missing


def has_multi_year(raw_year: str | None) -> bool:
    years = re.findall(r"\b(?:20|19)\d{2}\b", raw_year or "")
    return len(set(years)) > 1


def load_company_map(companies_path: Path) -> dict[str, dict]:
    companies = {}
    for row in read_csv(companies_path):
        ticker = (row.get("ticker") or "").strip().upper()
        if ticker:
            companies[ticker] = row
    return companies


def load_source_registry(path: Path) -> dict[tuple[str, str], dict]:
    registry: dict[tuple[str, str], dict] = {}
    for row in read_csv(path):
        ticker = (row.get("observed_ticker") or row.get("ticker") or "").strip().upper()
        pdf_stem = (row.get("pdf_stem") or "").strip()
        if ticker and pdf_stem:
            registry[(ticker, pdf_stem)] = row
    return registry


def group_parse_by_doc(rows: list[dict]) -> dict[tuple[str, str], list[dict]]:
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        ticker = (row.get("ticker") or "").strip().upper()
        pdf_stem = pdf_stem_from_parse_row(row)
        if ticker and pdf_stem:
            grouped[(ticker, pdf_stem)].append(row)
    return grouped


def load_layout_audit(path: Path) -> dict[tuple[str, str], list[dict]]:
    rows_by_doc: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in read_csv(path):
        ticker = (row.get("ticker") or "").strip().upper()
        pdf_stem = (row.get("pdf_stem") or "").strip()
        if ticker and pdf_stem:
            rows_by_doc[(ticker, pdf_stem)].append(row)
    return rows_by_doc


def _chunk_overlaps_pages(row: dict, held_pages: set[int]) -> bool:
    start = parse_int(row.get("page_start"))
    end = parse_int(row.get("page_end"))
    if start is None or end is None or start < 1 or end < start:
        return bool(held_pages)
    return any(page in held_pages for page in range(start, end + 1))


def layout_summary_for_doc(
    layout_rows: list[dict],
    parse_rows: list[dict],
    chunk_rows: list[dict],
) -> dict[str, int | str]:
    if not parse_rows:
        return {
            "status": "not_applicable",
            "checked_page_count": 0,
            "auto_hold_page_count": 0,
            "held_chunk_count": 0,
        }

    expected_page_count = max((parse_int(row.get("page_count")) or 0) for row in parse_rows)
    source_hashes = {(row.get("source_sha256") or "").strip() for row in parse_rows}
    parsed_hashes = {(row.get("content_hash") or "").strip() for row in parse_rows}
    current_rows = [
        row
        for row in layout_rows
        if row.get("audit_version") == LAYOUT_AUDIT_VERSION
        and (row.get("source_sha256") or "").strip() in source_hashes
        and (row.get("parsed_text_sha256") or "").strip() in parsed_hashes
        and parse_int(row.get("page")) is not None
    ]
    checked_pages = {parse_int(row.get("page")) for row in current_rows}
    held_pages = {
        parse_int(row.get("page"))
        for row in current_rows
        if (row.get("decision") or "").strip() in LAYOUT_HOLD_DECISIONS
        and parse_int(row.get("page")) is not None
    }

    if expected_page_count and len(checked_pages) != expected_page_count:
        status = "missing_or_stale"
    elif held_pages:
        status = "auto_hold"
    else:
        status = "auto_pass"

    return {
        "status": status,
        "checked_page_count": len(checked_pages),
        "auto_hold_page_count": len(held_pages),
        "held_chunk_count": sum(
            1 for row in chunk_rows if _chunk_overlaps_pages(row, held_pages)
        ),
    }


def pdf_file_for_doc(pdf_stem: str, parse_rows: list[dict]) -> str:
    for row in parse_rows:
        pdf_file = (row.get("pdf_file") or "").strip()
        if pdf_file:
            return pdf_file
    return f"{pdf_stem}.pdf" if pdf_stem else ""


def source_registry_exclusion_reason(row: dict) -> str:
    duplicate_of = (row.get("duplicate_of_source_id") or "").strip()
    retrieval_tier = (row.get("retrieval_tier") or "").strip()
    source_scope = (row.get("source_scope") or "").strip()
    source_type = (row.get("source_type") or "").strip()
    notes = (row.get("notes") or "").strip()

    if duplicate_of:
        return f"source registry excludes duplicate of {duplicate_of}"
    if retrieval_tier == "specialized" or source_scope == "topic_specific":
        return "source registry excludes specialized/topic-specific source from company-wide ESG"
    if retrieval_tier in {"supplementary", "supplemental"} or source_scope == "supplemental":
        return "source registry excludes supplemental source from primary ESG index"
    if retrieval_tier == "remediation_required":
        return "source registry holds source pending remediation"
    if notes:
        return f"source registry excludes source: {notes}"
    return "source registry excludes source from ESG index"


def chunk_row_excluded(row: dict) -> bool:
    return (
        not parse_bool(row.get("include_in_esg_index", "true"))
        or (row.get("rag_action") or "").strip() == "exclude_from_esg_index"
    )


def registry_aware_document_policy(
    *,
    registry_row: dict | None,
    chunk_rows: list[dict],
    doc_quality_status: str,
    tracker_status: str,
) -> tuple[str, str, list[str]]:
    notes: list[str] = []
    rag_action = rag_action_for_quality_status(doc_quality_status, tracker_status)

    if registry_row is not None:
        retrieval_tier = (registry_row.get("retrieval_tier") or "").strip()
        source_type = (registry_row.get("source_type") or "").strip()
        source_scope = (registry_row.get("source_scope") or "").strip()
        include = parse_bool(registry_row.get("include_in_esg_index", "true"))
        notes.append(
            "source registry policy: "
            f"type={source_type or 'unknown'}, "
            f"scope={source_scope or 'unknown'}, "
            f"tier={retrieval_tier or 'unknown'}, "
            f"include_in_esg_index={str(include).lower()}"
        )
        if not include:
            return (
                "source_registry_excluded",
                "exclude_from_esg_index",
                [*notes, source_registry_exclusion_reason(registry_row)],
            )

    if (
        rag_action == "index_as_esg"
        and chunk_rows
        and all(chunk_row_excluded(row) for row in chunk_rows)
    ):
        return (
            "source_registry_excluded",
            "exclude_from_esg_index",
            [
                *notes,
                "all document chunks are excluded by chunk policy; QA cannot mark document index_as_esg",
            ],
        )

    if (
        rag_action == "index_as_esg"
        and chunk_rows
        and any(chunk_row_excluded(row) for row in chunk_rows)
    ):
        notes.append("some chunks are excluded from retrieval by row-level policy")

    return doc_quality_status, rag_action, notes


def status_for_row(
    tracker_status: str,
    pdf_count: int,
    parsed_count: int,
    ocr_required_count: int,
    failed_parse_count: int,
    section_count: int,
    chunk_count: int,
    invalid_chunk_count: int,
    doc_quality_status: str,
    missing_citation_metadata_count: int,
    layout_audit_status: str = "not_applicable",
    layout_auto_hold_page_count: int = 0,
) -> str:
    if doc_quality_status == "source_registry_excluded":
        return "excluded_from_esg_index"
    if tracker_status == "not_found":
        if pdf_count or parsed_count or section_count or chunk_count:
            return "tracker_needs_cleanup"
        return "not_found"
    if tracker_status != "downloaded":
        return "tracker_needs_cleanup"
    if pdf_count == 0:
        return "missing_pdf"
    if doc_quality_status in {"exclude_from_esg_rag", "needs_review"}:
        return "needs_review"
    if layout_audit_status == "missing_or_stale":
        return "needs_review"
    if chunk_count > 0 and missing_citation_metadata_count > 0:
        return "needs_review"
    if parsed_count > 0 and section_count > 0 and chunk_count > 0 and invalid_chunk_count == 0:
        if layout_auto_hold_page_count:
            return "complete_with_layout_quarantine"
        return "complete"
    if parsed_count == 0 and ocr_required_count > 0 and failed_parse_count == 0:
        return "ocr_required"
    if parsed_count == 0 and failed_parse_count > 0 and ocr_required_count == 0:
        return "parse_failed"
    return "incomplete"


def summarize_counts(rows: list[dict], field: str) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[row.get(field, "")] += 1
    return dict(sorted(counts.items()))


def print_priority_fixes(rows: list[dict], cleanup_tickers: list[str]) -> None:
    print()
    print("Priority fixes:")

    priorities = [
        (
            "possible wrong document type",
            [
                r["ticker"]
                for r in rows
                if parse_int(r.get("wrong_doc_type_count")) and parse_int(r.get("wrong_doc_type_count")) > 0
            ],
        ),
        ("downloaded but no local PDF", [r["ticker"] for r in rows if r["status"] == "missing_pdf"]),
        (
            "parsed but zero sections",
            [
                r["ticker"]
                for r in rows
                if parse_int(r["parsed_count"]) and not parse_int(r["section_count"])
            ],
        ),
        (
            "sections but zero chunks",
            [
                r["ticker"]
                for r in rows
                if parse_int(r["section_count"]) and not parse_int(r["chunk_count"])
            ],
        ),
        (
            "invalid chunk token counts",
            [
                r["ticker"]
                for r in rows
                if "invalid chunk token count" in r.get("notes", "")
            ],
        ),
        (
            "chunks without verified semantic citation provenance",
            [
                r["ticker"]
                for r in rows
                if parse_int(r.get("missing_citation_metadata_count"))
                and parse_int(r.get("missing_citation_metadata_count")) > 0
            ],
        ),
        ("tracker cleanup issues", cleanup_tickers),
    ]

    for title, tickers in priorities:
        unique = sorted(set(tickers))
        preview = ", ".join(unique[:20])
        if len(unique) > 20:
            preview += f", ... {len(unique) - 20} more"
        print(f"  {title}: {len(unique)}" + (f" ({preview})" if preview else ""))


def run(
    out: str | Path,
    tracker_path: str | Path = "data/00_reference/sustainability_report_tracker.csv",
    companies_path: str | Path = "data/00_reference/companies.csv",
    parse_index_path: str | Path = "data/00_reference/esg_parse_index.csv",
    sections_index_path: str | Path = "data/00_reference/esg_sections_index.csv",
    chunks_index_path: str | Path = "data/00_reference/esg_chunks_index.csv",
    source_registry_path: str | Path = "data/00_reference/esg_source_registry.csv",
    layout_audit_path: str | Path = "data/00_reference/esg_page_layout_qa.csv",
    raw_root: str | Path = "data/01_raw/sustainability",
    ticker: str | None = None,
    pdf_stem: str | None = None,
) -> list[dict]:
    out_path = Path(out)
    tracker_rows = read_csv(Path(tracker_path))
    company_map = load_company_map(Path(companies_path))
    parse_rows = read_csv(Path(parse_index_path))
    section_rows = read_csv(Path(sections_index_path))
    chunk_rows = read_csv(Path(chunks_index_path))
    source_registry = load_source_registry(Path(source_registry_path))
    layout_by_doc = load_layout_audit(Path(layout_audit_path))
    local_pdfs = count_local_pdfs(Path(raw_root))

    selected_ticker = ticker.strip().upper() if ticker else None
    selected_pdf_stem = Path(pdf_stem).stem if pdf_stem else None
    if selected_ticker:
        parse_rows = [
            row for row in parse_rows
            if (row.get("ticker") or "").strip().upper() == selected_ticker
        ]
        tracker_rows = [
            row for row in tracker_rows
            if (row.get("ticker") or "").strip().upper() == selected_ticker
        ]
    if selected_pdf_stem:
        parse_rows = [
            row for row in parse_rows
            if pdf_stem_from_parse_row(row) == selected_pdf_stem
        ]
    if selected_ticker or selected_pdf_stem:
        section_rows = [
            row for row in section_rows
            if (
                (not selected_ticker or (row.get("ticker") or "").strip().upper() == selected_ticker)
                and (not selected_pdf_stem or (row.get("pdf_stem") or "").strip() == selected_pdf_stem)
            )
        ]
        chunk_rows = [
            row for row in chunk_rows
            if (
                (not selected_ticker or (row.get("ticker") or "").strip().upper() == selected_ticker)
                and (not selected_pdf_stem or (row.get("pdf_stem") or "").strip() == selected_pdf_stem)
            )
        ]

    parse_by_ticker = group_by(parse_rows, "ticker")
    parse_by_doc = group_parse_by_doc(parse_rows)
    sections_by_doc = group_by_doc(section_rows)
    chunks_by_doc = group_by_doc(chunk_rows)

    rows: list[dict] = []
    cleanup_tickers: list[str] = []
    covered_doc_keys: set[tuple[str, str]] = set()

    def append_row_for_doc(
        *,
        tracker: dict,
        ticker: str,
        pdf_stem: str,
        parse_doc_rows: list[dict],
        section_doc_rows: list[dict],
        chunk_doc_rows: list[dict],
        matching_pdfs: list[Path],
        extra_notes: list[str] | None = None,
    ) -> None:
        company = company_map.get(ticker, {})
        tracker_status = normalize_status(tracker.get("status"))
        token_counts = chunk_token_counts(chunk_doc_rows)
        invalid_chunk_count = sum(
            1 for row in chunk_doc_rows if not valid_chunk_token_count(row)
        )
        short_evidence_chunk_count = sum(
            1
            for row in chunk_doc_rows
            if (row.get("chunk_type") or CHUNK_TYPE_NORMAL).strip()
            == CHUNK_TYPE_SHORT_EVIDENCE
        )
        citation_ready_chunk_count = sum(
            1
            for row in chunk_doc_rows
            if is_semantically_citation_ready(row)
        )
        missing_citation_metadata_count = max(
            len(chunk_doc_rows) - citation_ready_chunk_count,
            0,
        )

        parsed_count = sum(1 for row in parse_doc_rows if row.get("status") == "parsed")
        ocr_required_count = sum(1 for row in parse_doc_rows if row.get("status") == "ocr_required")
        failed_parse_count = sum(1 for row in parse_doc_rows if row.get("status") == "failed")
        doc_quality_status = quality_status_for_parse_rows(parse_doc_rows)
        quality_flag_count = sum(1 for row in parse_doc_rows if row_quality_flags(row))
        wrong_doc_type_count = sum(1 for row in parse_doc_rows if parse_bool(row.get("possible_wrong_doc_type")))
        garbled_text_count = sum(1 for row in parse_doc_rows if "garbled_text" in row_quality_flags(row))
        low_text_quality_count = sum(
            1
            for row in parse_doc_rows
            if row_quality_flags(row) & {"low_readable_word_ratio", "low_text_per_page"}
        )
        layout_summary = layout_summary_for_doc(
            layout_by_doc.get((ticker, pdf_stem), []),
            parse_doc_rows,
            chunk_doc_rows,
        )
        bad_parse_status = [
            row.get("status", "")
            for row in parse_doc_rows
            if row.get("status") not in VALID_PARSE_STATUSES
        ]

        notes: list[str] = []
        if extra_notes:
            notes.extend(extra_notes)
        if not company:
            notes.append("ticker missing from companies.csv")
            cleanup_tickers.append(ticker)
        if tracker_status not in {"downloaded", "not_found"}:
            notes.append("blank or invalid tracker status")
            cleanup_tickers.append(ticker)
        if tracker_status == "not_found" and (
            parse_doc_rows or section_doc_rows or chunk_doc_rows
        ):
            notes.append("tracker says not_found but local ESG outputs exist")
            cleanup_tickers.append(ticker)
        if tracker_status == "downloaded" and not (tracker.get("format") or "").strip():
            notes.append("downloaded row has blank format")
            cleanup_tickers.append(ticker)
        if tracker_status == "downloaded" and is_bad_drive_link(tracker.get("drive_file_link"), ticker):
            notes.append("drive_file_link is blank or not a usable URL/file ID")
            cleanup_tickers.append(ticker)
        if has_multi_year(tracker.get("report_year")):
            notes.append("report_year contains multiple years; DB loader uses latest year unless files clearly split by year")
        if bad_parse_status:
            notes.append(f"invalid parse status values: {sorted(set(bad_parse_status))}")
        if wrong_doc_type_count:
            notes.append("possible 10-K or SEC filing in sustainability folder; manual review before indexing")
        if garbled_text_count:
            notes.append("garbled parsed text")
        if any("low_readable_word_ratio" in row_quality_flags(row) for row in parse_doc_rows):
            notes.append("low readable word ratio")
        if any("low_text_per_page" in row_quality_flags(row) for row in parse_doc_rows):
            notes.append("low text per page")
        if layout_summary["status"] == "missing_or_stale":
            doc_quality_status = "needs_review"
            notes.append("layout audit missing or stale for current parsed text")
        elif layout_summary["auto_hold_page_count"]:
            notes.append(
                "layout auto-quarantine: "
                f"pages={layout_summary['auto_hold_page_count']}; "
                f"affected_chunks={layout_summary['held_chunk_count']}"
            )
        missing_text = missing_index_files(parse_doc_rows, "parsed_text_file")
        if missing_text:
            notes.append(f"missing parsed text files: {missing_text}")
        missing_sections = missing_index_files(section_doc_rows, "section_file")
        if missing_sections:
            notes.append(f"missing section files: {missing_sections}")
        missing_chunks = missing_index_files(chunk_doc_rows, "chunk_file")
        if missing_chunks:
            notes.append(f"missing chunk files: {missing_chunks}")
        if invalid_chunk_count:
            notes.append(f"invalid chunk token count rows: {invalid_chunk_count}")
        if missing_citation_metadata_count:
            notes.append(
                "chunks without verified semantic citation provenance: "
                f"{missing_citation_metadata_count}"
            )

        registry_row = source_registry.get((ticker, pdf_stem))
        governed_annual_excerpt = bool(
            wrong_doc_type_count
            and registry_row
            and parse_bool(registry_row.get("include_in_esg_index"))
            and (registry_row.get("source_scope") or "").strip() in {"esg_excerpt", "governed_esg_excerpt"}
            and (registry_row.get("source_type") or "").strip() in {"annual_report", "annual_report_with_esg"}
        )
        if governed_annual_excerpt:
            doc_quality_status = "ok"
            notes.append("approved registry rule admits a governed ESG excerpt from an annual report")
        doc_quality_status, rag_action, policy_notes = registry_aware_document_policy(
            registry_row=registry_row,
            chunk_rows=chunk_doc_rows,
            doc_quality_status=doc_quality_status,
            tracker_status=tracker_status,
        )
        notes.extend(policy_notes)

        pdf_count = len(matching_pdfs)
        status = status_for_row(
            tracker_status,
            pdf_count,
            parsed_count,
            ocr_required_count,
            failed_parse_count,
            len(section_doc_rows),
            len(chunk_doc_rows),
            invalid_chunk_count,
            doc_quality_status,
            missing_citation_metadata_count,
            str(layout_summary["status"]),
            int(layout_summary["auto_hold_page_count"]),
        )

        rows.append(
            {
                "company_id": tracker.get("company_id") or company.get("company_id", ""),
                "ticker": ticker,
                "company_name": tracker.get("company_name") or company.get("name", ""),
                "tracker_status": tracker.get("status", ""),
                "report_year": tracker.get("report_year", ""),
                "format": tracker.get("format", ""),
                "drive_file_link": tracker.get("drive_file_link", ""),
                "pdf_file": pdf_file_for_doc(pdf_stem, parse_doc_rows),
                "pdf_count": pdf_count,
                "parsed_count": parsed_count,
                "ocr_required_count": ocr_required_count,
                "failed_parse_count": failed_parse_count,
                "doc_quality_status": doc_quality_status,
                "rag_action": rag_action,
                "quality_flag_count": quality_flag_count,
                "wrong_doc_type_count": wrong_doc_type_count,
                "garbled_text_count": garbled_text_count,
                "low_text_quality_count": low_text_quality_count,
                "layout_audit_status": layout_summary["status"],
                "layout_checked_page_count": layout_summary["checked_page_count"],
                "layout_auto_hold_page_count": layout_summary["auto_hold_page_count"],
                "layout_held_chunk_count": layout_summary["held_chunk_count"],
                "section_count": len(section_doc_rows),
                "chunk_count": len(chunk_doc_rows),
                "short_evidence_chunk_count": short_evidence_chunk_count,
                "citation_ready_chunk_count": citation_ready_chunk_count,
                "missing_citation_metadata_count": missing_citation_metadata_count,
                "min_chunk_tokens": min(token_counts) if token_counts else "",
                "max_chunk_tokens": max(token_counts) if token_counts else "",
                "status": status,
                "notes": "; ".join(dict.fromkeys(notes)),
            }
        )

    for tracker in tracker_rows:
        ticker = (tracker.get("ticker") or "").strip().upper()
        if not ticker:
            continue

        ticker_parse_rows = filter_parse_rows_for_tracker(
            tracker,
            parse_by_ticker.get(ticker, []),
        )
        matched_pdf_stems = sorted(
            {pdf_stem_from_parse_row(row) for row in ticker_parse_rows if pdf_stem_from_parse_row(row)}
        )

        if selected_pdf_stem:
            matched_pdf_stems = [selected_pdf_stem]
        if matched_pdf_stems:
            for pdf_stem in matched_pdf_stems:
                key = (ticker, pdf_stem)
                if key in covered_doc_keys:
                    continue
                covered_doc_keys.add(key)
                append_row_for_doc(
                    tracker=tracker,
                    ticker=ticker,
                    pdf_stem=pdf_stem,
                    parse_doc_rows=parse_by_doc.get(key, []),
                    section_doc_rows=sections_by_doc.get(key, []),
                    chunk_doc_rows=chunks_by_doc.get(key, []),
                    matching_pdfs=[
                        path for path in local_pdfs.get(ticker, []) if path.stem == pdf_stem
                    ],
                )
            continue

        append_row_for_doc(
            tracker=tracker,
            ticker=ticker,
            pdf_stem="",
            parse_doc_rows=[],
            section_doc_rows=[],
            chunk_doc_rows=[],
            matching_pdfs=filter_paths_for_tracker(tracker, local_pdfs.get(ticker, [])),
        )

    all_doc_keys = set(parse_by_doc) | set(sections_by_doc) | set(chunks_by_doc)
    for ticker, pdf_stem in sorted(all_doc_keys - covered_doc_keys):
        append_row_for_doc(
            tracker={},
            ticker=ticker,
            pdf_stem=pdf_stem,
            parse_doc_rows=parse_by_doc.get((ticker, pdf_stem), []),
            section_doc_rows=sections_by_doc.get((ticker, pdf_stem), []),
            chunk_doc_rows=chunks_by_doc.get((ticker, pdf_stem), []),
            matching_pdfs=[
                path for path in local_pdfs.get(ticker, []) if path.stem == pdf_stem
            ],
            extra_notes=["parsed/sectioned/chunked ESG document has no matching tracker row"],
        )
        cleanup_tickers.append(ticker)

    rows = sorted(rows, key=lambda r: (r["status"], r["ticker"], r.get("pdf_file", "")))
    if selected_ticker or selected_pdf_stem:
        existing_rows = read_csv(out_path)
        rows.extend(
            row for row in existing_rows
            if not (
                (not selected_ticker or (row.get("ticker") or "").strip().upper() == selected_ticker)
                and (not selected_pdf_stem or Path(row.get("pdf_file") or "").stem == selected_pdf_stem)
            )
        )
        rows.sort(key=lambda r: (r["status"], r["ticker"], r.get("pdf_file", "")))
    write_csv(out_path, rows)

    print(f"Wrote ESG QA report: {out_path}")
    print("Status counts:")
    for status, count in summarize_counts(rows, "status").items():
        print(f"  {status}: {count}")
    print_priority_fixes(rows, cleanup_tickers)
    return rows


def main():
    parser = argparse.ArgumentParser(description="Create ESG pipeline QA summary CSV.")
    parser.add_argument("--out", default="data/00_reference/esg_pipeline_qa.csv")
    parser.add_argument("--tracker", default="data/00_reference/sustainability_report_tracker.csv")
    parser.add_argument("--companies", default="data/00_reference/companies.csv")
    parser.add_argument("--parse-index", default="data/00_reference/esg_parse_index.csv")
    parser.add_argument("--sections-index", default="data/00_reference/esg_sections_index.csv")
    parser.add_argument("--chunks-index", default="data/00_reference/esg_chunks_index.csv")
    parser.add_argument("--source-registry", default="data/00_reference/esg_source_registry.csv")
    parser.add_argument("--layout-audit", default="data/00_reference/esg_page_layout_qa.csv")
    parser.add_argument("--raw-root", default="data/01_raw/sustainability")
    parser.add_argument("--ticker", default=None)
    parser.add_argument("--pdf-file", default=None)
    parser.add_argument("--pdf-stem", default=None)
    args = parser.parse_args()
    if args.pdf_file and args.pdf_stem:
        parser.error("use only one of --pdf-file and --pdf-stem")

    run(
        out=args.out,
        tracker_path=args.tracker,
        companies_path=args.companies,
        parse_index_path=args.parse_index,
        sections_index_path=args.sections_index,
        chunks_index_path=args.chunks_index,
        source_registry_path=args.source_registry,
        layout_audit_path=args.layout_audit,
        raw_root=args.raw_root,
        ticker=args.ticker,
        pdf_stem=args.pdf_stem or (Path(args.pdf_file).stem if args.pdf_file else None),
    )


if __name__ == "__main__":
    main()
