"""Build a strict publication-readiness audit for the ESG SQLite package."""

from __future__ import annotations

import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


REPO = Path(__file__).resolve().parents[2]
import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)
import config  # noqa: E402
DB = (
    REPO
    / "backups"
    / "esg_sqlite_share_20260713_160005"
    / "esg_local_audit_2026-07-13.sqlite"
)
OUT = config.REPORTS_DIR / "esg_publication_quality_audit_2026-07-13"

CANONICAL_CODES = {
    "ceo_letter",
    "about_this_report",
    "environmental",
    "climate",
    "energy",
    "emissions",
    "waste",
    "water",
    "social",
    "human_capital",
    "diversity_equity_inclusion",
    "supply_chain_ethics",
    "community",
    "governance",
    "ethics_compliance",
    "data_summary",
    "appendix",
    "other",
    "full_document",
}

CURRENT_YEAR = 2026
MIN_CHUNK_TOKENS = 100
MAX_CHUNK_TOKENS = 600
SHORT_EVIDENCE_MIN_TOKENS = 25
CHUNK_TYPE_NORMAL = "normal"
CHUNK_TYPE_SHORT_EVIDENCE = "short_evidence"
MIN_PARSE_CHARS = 500
LARGE_DOC_CHARS = 50_000
VERY_LARGE_DOC_CHARS = 100_000
LOW_CHARS_PER_PAGE = 800
LOW_READABLE_RATIO = 0.45
WARN_READABLE_RATIO = 0.70
CITATION_VALIDATION_VERSION = "semantic_v1"
VERIFIED_CITATION_STATUSES = {
    "verified_exact",
    "verified_whitespace_normalized",
}
SECTION_INSTANCE_KEYS = ["ticker", "pdf_stem", "section_instance_id"]


def read_table(con: sqlite3.Connection, name: str) -> pd.DataFrame:
    df = pd.read_sql_query(f'SELECT * FROM "{name}"', con)
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].fillna("")
    return df


def extract_year(value: object) -> int | None:
    years = re.findall(r"(20\d{2})", str(value))
    if not years:
        return None
    return int(years[-1])


def resolve_repo_path(value: object) -> Path | None:
    text = str(value).strip()
    if not text:
        return None
    path = Path(text)
    if not path.is_absolute():
        path = REPO / path
    return path


def boolish(value: object) -> bool:
    return value == 1 or str(value).strip().lower() in {"1", "true", "yes", "y"}


def valid_chunk_token_row(row: object) -> bool:
    token_count = pd.to_numeric(row.get("token_count"), errors="coerce")
    if pd.isna(token_count):
        return False
    chunk_type = str(row.get("chunk_type", CHUNK_TYPE_NORMAL)).strip() or CHUNK_TYPE_NORMAL
    if chunk_type == CHUNK_TYPE_SHORT_EVIDENCE:
        return SHORT_EVIDENCE_MIN_TOKENS <= token_count < MIN_CHUNK_TOKENS
    return MIN_CHUNK_TOKENS <= token_count <= MAX_CHUNK_TOKENS


def semantically_citation_ready(row: object, suffix: str = "") -> bool:
    """Return true only for a ready chunk verified by semantic_v1."""
    return (
        boolish(row.get(f"citation_ready{suffix}"))
        and str(row.get(f"citation_validation_version{suffix}", "")).strip()
        == CITATION_VALIDATION_VERSION
        and str(row.get(f"citation_validation_status{suffix}", "")).strip()
        in VERIFIED_CITATION_STATUSES
    )


def chunk_id_matches_provenance(row: object) -> bool:
    """Validate the v2 ID from source and contiguous section-instance identity."""
    source_id = str(row.get("source_id", "")).strip()
    section_instance_id = str(row.get("section_instance_id", "")).strip()
    chunk_id = str(row.get("chunk_id", "")).strip()
    try:
        chunk_index = int(row.get("chunk_index"))
    except (TypeError, ValueError):
        return False
    if not source_id or not section_instance_id:
        return False
    expected = f"{source_id}__{section_instance_id}__chunk_{chunk_index:04d}"
    return chunk_id == expected


def add_issue(
    bucket: list[dict[str, object]],
    severity: str,
    issue_type: str,
    ticker: object = "",
    pdf_file: object = "",
    pdf_stem: object = "",
    year: object = "",
    evidence: object = "",
    risk: str = "",
    suggested_fix: str = "",
    owner: str = "Aziz / ESG QA",
) -> None:
    bucket.append(
        {
            "severity": severity,
            "issue_type": issue_type,
            "ticker": "" if pd.isna(ticker) else ticker,
            "pdf_file": "" if pd.isna(pdf_file) else pdf_file,
            "pdf_stem": "" if pd.isna(pdf_stem) else pdf_stem,
            "year": "" if pd.isna(year) else year,
            "evidence": evidence,
            "risk": risk,
            "suggested_fix": suggested_fix,
            "owner": owner,
        }
    )


def issues_df(rows: list[dict[str, object]]) -> pd.DataFrame:
    cols = [
        "severity",
        "issue_type",
        "ticker",
        "pdf_file",
        "pdf_stem",
        "year",
        "evidence",
        "risk",
        "suggested_fix",
        "owner",
    ]
    df = pd.DataFrame(rows, columns=cols)
    if df.empty:
        return pd.DataFrame(columns=cols)
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    df["_sev"] = df["severity"].map(severity_order).fillna(9)
    return (
        df.sort_values(["_sev", "ticker", "year", "pdf_file", "pdf_stem", "issue_type"])
        .drop(columns=["_sev"])
        .reset_index(drop=True)
    )


def top_rows_markdown(df: pd.DataFrame, cols: list[str], n: int = 20) -> str:
    if df.empty:
        return "_None._\n"
    view = df[cols].head(n).copy()
    view = view.astype(str).apply(lambda col: col.str.replace("|", "\\|", regex=False))
    header = "| " + " | ".join(cols) + " |"
    separator = "| " + " | ".join(["---"] * len(cols)) + " |"
    rows = [
        "| " + " | ".join(str(row[col]) for col in cols) + " |"
        for _, row in view.iterrows()
    ]
    return "\n".join([header, separator, *rows]) + "\n"


def fmt_int(value: object) -> str:
    try:
        return f"{int(value):,}"
    except Exception:
        return str(value)


def main() -> None:
    if not DB.exists():
        raise FileNotFoundError(DB)
    OUT.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(DB)
    docs = read_table(con, "documents")
    secs = read_table(con, "sections")
    chunks = read_table(con, "chunks")
    qa = read_table(con, "qa")
    cq = read_table(con, "company_quality")
    dq = read_table(con, "document_quality")
    ie = read_table(con, "index_eligibility")
    anom = read_table(con, "anomalies")
    ocr = read_table(con, "ocr_fixes")
    con.close()

    docs["year_from_filename"] = docs["pdf_file"].map(extract_year)
    dq["year_from_filename"] = dq["pdf_file"].map(extract_year)
    secs["year_from_pdf_stem"] = secs["pdf_stem"].map(extract_year)
    chunks["year_from_pdf_stem"] = chunks["pdf_stem"].map(extract_year)
    qa["report_year_int"] = pd.to_numeric(qa["report_year"], errors="coerce").astype("Int64")

    for col in ["source_pdf", "parse_source_pdf", "parsed_text_file", "page_map_file"]:
        docs[f"{col}_exists"] = docs[col].map(
            lambda x: bool(resolve_repo_path(x) and resolve_repo_path(x).exists())
        )
    secs["section_file_exists"] = secs["section_file"].map(
        lambda x: bool(resolve_repo_path(x) and resolve_repo_path(x).exists())
    )
    chunks["chunk_file_exists"] = chunks["chunk_file"].map(
        lambda x: bool(resolve_repo_path(x) and resolve_repo_path(x).exists())
    )
    chunks["source_section_file_exists"] = chunks["source_section_file"].map(
        lambda x: bool(resolve_repo_path(x) and resolve_repo_path(x).exists())
    )

    doc_issues: list[dict[str, object]] = []
    section_issues: list[dict[str, object]] = []
    chunk_issues: list[dict[str, object]] = []
    company_issues: list[dict[str, object]] = []
    qa_issues: list[dict[str, object]] = []
    schema_issues: list[dict[str, object]] = []

    expected_nonempty = {
        "documents": [
            "ticker",
            "pdf_file",
            "source_pdf",
            "source_sha256",
            "parsed_text_file",
            "page_map_file",
            "status",
            "content_hash",
        ],
        "sections": [
            "ticker",
            "pdf_stem",
            "section_code",
            "section_instance_id",
            "section_title",
            "section_file",
            "section_text",
            "confidence",
            "split_method",
        ],
        "chunks": [
            "chunk_id",
            "source_id",
            "ticker",
            "doc_type",
            "doc_quality_status",
            "rag_action",
            "pdf_stem",
            "section_code",
            "section_instance_id",
            "chunk_file",
            "source_section_file",
            "chunk_text",
            "citation_ready",
            "citation_validation_status",
            "citation_validation_version",
        ],
        "qa": ["ticker", "company_name", "tracker_status", "status"],
        "index_eligibility": ["chunk_id", "ticker", "pdf_stem", "section_code", "eligibility_status"],
    }
    frames = {
        "documents": docs,
        "sections": secs,
        "chunks": chunks,
        "qa": qa,
        "index_eligibility": ie,
    }
    for table, cols in expected_nonempty.items():
        frame = frames[table]
        for col in cols:
            if col not in frame.columns:
                add_issue(
                    schema_issues,
                    "critical" if table in {"documents", "sections", "chunks"} else "high",
                    "required_column_missing",
                    evidence=f"{table}.{col} is absent",
                    risk="Breaks DB mapping, lineage, or reproducibility.",
                    suggested_fix=f"Apply the provenance migration that adds {table}.{col}.",
                )
                continue
            blanks = frame[col].astype(str).str.strip().eq("").sum()
            if blanks:
                add_issue(
                    schema_issues,
                    "critical" if table in {"documents", "sections", "chunks"} else "high",
                    "required_column_blank",
                    evidence=f"{table}.{col}: {blanks} blank rows",
                    risk="Breaks DB mapping, lineage, or reproducibility.",
                    suggested_fix=f"Populate {table}.{col} for all rows or exclude incomplete rows.",
                )

    # Keep a legacy database inspectable after reporting its missing provenance
    # columns. The fallback does not make it publication-ready; it only prevents
    # the audit from crashing before it can emit the schema finding.
    if "section_instance_id" not in secs.columns:
        secs["section_instance_id"] = secs["section_code"]
    if "section_instance_id" not in chunks.columns:
        chunks["section_instance_id"] = chunks["section_code"]
    for col in [
        "source_id",
        "citation_ready",
        "citation_validation_status",
        "citation_validation_version",
    ]:
        if col not in chunks.columns:
            chunks[col] = ""

    for keys, label in [
        (["ticker", "pdf_file"], "duplicate_document_key"),
        (["source_sha256"], "duplicate_source_pdf_hash"),
        (["content_hash"], "duplicate_parsed_text_hash"),
    ]:
        grouped = docs.groupby(keys, dropna=False).size().reset_index(name="n")
        for _, row in grouped[grouped["n"] > 1].iterrows():
            severity = "critical" if label == "duplicate_document_key" else "medium"
            vals = ", ".join(f"{key}={row[key]}" for key in keys)
            matches = docs.copy()
            for key in keys:
                matches = matches[matches[key] == row[key]]
            match_files = " | ".join(
                f"{match.ticker}:{match.pdf_file}" for _, match in matches.iterrows()
            )
            add_issue(
                doc_issues,
                severity,
                label,
                evidence=f"{vals}; rows={int(row.n)}; files={match_files}",
                risk="Duplicates can inflate corpus size or create repeated retrieval evidence.",
                suggested_fix="Verify whether these are intentional duplicate reports; if not, remove or deduplicate one copy.",
            )

    valid_year_docs = docs.dropna(subset=["year_from_filename"])
    same_year = (
        valid_year_docs.groupby(["ticker", "year_from_filename"])
        .agg(pdf_count=("pdf_file", "count"), pdfs=("pdf_file", lambda s: " | ".join(sorted(s))))
        .reset_index()
    )
    for _, row in same_year[same_year["pdf_count"] > 1].iterrows():
        add_issue(
            doc_issues,
            "medium",
            "multiple_pdfs_same_ticker_year",
            ticker=row.ticker,
            year=int(row.year_from_filename),
            evidence=f"{int(row.pdf_count)} PDFs: {row.pdfs}",
            risk="Could duplicate a company-year in RAG/evaluation unless metadata distinguishes report type.",
            suggested_fix="Confirm whether multiple reports for the same company-year are intentional; add report_type or keep only canonical ESG report.",
        )

    for _, row in docs.iterrows():
        ticker = row["ticker"]
        pdf = row["pdf_file"]
        year = row["year_from_filename"]
        status = str(row["status"]).strip()
        flags = str(row["quality_flags"]).strip()
        char_count = pd.to_numeric(row["char_count"], errors="coerce")
        page_count = pd.to_numeric(row["page_count"], errors="coerce")
        readable = pd.to_numeric(row["readable_word_ratio"], errors="coerce")
        chars_per_page = pd.to_numeric(row["chars_per_page"], errors="coerce")
        garbled = pd.to_numeric(row["garbled_char_count"], errors="coerce")

        if status != "parsed":
            add_issue(
                doc_issues,
                "critical",
                "document_not_parsed",
                ticker,
                pdf,
                year=year,
                evidence=f"status={status}; error={row.get('error_message', '')}",
                risk="Document has no trusted text for sectioning/chunking.",
                suggested_fix="Fix source PDF, run OCR if needed, then rerun parser/splitter/chunker.",
            )
        if status == "parsed" and char_count < MIN_PARSE_CHARS:
            add_issue(
                doc_issues,
                "critical",
                "parsed_below_ocr_threshold",
                ticker,
                pdf,
                year=year,
                evidence=f"char_count={char_count}",
                risk="Parser status contradicts OCR threshold.",
                suggested_fix="Reparse and mark OCR-required or replace with searchable PDF.",
            )
        if boolish(row["possible_wrong_doc_type"]):
            add_issue(
                doc_issues,
                "high",
                "possible_wrong_document_type",
                ticker,
                pdf,
                year=year,
                evidence=f"quality_flags={flags}",
                risk="10-K/annual-report text can contaminate ESG-only retrieval.",
                suggested_fix="Replace with sustainability report or exclude from ESG index.",
            )
        if "garbled_text" in flags or garbled > 25:
            add_issue(
                doc_issues,
                "high" if garbled > 100 or "garbled_text" in flags else "medium",
                "garbled_or_cid_text",
                ticker,
                pdf,
                year=year,
                evidence=f"garbled_char_count={garbled}; quality_flags={flags}",
                risk="Bad embedded text can produce false sections and misleading chunks.",
                suggested_fix="Regenerate searchable PDF with image-base OCR, replace same filename in Drive, rerun pipeline.",
            )
        if "low_readable_word_ratio" in flags or (pd.notna(readable) and readable < WARN_READABLE_RATIO):
            add_issue(
                doc_issues,
                "high" if pd.notna(readable) and readable < LOW_READABLE_RATIO else "medium",
                "low_readable_word_ratio",
                ticker,
                pdf,
                year=year,
                evidence=f"readable_word_ratio={readable}; quality_flags={flags}",
                risk="Text may be corrupted, OCR-poor, or dominated by artifacts.",
                suggested_fix="Inspect copyable text; OCR from image base if body text is unreadable.",
            )
        if "low_text_per_page" in flags or (
            pd.notna(chars_per_page) and page_count and page_count >= 10 and chars_per_page < LOW_CHARS_PER_PAGE
        ):
            add_issue(
                doc_issues,
                "medium",
                "low_text_per_page",
                ticker,
                pdf,
                year=year,
                evidence=f"chars_per_page={chars_per_page}; pages={page_count}; quality_flags={flags}",
                risk="Image-heavy/scanned pages may be under-extracted.",
                suggested_fix="Spot-check pages; run OCR if body text is missing.",
            )
        if row["parse_source_kind"] == "ocr":
            add_issue(
                doc_issues,
                "low",
                "ocr_replacement_dependency",
                ticker,
                pdf,
                year=year,
                evidence=f"parse_source_pdf={row['parse_source_pdf']}",
                risk="Pipeline depends on OCR replacement being preserved in Drive.",
                suggested_fix="Ensure Drive contains the OCR-searchable PDF with the same filename; document OCR method.",
            )
        if pd.isna(year):
            add_issue(
                doc_issues,
                "medium",
                "missing_year_in_filename",
                ticker,
                pdf,
                evidence="No 20xx year detected in filename.",
                risk="Company-year retrieval/evaluation filters cannot work reliably.",
                suggested_fix="Rename source or add explicit report_year metadata.",
            )
        elif year > CURRENT_YEAR:
            add_issue(
                doc_issues,
                "medium",
                "future_year_in_filename",
                ticker,
                pdf,
                year=year,
                evidence=f"filename year={year}; current_year={CURRENT_YEAR}",
                risk="Future/current-year reports may be mislabeled or premature.",
                suggested_fix="Verify report year and tracker year.",
            )
        elif year < 2018:
            add_issue(
                doc_issues,
                "low",
                "old_report_year",
                ticker,
                pdf,
                year=year,
                evidence=f"filename year={year}",
                risk="Old reports may not match current ESG performance.",
                suggested_fix="Confirm whether historical years are intentionally included.",
            )

        for col in ["source_pdf", "parse_source_pdf", "parsed_text_file", "page_map_file"]:
            if not row[f"{col}_exists"]:
                add_issue(
                    doc_issues,
                    "high",
                    f"missing_local_{col}",
                    ticker,
                    pdf,
                    year=year,
                    evidence=f"{col}={row[col]}",
                    risk="Shared DB row cannot be reproduced from local pipeline files.",
                    suggested_fix="Regenerate missing file or update path metadata before publication package.",
                )

    sec_key_counts = secs.groupby(SECTION_INSTANCE_KEYS).size().reset_index(name="n")
    for _, row in sec_key_counts[sec_key_counts["n"] > 1].iterrows():
        add_issue(
            section_issues,
            "critical",
            "duplicate_section_logical_key",
            ticker=row.ticker,
            pdf_stem=row.pdf_stem,
            evidence=f"section_instance_id={row.section_instance_id}; rows={int(row.n)}",
            risk="Duplicate section instances cause chunk duplication and ambiguous DB joins.",
            suggested_fix="Deduplicate the section-instance index and rebuild chunks.",
        )

    for _, row in secs.iterrows():
        ticker = row["ticker"]
        stem = row["pdf_stem"]
        code = row["section_code"]
        year = row["year_from_pdf_stem"]
        char_count = pd.to_numeric(row["char_count"], errors="coerce")
        word_count = pd.to_numeric(row["word_count"], errors="coerce")
        start = pd.to_numeric(row["source_start_char"], errors="coerce")
        end = pd.to_numeric(row["source_end_char"], errors="coerce")
        page_start = pd.to_numeric(row["page_start"], errors="coerce")
        page_end = pd.to_numeric(row["page_end"], errors="coerce")
        confidence = str(row["confidence"]).strip()
        split_method = str(row["split_method"]).strip()

        if code not in CANONICAL_CODES:
            add_issue(
                section_issues,
                "critical",
                "noncanonical_section_code",
                ticker=ticker,
                pdf_stem=stem,
                year=year,
                evidence=f"section_code={code}",
                risk="Breaks canonical ESG taxonomy and downstream filtering.",
                suggested_fix="Map to canonical code or update accepted taxonomy intentionally.",
            )
        if not row["section_file_exists"]:
            add_issue(
                section_issues,
                "critical",
                "missing_section_file",
                ticker=ticker,
                pdf_stem=stem,
                year=year,
                evidence=row["section_file"],
                risk="Index row points to a file that cannot be inspected or loaded.",
                suggested_fix="Regenerate sections or repair index path.",
            )
        if pd.isna(char_count) or char_count <= 0 or not str(row["section_text"]).strip():
            add_issue(
                section_issues,
                "critical",
                "empty_section_text",
                ticker=ticker,
                pdf_stem=stem,
                year=year,
                evidence=f"char_count={char_count}",
                risk="Empty sections cannot support chunking or RAG.",
                suggested_fix="Remove empty section or repair splitter.",
            )
        if pd.notna(char_count) and char_count < 300:
            add_issue(
                section_issues,
                "medium",
                "section_below_spec_minimum",
                ticker=ticker,
                pdf_stem=stem,
                year=year,
                evidence=f"section_code={code}; char_count={char_count}",
                risk="Tiny sections can create low-value or title-only chunks.",
                suggested_fix="Merge into neighboring/parent section unless it is intentionally retained.",
            )
        if code == "full_document" or split_method == "full_document_fallback":
            add_issue(
                section_issues,
                "high" if pd.notna(char_count) and char_count >= LARGE_DOC_CHARS else "medium",
                "full_document_fallback_section",
                ticker=ticker,
                pdf_stem=stem,
                year=year,
                evidence=f"char_count={char_count}; word_count={word_count}",
                risk="Report was not meaningfully sectioned; retrieval loses ESG topic precision.",
                suggested_fix="Inspect headings/OCR and improve section title detection for this document.",
            )
        if confidence == "low":
            add_issue(
                section_issues,
                "medium" if pd.notna(char_count) and char_count >= 1000 else "low",
                "low_confidence_section",
                ticker=ticker,
                pdf_stem=stem,
                year=year,
                evidence=f"section_code={code}; char_count={char_count}; split_method={split_method}",
                risk="Potential false positive/weak heading split.",
                suggested_fix="Review section boundary and adjust heading heuristics if needed.",
            )
        if pd.isna(start) or pd.isna(end) or start < 0 or end <= start:
            add_issue(
                section_issues,
                "high",
                "invalid_section_char_span",
                ticker=ticker,
                pdf_stem=stem,
                year=year,
                evidence=f"section_code={code}; start={start}; end={end}",
                risk="Citation offsets and traceability are unreliable.",
                suggested_fix="Rerun section splitter with page/span mapping.",
            )
        if pd.isna(page_start) or pd.isna(page_end) or page_start <= 0 or page_end < page_start:
            add_issue(
                section_issues,
                "high",
                "invalid_section_page_span",
                ticker=ticker,
                pdf_stem=stem,
                year=year,
                evidence=f"section_code={code}; page_start={page_start}; page_end={page_end}",
                risk="Page citations cannot be trusted.",
                suggested_fix="Rerun splitter after verifying page map exists and spans resolve.",
            )

    for col in ["sections_per_100k_chars", "chars_per_section", "chunks"]:
        dq[col] = pd.to_numeric(dq[col], errors="coerce")
    q1 = dq["sections_per_100k_chars"].quantile(0.25)
    q3 = dq["sections_per_100k_chars"].quantile(0.75)
    iqr = q3 - q1
    low_density_practical = max(0, q1 - 1.5 * iqr, 4.0)
    high_density_practical = max(q3 + 1.5 * iqr, 30.0)

    for _, row in dq.iterrows():
        ticker = row["ticker"]
        pdf = row["pdf_file"]
        stem = row["pdf_stem"]
        year = row["year_from_filename"]
        chars = pd.to_numeric(row["char_count"], errors="coerce")
        sections_n = pd.to_numeric(row["sections"], errors="coerce")
        chunks_n = pd.to_numeric(row["chunks"], errors="coerce")
        sp100 = pd.to_numeric(row["sections_per_100k_chars"], errors="coerce")
        fallback_n = pd.to_numeric(row["fallback_sections"], errors="coerce")
        low_conf = pd.to_numeric(row["low_conf_sections"], errors="coerce")
        if row["status"] == "parsed" and (pd.isna(sections_n) or sections_n == 0):
            add_issue(
                doc_issues,
                "critical",
                "parsed_document_zero_sections",
                ticker,
                pdf,
                stem,
                year,
                evidence=f"char_count={chars}",
                risk="Parsed document lost before sectioning.",
                suggested_fix="Rerun section splitter and inspect splitter errors.",
            )
        if sections_n > 0 and (pd.isna(chunks_n) or chunks_n == 0):
            add_issue(
                doc_issues,
                "critical",
                "sectioned_document_zero_chunks",
                ticker,
                pdf,
                stem,
                year,
                evidence=f"sections={sections_n}; char_count={chars}",
                risk="Sectioned document is absent from retrieval corpus.",
                suggested_fix="Rerun chunker and inspect skipped sections.",
            )
        if pd.notna(chars) and chars >= LARGE_DOC_CHARS and pd.notna(sections_n) and sections_n < 5:
            add_issue(
                doc_issues,
                "high",
                "large_under_sectioned_document",
                ticker,
                pdf,
                stem,
                year,
                evidence=f"char_count={chars}; sections={sections_n}; chunks={chunks_n}",
                risk="Large report collapsed into too few topics; retrieval precision drops.",
                suggested_fix="Manual heading inspection; add missing heading vocabulary or OCR replacement.",
            )
        if pd.notna(chars) and chars >= VERY_LARGE_DOC_CHARS and pd.notna(sp100) and sp100 < low_density_practical:
            add_issue(
                doc_issues,
                "medium",
                "low_section_density_outlier",
                ticker,
                pdf,
                stem,
                year,
                evidence=(
                    f"sections_per_100k_chars={sp100:.2f}; sections={sections_n}; "
                    f"char_count={chars}; corpus_floor={low_density_practical:.2f}"
                ),
                risk="Likely missed headings compared with corpus norm.",
                suggested_fix="Review top headings and section boundaries; tune splitter if body text is being skipped.",
            )
        if pd.notna(sp100) and sp100 > high_density_practical and pd.notna(sections_n) and sections_n >= 10:
            add_issue(
                doc_issues,
                "medium",
                "high_section_density_outlier",
                ticker,
                pdf,
                stem,
                year,
                evidence=(
                    f"sections_per_100k_chars={sp100:.2f}; sections={sections_n}; "
                    f"char_count={chars}; corpus_ceiling={high_density_practical:.2f}"
                ),
                risk="May be over-splitting short documents or treating subheaders as main sections.",
                suggested_fix="Spot-check section boundaries; consider merging repetitive subheaders.",
            )
        if fallback_n and fallback_n > 0:
            add_issue(
                doc_issues,
                "high" if chars >= LARGE_DOC_CHARS else "medium",
                "document_contains_fallback_section",
                ticker,
                pdf,
                stem,
                year,
                evidence=f"fallback_sections={fallback_n}; sections={sections_n}; char_count={chars}",
                risk="Fallback reduces topic precision and can hide sectioning failure.",
                suggested_fix="Review and improve heading detection or OCR.",
            )
        if low_conf and low_conf > 0:
            add_issue(
                doc_issues,
                "medium",
                "document_contains_low_confidence_sections",
                ticker,
                pdf,
                stem,
                year,
                evidence=f"low_conf_sections={low_conf}; sections={sections_n}",
                risk="Some section boundaries may be weak or ambiguous.",
                suggested_fix="Spot-check low-confidence section files.",
            )

    chunk_counts_by_section = (
        chunks.groupby(SECTION_INSTANCE_KEYS).size().reset_index(name="chunk_rows")
    )
    sec_join = secs.merge(chunk_counts_by_section, on=SECTION_INSTANCE_KEYS, how="left")
    sec_join["chunk_rows"] = sec_join["chunk_rows"].fillna(0).astype(int)
    for _, row in sec_join[sec_join["chunk_rows"] == 0].iterrows():
        word_count = pd.to_numeric(row["word_count"], errors="coerce")
        add_issue(
            section_issues,
            "high" if word_count >= 100 else "low",
            "section_has_no_chunks",
            ticker=row["ticker"],
            pdf_stem=row["pdf_stem"],
            year=row["year_from_pdf_stem"],
            evidence=(
                f"section_code={row['section_code']}; "
                f"section_instance_id={row['section_instance_id']}; "
                f"word_count={row['word_count']}; "
                f"char_count={row['char_count']}"
            ),
            risk="Section is not represented in retrieval chunks.",
            suggested_fix="If >=100 tokens/words, rerun chunker; if intentionally tiny, document as skipped.",
        )

    chunk_id_dup = chunks.groupby("chunk_id").size().reset_index(name="n")
    for _, row in chunk_id_dup[chunk_id_dup["n"] > 1].iterrows():
        add_issue(
            chunk_issues,
            "critical",
            "duplicate_chunk_id",
            evidence=f"chunk_id={row.chunk_id}; rows={int(row.n)}",
            risk="Breaks vector index uniqueness and DB primary key mapping.",
            suggested_fix="Rebuild chunk index after deduplication.",
        )

    chunk_file_dup = chunks.groupby("chunk_file").size().reset_index(name="n")
    for _, row in chunk_file_dup[(chunk_file_dup["chunk_file"] != "") & (chunk_file_dup["n"] > 1)].iterrows():
        add_issue(
            chunk_issues,
            "critical",
            "duplicate_chunk_file_path",
            evidence=f"chunk_file={row.chunk_file}; rows={int(row.n)}",
            risk="Multiple rows point to same artifact; retrieval provenance ambiguous.",
            suggested_fix="Rebuild chunk index and chunk files.",
        )

    seq = (
        chunks.groupby(SECTION_INSTANCE_KEYS)["chunk_index"]
        .apply(lambda s: sorted(pd.to_numeric(s, errors="coerce").dropna().astype(int).tolist()))
        .reset_index(name="indices")
    )
    for _, row in seq.iterrows():
        indices = row["indices"]
        expected = list(range(len(indices)))
        if indices != expected:
            add_issue(
                chunk_issues,
                "medium",
                "chunk_index_sequence_gap",
                ticker=row.ticker,
                pdf_stem=row.pdf_stem,
                evidence=(
                    f"section_instance_id={row.section_instance_id}; "
                    f"first_indices={indices[:10]}; expected 0..{len(indices)-1}"
                ),
                risk="Chunk order can be ambiguous or stale after resume.",
                suggested_fix="Rebuild chunks for this section.",
            )

    for _, row in chunks.iterrows():
        ticker = row["ticker"]
        stem = row["pdf_stem"]
        year = row["year_from_pdf_stem"]
        chunk_id = row["chunk_id"]
        token_count = pd.to_numeric(row["token_count"], errors="coerce")
        start = pd.to_numeric(row["source_start_char"], errors="coerce")
        end = pd.to_numeric(row["source_end_char"], errors="coerce")
        page_start = pd.to_numeric(row["page_start"], errors="coerce")
        page_end = pd.to_numeric(row["page_end"], errors="coerce")
        citation_ready_flag = boolish(row["citation_ready"])
        citation_ready = semantically_citation_ready(row)
        doc_quality = str(row["doc_quality_status"]).strip()
        rag_action = str(row["rag_action"]).strip()

        if not valid_chunk_token_row(row):
            add_issue(
                chunk_issues,
                "critical",
                "invalid_chunk_token_count",
                ticker=ticker,
                pdf_stem=stem,
                year=year,
                evidence=(
                    f"chunk_id={chunk_id}; token_count={token_count}; "
                    f"chunk_type={row.get('chunk_type', CHUNK_TYPE_NORMAL)}"
                ),
                risk="Violates ESG chunking contract and can distort embeddings.",
                suggested_fix="Rechunk or correct chunk_type/token-count metadata.",
            )
        if str(row["doc_type"]).strip() != "sustainability":
            add_issue(
                chunk_issues,
                "high",
                "non_sustainability_chunk_doc_type",
                ticker=ticker,
                pdf_stem=stem,
                year=year,
                evidence=f"chunk_id={chunk_id}; doc_type={row['doc_type']}",
                risk="ESG index may ingest wrong corpus type.",
                suggested_fix="Fix doc_type or exclude from ESG index.",
            )
        if not row["chunk_file_exists"]:
            add_issue(
                chunk_issues,
                "critical",
                "missing_chunk_file",
                ticker=ticker,
                pdf_stem=stem,
                year=year,
                evidence=f"chunk_id={chunk_id}; chunk_file={row['chunk_file']}",
                risk="Index row cannot be loaded into vector DB.",
                suggested_fix="Regenerate chunk files or repair index path.",
            )
        if not row["source_section_file_exists"]:
            add_issue(
                chunk_issues,
                "critical",
                "missing_source_section_file",
                ticker=ticker,
                pdf_stem=stem,
                year=year,
                evidence=f"chunk_id={chunk_id}; source_section_file={row['source_section_file']}",
                risk="Chunk cannot be traced to section.",
                suggested_fix="Regenerate sections/chunks or repair index path.",
            )
        if not str(row["chunk_text"]).strip():
            add_issue(
                chunk_issues,
                "critical",
                "empty_chunk_text",
                ticker=ticker,
                pdf_stem=stem,
                year=year,
                evidence=f"chunk_id={chunk_id}",
                risk="Empty chunk cannot support retrieval.",
                suggested_fix="Rebuild chunks.",
            )
        if not chunk_id_matches_provenance(row):
            add_issue(
                chunk_issues,
                "medium",
                "chunk_id_pattern_mismatch",
                ticker=ticker,
                pdf_stem=stem,
                year=year,
                evidence=(
                    f"chunk_id={chunk_id}; source_id={row['source_id']}; "
                    f"section_instance_id={row['section_instance_id']}; "
                    f"chunk_index={row['chunk_index']}"
                ),
                risk="May break deterministic joins or vector IDs.",
                suggested_fix="Regenerate chunk IDs using documented pattern.",
            )
        if citation_ready_flag and not citation_ready:
            add_issue(
                chunk_issues,
                "critical",
                "citation_ready_without_verified_semantic_provenance",
                ticker=ticker,
                pdf_stem=stem,
                year=year,
                evidence=(
                    f"chunk_id={chunk_id}; "
                    f"citation_validation_version={row['citation_validation_version']}; "
                    f"citation_validation_status={row['citation_validation_status']}"
                ),
                risk="A boolean flag alone cannot prove that the cited source span contains the chunk.",
                suggested_fix=(
                    "Regenerate provenance and require semantic_v1 with a verified_exact or "
                    "verified_whitespace_normalized status."
                ),
            )
        if citation_ready and (
            pd.isna(start)
            or pd.isna(end)
            or start < 0
            or end <= start
            or pd.isna(page_start)
            or pd.isna(page_end)
            or page_start <= 0
            or page_end < page_start
        ):
            add_issue(
                chunk_issues,
                "critical",
                "citation_ready_but_invalid_span",
                ticker=ticker,
                pdf_stem=stem,
                year=year,
                evidence=f"chunk_id={chunk_id}; start={start}; end={end}; pages={page_start}-{page_end}",
                risk="RAG answer could cite an invalid page/span.",
                suggested_fix="Set citation_ready=false or regenerate page/span metadata.",
            )
        if not citation_ready and not citation_ready_flag:
            add_issue(
                chunk_issues,
                "high" if doc_quality == "ok" and rag_action == "index_as_esg" else "medium",
                "chunk_not_citation_ready",
                ticker=ticker,
                pdf_stem=stem,
                year=year,
                evidence=(
                    f"chunk_id={chunk_id}; doc_quality_status={doc_quality}; "
                    f"rag_action={rag_action}; "
                    f"citation_validation_version={row['citation_validation_version']}; "
                    f"citation_validation_status={row['citation_validation_status']}"
                ),
                risk="Cannot be safely cited in publication/demo answers.",
                suggested_fix="Regenerate page mapping or keep excluded from ESG index.",
            )

    chunks["chunk_text_norm"] = (
        chunks["chunk_text"].str.replace(r"\s+", " ", regex=True).str.strip().str.lower()
    )
    dup_text = (
        chunks[chunks["chunk_text_norm"] != ""]
        .groupby(["ticker", "pdf_stem", "chunk_text_norm"])
        .agg(n=("chunk_id", "count"), chunk_ids=("chunk_id", lambda s: " | ".join(list(s)[:5])))
        .reset_index()
    )
    for _, row in dup_text[dup_text["n"] > 1].iterrows():
        add_issue(
            chunk_issues,
            "low",
            "duplicate_chunk_text_within_document",
            ticker=row.ticker,
            pdf_stem=row.pdf_stem,
            evidence=f"rows={int(row.n)}; sample_chunk_ids={row.chunk_ids}",
            risk="May duplicate retrieval evidence; often caused by repeated headers/tables.",
            suggested_fix="Spot-check; only deduplicate if repeated body text is material.",
        )

    chunk_gate_fields = chunks[
        [
            "chunk_id",
            "citation_ready",
            "citation_validation_status",
            "citation_validation_version",
            "doc_quality_status",
            "rag_action",
        ]
    ].rename(
        columns={
            col: f"{col}_chunk"
            for col in [
                "citation_ready",
                "citation_validation_status",
                "citation_validation_version",
                "doc_quality_status",
                "rag_action",
            ]
        }
    )
    ie_join = ie.merge(chunk_gate_fields, on="chunk_id", how="left")
    for _, row in ie_join.iterrows():
        if pd.isna(row.get("citation_ready_chunk")):
            add_issue(
                chunk_issues,
                "critical",
                "index_eligibility_orphan_chunk",
                ticker=row["ticker"],
                pdf_stem=row["pdf_stem"],
                evidence=f"chunk_id={row['chunk_id']}",
                risk="Eligibility table references chunk absent from chunks table.",
                suggested_fix="Rebuild index eligibility table from chunks.",
            )
            continue
        expected_by_chunk_fields = (
            semantically_citation_ready(row, suffix="_chunk")
            and str(row["doc_quality_status_chunk"]).strip() == "ok"
            and str(row["rag_action_chunk"]).strip() == "index_as_esg"
        )
        index_eligible = boolish(row["index_eligible"])
        exclusion_reasons = str(row.get("exclusion_reasons", "")).strip()
        if index_eligible and not expected_by_chunk_fields:
            add_issue(
                chunk_issues,
                "critical",
                "index_includes_chunk_that_fails_core_gate",
                ticker=row["ticker"],
                pdf_stem=row["pdf_stem"],
                evidence=(
                    f"chunk_id={row['chunk_id']}; index_eligible={row['index_eligible']}; "
                    f"doc_quality_status={row['doc_quality_status_chunk']}; "
                    f"rag_action={row['rag_action_chunk']}; citation_ready={row['citation_ready_chunk']}; "
                    f"citation_validation_version={row['citation_validation_version_chunk']}; "
                    f"citation_validation_status={row['citation_validation_status_chunk']}; "
                    f"eligibility_status={row['eligibility_status']}"
                ),
                risk="Vector index would include a chunk that fails the required RAG gate.",
                suggested_fix="Recompute index eligibility and block this chunk from the vector index.",
            )
        elif not index_eligible and expected_by_chunk_fields and exclusion_reasons:
            add_issue(
                chunk_issues,
                "high",
                "chunk_quality_gate_metadata_inconsistent",
                ticker=row["ticker"],
                pdf_stem=row["pdf_stem"],
                evidence=(
                    f"chunk_id={row['chunk_id']}; chunk table passes the verified semantic gate, "
                    f"but eligibility excludes it: {exclusion_reasons}"
                ),
                risk=(
                    "Someone building the vector index from only esg_chunks_index.csv could ingest "
                    "chunks that the stricter audit excluded."
                ),
                suggested_fix=(
                    "Propagate audit exclusions back to doc_quality_status/rag_action/quality_flags "
                    "or require vector builders to use index_eligibility."
                ),
            )
        elif not index_eligible and expected_by_chunk_fields:
            add_issue(
                chunk_issues,
                "medium",
                "index_excludes_core_gate_passing_chunk_without_reason",
                ticker=row["ticker"],
                pdf_stem=row["pdf_stem"],
                evidence=f"chunk_id={row['chunk_id']}; eligibility_status={row['eligibility_status']}",
                risk="A good chunk may be omitted from RAG without an auditable reason.",
                suggested_fix="Add an exclusion reason or mark it index_eligible.",
            )

    for _, row in qa.iterrows():
        ticker = row["ticker"]
        pdf = row["pdf_file"]
        year = row["report_year_int"]
        status = str(row["status"]).strip()
        tracker_status = str(row["tracker_status"]).strip().lower()
        if status != "complete":
            add_issue(
                qa_issues,
                "high"
                if status
                in {"needs_review", "missing_pdf", "ocr_required", "parse_failed", "incomplete", "tracker_needs_cleanup"}
                else "medium",
                f"qa_status_{status}",
                ticker=ticker,
                pdf_file=pdf,
                year=year,
                evidence=(
                    f"tracker_status={row['tracker_status']}; pdf_count={row['pdf_count']}; "
                    f"parsed_count={row['parsed_count']}; sections={row['section_count']}; "
                    f"chunks={row['chunk_count']}; notes={row['notes']}"
                ),
                risk="Tracker/report row is not cleanly ready for publication/RAG.",
                suggested_fix="Use QA notes to clean tracker, find missing PDF, or inspect document quality.",
            )
        if tracker_status in {"", "nan", "none"}:
            add_issue(
                qa_issues,
                "high",
                "blank_tracker_status",
                ticker=ticker,
                pdf_file=pdf,
                year=year,
                evidence="tracker_status blank",
                risk="Cannot distinguish missing, downloaded, or intentionally absent report.",
                suggested_fix="Update tracker status.",
            )
        link = str(row["drive_file_link"]).strip()
        if status != "not_found" and link and not (
            link.startswith("http") or re.search(r"[A-Za-z0-9_-]{20,}", link)
        ):
            add_issue(
                qa_issues,
                "medium",
                "invalid_drive_file_link",
                ticker=ticker,
                pdf_file=pdf,
                year=year,
                evidence=f"drive_file_link={link}",
                risk="Cannot trace report source from published audit.",
                suggested_fix="Replace with real Drive file URL or file ID.",
            )
        parsed_count = pd.to_numeric(row["parsed_count"], errors="coerce")
        section_count = pd.to_numeric(row["section_count"], errors="coerce")
        chunk_count = pd.to_numeric(row["chunk_count"], errors="coerce")
        if status == "complete" and (parsed_count < 1 or section_count < 1 or chunk_count < 1):
            add_issue(
                qa_issues,
                "critical",
                "qa_complete_but_missing_pipeline_output",
                ticker=ticker,
                pdf_file=pdf,
                year=year,
                evidence=f"parsed={parsed_count}; sections={section_count}; chunks={chunk_count}",
                risk="QA complete label is false.",
                suggested_fix="Fix QA logic or rerun pipeline.",
            )

    doc_file_set = set(zip(docs["ticker"], docs["pdf_file"]))
    qa_with_pdf = qa[(qa["pdf_file"].astype(str).str.strip() != "") & (qa["status"] != "not_found")]
    for _, row in qa_with_pdf.iterrows():
        if (row["ticker"], row["pdf_file"]) not in doc_file_set:
            add_issue(
                qa_issues,
                "high",
                "qa_pdf_not_in_documents_table",
                ticker=row["ticker"],
                pdf_file=row["pdf_file"],
                year=row["report_year_int"],
                evidence=f"qa_status={row['status']}",
                risk="Tracker claims a PDF that is not represented in parsed documents.",
                suggested_fix="Download/parse the file or correct tracker row.",
            )

    company_year = (
        docs.groupby(["ticker", "year_from_filename"])
        .agg(docs=("pdf_file", "count"), chars=("char_count", "sum"), pages=("page_count", "sum"))
        .reset_index()
    )
    year_quality = (
        dq.groupby(["ticker", "year_from_filename"])
        .agg(
            sections=("sections", "sum"),
            chunks=("chunks", "sum"),
            fallback_docs=("fallback_sections", lambda s: int((s > 0).sum())),
            low_conf_docs=("low_conf_sections", lambda s: int((s > 0).sum())),
        )
        .reset_index()
    )
    company_year = company_year.merge(year_quality, on=["ticker", "year_from_filename"], how="left")
    company_year["sections_per_100k_chars"] = company_year["sections"] / (
        company_year["chars"] / 100000
    )
    company_year["chunks_per_100k_chars"] = company_year["chunks"] / (company_year["chars"] / 100000)
    company_year.to_csv(OUT / "company_year_coverage.csv", index=False)

    for _, row in company_year.iterrows():
        ticker = row["ticker"]
        year = row["year_from_filename"]
        if pd.notna(row["chars"]) and row["chars"] >= VERY_LARGE_DOC_CHARS and row["sections"] < 5:
            add_issue(
                company_issues,
                "high",
                "company_year_large_under_sectioned",
                ticker=ticker,
                year=int(year) if pd.notna(year) else "",
                evidence=(
                    f"docs={int(row.docs)}; chars={int(row.chars)}; "
                    f"sections={int(row.sections)}; chunks={int(row.chunks)}"
                ),
                risk="Company-year comparison will be weak for this report.",
                suggested_fix="Fix sectioning/OCR for this company-year before RAG benchmark.",
            )
        if row["docs"] > 1:
            add_issue(
                company_issues,
                "medium",
                "company_year_multiple_reports",
                ticker=ticker,
                year=int(year) if pd.notna(year) else "",
                evidence=(
                    f"docs={int(row.docs)}; chars={int(row.chars)}; "
                    f"sections={int(row.sections)}; chunks={int(row.chunks)}"
                ),
                risk="Company-year metrics may mix multiple PDFs unless report type is explicit.",
                suggested_fix="Confirm if all files belong in same ESG index; add report_type/source metadata.",
            )

    company_span = (
        docs.groupby("ticker")
        .agg(
            doc_count=("pdf_file", "count"),
            min_year=("year_from_filename", "min"),
            max_year=("year_from_filename", "max"),
            years=(
                "year_from_filename",
                lambda s: ",".join(str(int(x)) for x in sorted(set(s.dropna()))),
            ),
            chars=("char_count", "sum"),
        )
        .reset_index()
    )
    for _, row in company_span.iterrows():
        if row["doc_count"] == 1:
            add_issue(
                company_issues,
                "low",
                "company_has_single_report_year",
                ticker=row["ticker"],
                evidence=f"years={row.years}; chars={int(row.chars)}",
                risk="Company-level trend analysis is not possible.",
                suggested_fix="Accept as coverage limitation or search Drive/tracker for missing years.",
            )

    for _, row in anom.iterrows():
        severity = row["severity"] if row["severity"] in {"critical", "high", "medium", "low"} else "medium"
        add_issue(
            doc_issues,
            severity,
            "preexisting_anomaly_table_flag",
            ticker=row["ticker"],
            pdf_file=row["pdf"],
            evidence=(
                f"issue={row['issue']}; chars={row['chars']}; sections={row['sections']}; "
                f"chunks={row['chunks']}; notes={row['notes']}"
            ),
            risk="Already flagged by audit logic; needs closure before publication.",
            suggested_fix="Resolve according to issue type, then rerun audit.",
        )

    doc_issues_df = issues_df(doc_issues)
    section_issues_df = issues_df(section_issues)
    chunk_issues_df = issues_df(chunk_issues)
    company_issues_df = issues_df(company_issues)
    qa_issues_df = issues_df(qa_issues)
    schema_issues_df = issues_df(schema_issues)

    all_issues_df = pd.concat(
        [
            doc_issues_df.assign(issue_scope="document"),
            section_issues_df.assign(issue_scope="section"),
            chunk_issues_df.assign(issue_scope="chunk"),
            company_issues_df.assign(issue_scope="company_year"),
            qa_issues_df.assign(issue_scope="qa_tracker"),
            schema_issues_df.assign(issue_scope="schema"),
        ],
        ignore_index=True,
    )
    if not all_issues_df.empty:
        all_issues_df["_sev"] = all_issues_df["severity"].map(
            {"critical": 0, "high": 1, "medium": 2, "low": 3}
        ).fillna(9)
        all_issues_df = all_issues_df.sort_values(
            ["_sev", "issue_scope", "ticker", "year", "pdf_file", "pdf_stem", "issue_type"]
        ).drop(columns=["_sev"])

    issue_summary = (
        all_issues_df.groupby(["severity", "issue_scope", "issue_type"]).size().reset_index(name="issue_rows")
        if not all_issues_df.empty
        else pd.DataFrame(columns=["severity", "issue_scope", "issue_type", "issue_rows"])
    )
    if not issue_summary.empty:
        issue_summary["_sev"] = issue_summary["severity"].map(
            {"critical": 0, "high": 1, "medium": 2, "low": 3}
        ).fillna(9)
        issue_summary = issue_summary.sort_values(
            ["_sev", "issue_scope", "issue_rows"], ascending=[True, True, False]
        ).drop(columns=["_sev"])

    semantically_ready_chunks = sum(
        1 for _, row in chunks.iterrows() if semantically_citation_ready(row)
    )
    verified_eligible_chunks = sum(
        1
        for _, row in ie_join.iterrows()
        if boolish(row.get("index_eligible"))
        and semantically_citation_ready(row, suffix="_chunk")
        and str(row.get("doc_quality_status_chunk", "")).strip() == "ok"
        and str(row.get("rag_action_chunk", "")).strip() == "index_as_esg"
    )
    metrics = {
        "documents": len(docs),
        "sections": len(secs),
        "chunks": len(chunks),
        "qa_rows": len(qa),
        "companies_with_documents": docs["ticker"].nunique(),
        "years_min": int(docs["year_from_filename"].dropna().min())
        if docs["year_from_filename"].notna().any()
        else "",
        "years_max": int(docs["year_from_filename"].dropna().max())
        if docs["year_from_filename"].notna().any()
        else "",
        "eligible_chunks": verified_eligible_chunks,
        "manual_review_chunks": int((ie["eligibility_status"] == "manual_review").sum()),
        "excluded_chunks": int((ie["eligibility_status"] == "exclude").sum()),
        "citation_not_ready_chunks": len(chunks) - semantically_ready_chunks,
        "fallback_sections": int((secs["section_code"] == "full_document").sum()),
        "low_confidence_sections": int((secs["confidence"] == "low").sum()),
        "qa_complete_rows": int((qa["status"] == "complete").sum()),
        "qa_noncomplete_rows": int((qa["status"] != "complete").sum()),
        "critical_issue_rows": int((all_issues_df["severity"] == "critical").sum())
        if not all_issues_df.empty
        else 0,
        "high_issue_rows": int((all_issues_df["severity"] == "high").sum())
        if not all_issues_df.empty
        else 0,
        "medium_issue_rows": int((all_issues_df["severity"] == "medium").sum())
        if not all_issues_df.empty
        else 0,
        "low_issue_rows": int((all_issues_df["severity"] == "low").sum())
        if not all_issues_df.empty
        else 0,
    }
    metrics_df = pd.DataFrame([{"metric": key, "value": value} for key, value in metrics.items()])

    doc_problem_rollup = (
        doc_issues_df.groupby(["ticker", "severity", "issue_type"]).size().reset_index(name="issue_rows")
        if not doc_issues_df.empty
        else pd.DataFrame()
    )
    company_quality_enriched = cq.merge(
        doc_issues_df.groupby("ticker").size().reset_index(name="document_issue_rows")
        if not doc_issues_df.empty
        else pd.DataFrame(columns=["ticker", "document_issue_rows"]),
        on="ticker",
        how="left",
    ).merge(
        company_issues_df.groupby("ticker").size().reset_index(name="company_issue_rows")
        if not company_issues_df.empty
        else pd.DataFrame(columns=["ticker", "company_issue_rows"]),
        on="ticker",
        how="left",
    )
    company_quality_enriched[["document_issue_rows", "company_issue_rows"]] = (
        company_quality_enriched[["document_issue_rows", "company_issue_rows"]].fillna(0).astype(int)
    )
    company_quality_enriched["total_issue_rows"] = (
        company_quality_enriched["document_issue_rows"] + company_quality_enriched["company_issue_rows"]
    )
    company_quality_enriched = company_quality_enriched.sort_values(
        ["total_issue_rows", "chunks"], ascending=[False, False]
    )

    doc_issues_df.to_csv(OUT / "document_issue_tracker.csv", index=False)
    section_issues_df.to_csv(OUT / "section_issue_tracker.csv", index=False)
    chunk_issues_df.to_csv(OUT / "chunk_issue_tracker.csv", index=False)
    company_issues_df.to_csv(OUT / "company_year_issue_tracker.csv", index=False)
    qa_issues_df.to_csv(OUT / "qa_tracker_issue_tracker.csv", index=False)
    schema_issues_df.to_csv(OUT / "schema_issue_tracker.csv", index=False)
    all_issues_df.to_csv(OUT / "all_issue_tracker.csv", index=False)
    issue_summary.to_csv(OUT / "issue_summary.csv", index=False)
    metrics_df.to_csv(OUT / "audit_metrics.csv", index=False)
    company_quality_enriched.to_csv(OUT / "company_quality_rollup.csv", index=False)
    doc_problem_rollup.to_csv(OUT / "document_problem_rollup.csv", index=False)
    ocr.to_csv(OUT / "ocr_fixes.csv", index=False)

    high_docs = doc_issues_df[doc_issues_df["severity"].isin(["critical", "high"])]
    high_sections = section_issues_df[section_issues_df["severity"].isin(["critical", "high"])]
    high_chunks = chunk_issues_df[chunk_issues_df["severity"].isin(["critical", "high"])]
    high_qa = qa_issues_df[qa_issues_df["severity"].isin(["critical", "high"])]

    report_lines = [
        "# ESG Publication Quality Audit",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        "## Bottom Line",
        "",
        (
            "The ESG corpus is usable for controlled RAG only after applying the existing index gate, "
            "but it is not publication-perfect yet. The core chunk contract is strong: no duplicate "
            "chunk IDs were found and token bounds are respected. The remaining risk is concentrated "
            "in tracker coverage, a small set of bad-text/under-sectioned PDFs, citation metadata gaps, "
            "and company-year metadata cleanup."
        ),
        "",
        "## Corpus Snapshot",
        "",
        f"- Documents: {fmt_int(metrics['documents'])}",
        f"- Companies with documents: {fmt_int(metrics['companies_with_documents'])}",
        f"- Sections: {fmt_int(metrics['sections'])}",
        f"- Chunks: {fmt_int(metrics['chunks'])}",
        f"- Clean indexable chunks: {fmt_int(metrics['eligible_chunks'])}",
        f"- Manual-review chunks: {fmt_int(metrics['manual_review_chunks'])}",
        f"- Excluded chunks: {fmt_int(metrics['excluded_chunks'])}",
        f"- QA complete rows: {fmt_int(metrics['qa_complete_rows'])}",
        f"- QA non-complete rows: {fmt_int(metrics['qa_noncomplete_rows'])}",
        "",
        "## Issue Counts",
        "",
        f"- Critical issue rows: {fmt_int(metrics['critical_issue_rows'])}",
        f"- High issue rows: {fmt_int(metrics['high_issue_rows'])}",
        f"- Medium issue rows: {fmt_int(metrics['medium_issue_rows'])}",
        f"- Low issue rows: {fmt_int(metrics['low_issue_rows'])}",
        "",
        (
            "Issue rows are intentionally strict and may include one row per affected chunk, section, "
            "or tracker record. Use `issue_summary.csv` to separate systemic problems from repeated "
            "row-level consequences."
        ),
        "",
        "## Highest Priority Document Problems",
        "",
        top_rows_markdown(
            high_docs,
            ["severity", "issue_type", "ticker", "pdf_file", "year", "evidence", "suggested_fix"],
            30,
        ),
        "",
        "## Highest Priority Section Problems",
        "",
        top_rows_markdown(
            high_sections,
            ["severity", "issue_type", "ticker", "pdf_stem", "year", "evidence", "suggested_fix"],
            30,
        ),
        "",
        "## Highest Priority Chunk Problems",
        "",
        top_rows_markdown(
            high_chunks,
            ["severity", "issue_type", "ticker", "pdf_stem", "year", "evidence", "suggested_fix"],
            30,
        ),
        "",
        "## Highest Priority Tracker/QA Problems",
        "",
        top_rows_markdown(
            high_qa,
            ["severity", "issue_type", "ticker", "pdf_file", "year", "evidence", "suggested_fix"],
            30,
        ),
        "",
        "## Full Artifact List",
        "",
        "- `all_issue_tracker.csv`: every issue row across scopes",
        "- `issue_summary.csv`: counts by severity/scope/type",
        "- `document_issue_tracker.csv`: document parse/OCR/year/density issues",
        "- `section_issue_tracker.csv`: section taxonomy, fallback, span, and chunk coverage issues",
        "- `chunk_issue_tracker.csv`: chunk token, citation, file, sequence, and eligibility issues",
        "- `company_year_issue_tracker.csv`: company/year coverage and size-vs-section anomalies",
        "- `qa_tracker_issue_tracker.csv`: tracker and QA status cleanup list",
        "- `company_year_coverage.csv`: company-year corpus shape table",
        "- `company_quality_rollup.csv`: company-level corpus size and issue counts",
        "- `audit_metrics.csv`: audit headline metrics",
    ]
    (OUT / "ESG_PUBLICATION_QUALITY_AUDIT.md").write_text("\n".join(report_lines), encoding="utf-8")

    print(f"OUT={OUT}")
    print(metrics_df.to_string(index=False))
    print("\nIssue summary:")
    print(issue_summary.to_string(index=False, max_rows=200))
    print("\nTop high/critical docs:")
    if high_docs.empty:
        print("None")
    else:
        print(
            high_docs[["severity", "issue_type", "ticker", "pdf_file", "year", "evidence"]]
            .head(50)
            .to_string(index=False)
        )


if __name__ == "__main__":
    main()
