from __future__ import annotations

import argparse
import csv
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


REFERENCE_DIR = Path("data/00_reference")
RAW_ESG_ROOT = Path("data/01_raw/sustainability")
VALID_TRACKER_STATUSES = {"downloaded", "not_found"}
VALID_PARSE_STATUSES = {"parsed", "ocr_required", "failed"}
MIN_CHUNK_TOKENS = 100
MAX_CHUNK_TOKENS = 600
SHORT_EVIDENCE_MIN_TOKENS = 25
CHUNK_TYPE_NORMAL = "normal"
CHUNK_TYPE_SHORT_EVIDENCE = "short_evidence"
IDENTITY_ID_RE = re.compile(r"^(?:ls|sv|fa|ea)_[0-9a-f]{24}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

(
    Company,
    SustainabilityReport,
    LogicalSource,
    SourceVersion,
    FileAlias,
    ExtractionArtifact,
    SourceApproval,
    Document,
    Section,
    Chunk,
) = (None,) * 10


@dataclass
class LoadPlan:
    companies: dict[str, dict] = field(default_factory=dict)
    reports: list[dict] = field(default_factory=list)
    logical_sources: list[dict] = field(default_factory=list)
    source_versions: list[dict] = field(default_factory=list)
    file_aliases: list[dict] = field(default_factory=list)
    extraction_artifacts: list[dict] = field(default_factory=list)
    source_approvals: list[dict] = field(default_factory=list)
    documents: list[dict] = field(default_factory=list)
    sections: list[dict] = field(default_factory=list)
    chunks: list[dict] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)


@dataclass
class IdentityLookup:
    original_by_path: dict[str, dict] = field(default_factory=dict)
    original_by_sha256: dict[str, dict] = field(default_factory=dict)
    artifacts_by_id: dict[str, dict] = field(default_factory=dict)
    artifacts_by_version_hash: dict[tuple[str, str], dict] = field(default_factory=dict)


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def resolve_path(raw_path: str | None) -> Path | None:
    if not raw_path:
        return None
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return Path.cwd() / path


def display_path(path: str | Path) -> str:
    path = Path(path)
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def normalize_status(status: str | None) -> str:
    raw = (status or "").strip().lower().replace("-", "_").replace(" ", "_")
    if raw == "downloaded":
        return "downloaded"
    if raw in {"not_found", "notfound"}:
        return "not_found"
    return raw


def parse_int(value: str | int | None) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return (value or "").strip().lower() in {"true", "1", "yes", "y"}


def valid_chunk_token_count(row: dict) -> bool:
    token_count = parse_int(row.get("token_count"))
    chunk_type = (row.get("chunk_type") or CHUNK_TYPE_NORMAL).strip()
    if token_count is None:
        return False
    if chunk_type == CHUNK_TYPE_SHORT_EVIDENCE:
        return SHORT_EVIDENCE_MIN_TOKENS <= token_count < MIN_CHUNK_TOKENS
    return MIN_CHUNK_TOKENS <= token_count <= MAX_CHUNK_TOKENS


def row_quality_flags(row: dict) -> set[str]:
    raw_flags = (row.get("quality_flags") or "").strip()
    return {flag for flag in raw_flags.split("|") if flag}


def doc_quality_status_for_parse_row(row: dict) -> str:
    flags = row_quality_flags(row)
    if parse_bool(row.get("possible_wrong_doc_type")):
        return "exclude_from_esg_rag"
    if row.get("status") != "parsed" or flags & {
        "garbled_text",
        "low_readable_word_ratio",
        "low_text_per_page",
    }:
        return "needs_review"
    return "ok"


def rag_action_for_quality_status(status: str) -> str:
    if status == "exclude_from_esg_rag":
        return "exclude_from_esg_index"
    if status == "needs_review":
        return "manual_review_before_indexing"
    return "index_as_esg"


def doc_type_for_parse_row(row: dict) -> str:
    if parse_bool(row.get("possible_wrong_doc_type")):
        return "annual_report_with_esg"
    return "sustainability"


def extract_years(raw_year: str | None) -> list[int]:
    years = sorted({int(y) for y in re.findall(r"\b(?:19|20)\d{2}\b", raw_year or "")}, reverse=True)
    return years


def local_pdfs_by_ticker(raw_root: Path = RAW_ESG_ROOT) -> dict[str, list[Path]]:
    pdfs: dict[str, list[Path]] = defaultdict(list)
    if not raw_root.exists():
        return pdfs
    for pdf_file in raw_root.glob("*/*.pdf"):
        pdfs[pdf_file.parent.name.upper()].append(pdf_file)
    return pdfs


def is_bad_drive_link(value: str | None, ticker: str) -> bool:
    raw = (value or "").strip()
    if not raw:
        return True
    if raw.upper() == ticker.upper():
        return True
    if raw.startswith(("http://", "https://")):
        return False
    return not bool(re.fullmatch(r"[A-Za-z0-9_-]{20,}", raw))


def load_company_map(companies_path: Path) -> dict[str, dict]:
    companies: dict[str, dict] = {}
    for row in read_csv(companies_path):
        ticker = (row.get("ticker") or "").strip().upper()
        if ticker:
            companies[ticker] = row
    return companies


def normalize_identity_path(raw_path: str | None) -> str:
    raw = (raw_path or "").strip().replace("\\", "/")
    while "//" in raw:
        raw = raw.replace("//", "/")
    return raw.casefold()


def normalized_sha256(value: str | None) -> str | None:
    digest = (value or "").strip().lower()
    return digest if SHA256_RE.fullmatch(digest) else None


def parse_timestamp(value: str | None) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _put_identity(
    target: dict[str, dict],
    identity_id: str,
    row: dict,
    label: str,
    anomalies: list[str],
) -> None:
    previous = target.get(identity_id)
    if previous is None:
        target[identity_id] = row
    elif previous != row:
        anomalies.append(f"{label} {identity_id}: conflicting catalog rows")


def build_identity_plan(
    catalog_rows: list[dict],
    approval_rows: list[dict],
    companies: dict[str, dict],
    plan: LoadPlan,
) -> IdentityLookup:
    logical_sources: dict[str, dict] = {}
    source_versions: dict[str, dict] = {}
    file_aliases: dict[str, dict] = {}
    artifacts: dict[str, dict] = {}
    lookup = IdentityLookup()

    for row in catalog_rows:
        logical_source_id = (row.get("logical_source_id") or "").strip()
        source_version_id = (row.get("source_version_id") or "").strip()
        file_alias_id = (row.get("file_alias_id") or "").strip()
        extraction_artifact_id = (row.get("extraction_artifact_id") or "").strip()
        ids = {
            "logical_source_id": logical_source_id,
            "source_version_id": source_version_id,
            "file_alias_id": file_alias_id,
            "extraction_artifact_id": extraction_artifact_id,
        }
        invalid = [name for name, value in ids.items() if not IDENTITY_ID_RE.fullmatch(value)]
        if invalid:
            plan.anomalies.append(
                "catalog row has missing or invalid identity IDs "
                f"({', '.join(invalid)}): {row.get('file_path') or row.get('pdf_file') or row}"
            )
            continue

        artifact_sha256 = normalized_sha256(row.get("sha256") or row.get("artifact_sha256"))
        if artifact_sha256 is None:
            plan.anomalies.append(f"{extraction_artifact_id}: missing or invalid artifact sha256")
            continue
        role = (row.get("artifact_role") or "").strip().lower()
        if role == "raw":
            role = "original"
        if role not in {
            "original",
            "ocr_derivative",
            "page_ocr_override",
            "vlm_derivative",
            "parsed_text",
        }:
            plan.anomalies.append(f"{extraction_artifact_id}: invalid artifact_role '{role}'")
            continue

        canonical_ticker = (
            row.get("canonical_ticker") or row.get("observed_ticker") or ""
        ).strip().upper()
        company = companies.get(canonical_ticker)
        lifecycle_state = "active" if parse_bool(row.get("active")) else "superseded"
        logical_row = {
            "logical_source_id": logical_source_id,
            "ticker": canonical_ticker,
            "company_id_csv": parse_int(company.get("company_id")) if company else None,
            "policy_source_id": (row.get("policy_source_id") or row.get("source_id") or "").strip() or None,
            "source_type": (row.get("source_type") or "sustainability").strip(),
            "report_year": parse_int(row.get("report_year")),
            "title": (row.get("title") or row.get("pdf_file") or "").strip() or None,
            "lifecycle_state": lifecycle_state,
            "ownership_review_required": parse_bool(row.get("ownership_review_required")),
        }
        _put_identity(logical_sources, logical_source_id, logical_row, "logical source", plan.anomalies)

        version_row = source_versions.get(source_version_id)
        if version_row is None:
            version_row = {
                "source_version_id": source_version_id,
                "logical_source_id": logical_source_id,
                "original_sha256": None,
                "byte_size": parse_int(row.get("size_bytes") or row.get("source_size_bytes")),
                "media_type": (row.get("media_type") or "application/pdf").strip() or None,
                "lifecycle_state": lifecycle_state,
                "ownership_review_required": parse_bool(row.get("ownership_review_required")),
            }
            source_versions[source_version_id] = version_row
        elif version_row["logical_source_id"] != logical_source_id:
            plan.anomalies.append(
                f"{source_version_id}: mapped to more than one logical_source_id"
            )
            continue
        if role == "original":
            old_hash = version_row.get("original_sha256")
            if old_hash and old_hash != artifact_sha256:
                plan.anomalies.append(f"{source_version_id}: conflicting original hashes")
                continue
            version_row["original_sha256"] = artifact_sha256

        artifact_row = {
            "extraction_artifact_id": extraction_artifact_id,
            "source_version_id": source_version_id,
            "artifact_role": role,
            "artifact_sha256": artifact_sha256,
            "storage_path": (row.get("file_path") or "").strip() or None,
            "drive_file_id": (row.get("drive_id") or "").strip() or None,
            "parser_or_model": (row.get("parser_or_model") or row.get("model") or "").strip() or None,
            "prompt_version": (row.get("prompt_version") or "").strip() or None,
            "source_page_sha256": normalized_sha256(row.get("source_page_sha256")),
            "verification_state": (row.get("verification_state") or "unverified").strip(),
            "lifecycle_state": lifecycle_state,
        }
        _put_identity(artifacts, extraction_artifact_id, artifact_row, "artifact", plan.anomalies)
        lookup.artifacts_by_id[extraction_artifact_id] = artifact_row
        lookup.artifacts_by_version_hash[(source_version_id, artifact_sha256)] = artifact_row

        alias_row = {
            "file_alias_id": file_alias_id,
            "source_version_id": source_version_id,
            "extraction_artifact_id": extraction_artifact_id,
            "observed_ticker": (row.get("observed_ticker") or canonical_ticker).strip().upper(),
            "file_path": (row.get("file_path") or "").strip() or None,
            "drive_file_id": (row.get("drive_id") or "").strip() or None,
            "observed_filename": (row.get("pdf_file") or "").strip() or None,
            "lifecycle_state": lifecycle_state,
        }
        _put_identity(file_aliases, file_alias_id, alias_row, "file alias", plan.anomalies)

        if role == "original":
            identity = {
                "logical_source_id": logical_source_id,
                "source_version_id": source_version_id,
                "file_alias_id": file_alias_id,
                "extraction_artifact_id": extraction_artifact_id,
            }
            path_key = normalize_identity_path(row.get("file_path"))
            if path_key:
                lookup.original_by_path[path_key] = identity
            lookup.original_by_sha256[artifact_sha256] = identity

    for version in source_versions.values():
        if version["original_sha256"] is None:
            plan.anomalies.append(
                f"{version['source_version_id']}: catalog has no original artifact hash"
            )

    approvals: list[dict] = []
    for row in approval_rows:
        logical_source_id = (
            row.get("logical_source_id") or row.get("original_logical_source_id") or ""
        ).strip()
        source_version_id = (
            row.get("source_version_id") or row.get("original_source_version_id") or ""
        ).strip()
        extraction_artifact_id = (
            row.get("extraction_artifact_id") or row.get("ocr_extraction_artifact_id") or ""
        ).strip()
        source_hash = normalized_sha256(
            row.get("original_sha256") or row.get("original_source_sha256")
        )
        artifact_hash = normalized_sha256(
            row.get("ocr_sha256") or row.get("artifact_sha256")
        )
        if (
            logical_source_id not in logical_sources
            or source_version_id not in source_versions
            or extraction_artifact_id not in artifacts
            or source_hash is None
            or artifact_hash is None
        ):
            plan.anomalies.append(
                "approval row does not match a complete catalog identity: "
                f"{source_version_id or '<missing source version>'}"
            )
            continue
        approvals.append(
            {
                "logical_source_id": logical_source_id,
                "source_version_id": source_version_id,
                "extraction_artifact_id": extraction_artifact_id,
                "approval_type": (row.get("approval_type") or "ocr_replacement").strip(),
                "approval_status": (row.get("approval_status") or row.get("status") or "pending").strip().lower(),
                "approved_source_sha256": source_hash,
                "approved_artifact_sha256": artifact_hash,
                "reviewer": (row.get("reviewer") or "").strip() or None,
                "approval_date": parse_timestamp(row.get("approval_date")),
                "reason": (row.get("reason") or "").strip() or None,
                "lifecycle_state": (row.get("lifecycle_state") or row.get("state") or "active").strip().lower(),
            }
        )

    plan.logical_sources = list(logical_sources.values())
    plan.source_versions = list(source_versions.values())
    plan.file_aliases = list(file_aliases.values())
    plan.extraction_artifacts = list(artifacts.values())
    plan.source_approvals = approvals
    return lookup


def lineage_for_parse_row(row: dict, lookup: IdentityLookup) -> dict[str, str | None]:
    direct = {
        "logical_source_id": (row.get("logical_source_id") or "").strip() or None,
        "source_version_id": (row.get("source_version_id") or "").strip() or None,
        "file_alias_id": (row.get("file_alias_id") or "").strip() or None,
        "extraction_artifact_id": (row.get("extraction_artifact_id") or "").strip() or None,
    }
    if all(direct.values()):
        return direct

    source_path = normalize_identity_path(row.get("source_pdf") or row.get("filepath"))
    source_hash = normalized_sha256(row.get("source_sha256"))
    identity = lookup.original_by_path.get(source_path)
    if identity is None and source_hash:
        identity = lookup.original_by_sha256.get(source_hash)
    if identity is None:
        return direct

    selected_artifact_id = direct["extraction_artifact_id"]
    parse_hash = normalized_sha256(row.get("parse_source_sha256")) or source_hash
    if selected_artifact_id is None and parse_hash:
        artifact = lookup.artifacts_by_version_hash.get(
            (identity["source_version_id"], parse_hash)
        )
        if artifact:
            selected_artifact_id = artifact["extraction_artifact_id"]
    return {
        "logical_source_id": direct["logical_source_id"] or identity["logical_source_id"],
        "source_version_id": direct["source_version_id"] or identity["source_version_id"],
        "file_alias_id": direct["file_alias_id"] or identity["file_alias_id"],
        "extraction_artifact_id": selected_artifact_id or identity["extraction_artifact_id"],
    }


def choose_report_years(tracker: dict, local_pdfs: list[Path], anomalies: list[str]) -> list[int | None]:
    ticker = (tracker.get("ticker") or "").strip().upper()
    years = extract_years(tracker.get("report_year"))
    if not years:
        return [None]
    if len(years) == 1:
        return [years[0]]

    filename_years = {
        year
        for pdf_file in local_pdfs
        for year in extract_years(pdf_file.name)
        if year in years
    }
    if len(filename_years) > 1 and len(local_pdfs) >= len(filename_years):
        return sorted(filename_years, reverse=True)

    anomalies.append(
        f"{ticker}: tracker report_year has multiple years ({tracker.get('report_year')}); "
        f"using latest year {years[0]} for DB metadata"
    )
    return [years[0]]


def build_reports(tracker_rows: list[dict], companies: dict[str, dict], local_pdfs: dict[str, list[Path]], anomalies: list[str]) -> list[dict]:
    reports: list[dict] = []
    seen: set[tuple[str, int | None]] = set()

    for row in tracker_rows:
        ticker = (row.get("ticker") or "").strip().upper()
        if not ticker:
            anomalies.append("tracker row missing ticker")
            continue

        company = companies.get(ticker)
        if company is None:
            anomalies.append(f"{ticker}: tracker ticker missing from companies.csv")
            continue

        status = normalize_status(row.get("status"))
        if status not in VALID_TRACKER_STATUSES:
            anomalies.append(f"{ticker}: blank or invalid tracker status '{row.get('status', '')}'")
            continue

        if status == "downloaded" and not local_pdfs.get(ticker):
            anomalies.append(f"{ticker}: tracker says downloaded but no local PDF exists")

        if status == "downloaded" and not (row.get("format") or "").strip():
            anomalies.append(f"{ticker}: downloaded tracker row has blank format")

        if status == "downloaded" and is_bad_drive_link(row.get("drive_file_link"), ticker):
            anomalies.append(f"{ticker}: drive_file_link is blank or not a usable URL/file ID")

        for year in choose_report_years(row, local_pdfs.get(ticker, []), anomalies):
            key = (ticker, year)
            if key in seen:
                continue
            seen.add(key)
            reports.append(
                {
                    "ticker": ticker,
                    "company_id_csv": parse_int(company.get("company_id")),
                    "year": year,
                    "report_url": (row.get("drive_file_link") or "").strip() or None,
                    "format": (row.get("format") or "").strip() or None,
                    "download_status": status,
                }
            )

    return reports


def parse_doc_key(row: dict) -> tuple[str, str] | None:
    ticker = (row.get("ticker") or "").strip().upper()
    source_pdf = row.get("source_pdf") or row.get("filepath") or ""
    if not ticker or not source_pdf:
        return None
    return ticker, Path(source_pdf).stem


def build_documents(
    parse_rows: list[dict],
    companies: dict[str, dict],
    local_pdfs: dict[str, list[Path]],
    anomalies: list[str],
    identity_lookup: IdentityLookup | None = None,
) -> list[dict]:
    documents: dict[tuple[str, str], dict] = {}

    for row in parse_rows:
        key = parse_doc_key(row)
        if key is None:
            anomalies.append(f"parse index row missing ticker/source_pdf: {row}")
            continue

        ticker, pdf_stem = key
        if ticker not in companies:
            anomalies.append(f"{ticker}: parse index ticker missing from companies.csv")
            continue

        status = row.get("status") or ""
        if status not in VALID_PARSE_STATUSES:
            anomalies.append(f"{ticker} {pdf_stem}: invalid parse status '{status}'")
            status = "failed"

        source_pdf = row.get("source_pdf") or ""
        if source_pdf and not (resolve_path(source_pdf) or Path()).exists():
            anomalies.append(f"{ticker} {pdf_stem}: source_pdf missing locally: {source_pdf}")

        lineage = lineage_for_parse_row(row, identity_lookup or IdentityLookup())
        documents[key] = {
            "ticker": ticker,
            "pdf_stem": pdf_stem,
            "company_id_csv": parse_int(companies[ticker].get("company_id")),
            "doc_type": doc_type_for_parse_row(row),
            "filepath": source_pdf,
            "parse_status": status,
            "quality_flags": row.get("quality_flags") or "",
            "possible_wrong_doc_type": parse_bool(row.get("possible_wrong_doc_type")),
            "doc_quality_status": doc_quality_status_for_parse_row(row),
            "rag_action": rag_action_for_quality_status(doc_quality_status_for_parse_row(row)),
            **lineage,
            "lifecycle_state": (row.get("lifecycle_state") or "active").strip().lower(),
        }

    for ticker, pdf_files in local_pdfs.items():
        if ticker not in companies:
            anomalies.append(f"{ticker}: local PDF ticker missing from companies.csv")
            continue
        for pdf_file in pdf_files:
            key = (ticker, pdf_file.stem)
            documents.setdefault(
                key,
                {
                    "ticker": ticker,
                    "pdf_stem": pdf_file.stem,
                    "company_id_csv": parse_int(companies[ticker].get("company_id")),
                    "doc_type": "sustainability",
                    "filepath": display_path(pdf_file),
                    "parse_status": "not_started",
                    "quality_flags": "",
                    "possible_wrong_doc_type": False,
                    "doc_quality_status": "needs_review",
                    "rag_action": "manual_review_before_indexing",
                    "logical_source_id": None,
                    "source_version_id": None,
                    "file_alias_id": None,
                    "extraction_artifact_id": None,
                    "lifecycle_state": "active",
                },
            )

    return list(documents.values())


def build_sections(section_rows: list[dict], documents: list[dict], anomalies: list[str]) -> list[dict]:
    doc_map = {(doc["ticker"], doc["pdf_stem"]): doc for doc in documents}
    doc_keys = set(doc_map)
    sections: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    section_code_counts = Counter(
        (
            (row.get("ticker") or "").strip().upper(),
            (row.get("pdf_stem") or "").strip(),
            (row.get("section_code") or "").strip(),
        )
        for row in section_rows
    )

    for row in section_rows:
        ticker = (row.get("ticker") or "").strip().upper()
        pdf_stem = (row.get("pdf_stem") or "").strip()
        section_code = (row.get("section_code") or "").strip()
        section_instance_id = (row.get("section_instance_id") or "").strip()

        if not ticker or not pdf_stem or not section_code:
            anomalies.append(f"section index row missing ticker/pdf_stem/section_code: {row}")
            continue
        if not section_instance_id:
            legacy_key = (ticker, pdf_stem, section_code)
            if section_code_counts[legacy_key] != 1:
                anomalies.append(
                    f"{ticker} {pdf_stem} {section_code}: legacy section_code is not a "
                    "safe section_instance_id because it occurs more than once"
                )
                continue
            section_instance_id = section_code

        key = (ticker, pdf_stem, section_instance_id)
        if (ticker, pdf_stem) not in doc_keys:
            anomalies.append(f"{ticker} {pdf_stem}: section has no matching document")
            continue
        if key in seen:
            anomalies.append(
                f"{ticker} {pdf_stem} {section_instance_id}: duplicate section instance row"
            )
            continue

        section_file = row.get("section_file") or ""
        path = resolve_path(section_file)
        if path is None or not path.exists():
            anomalies.append(f"{ticker} {pdf_stem} {section_code}: section file missing: {section_file}")
            continue

        text = path.read_text(encoding="utf-8", errors="replace")
        seen.add(key)
        document = doc_map[(ticker, pdf_stem)]
        sections.append(
            {
                "ticker": ticker,
                "pdf_stem": pdf_stem,
                "section_instance_id": section_instance_id,
                "section_code": section_code,
                "section_title": row.get("section_title") or section_code.replace("_", " ").title(),
                "section_text": text,
                "char_count": len(text),
                "source_start_char": parse_int(row.get("source_start_char")),
                "source_end_char": parse_int(row.get("source_end_char")),
                "page_start": parse_int(row.get("page_start")),
                "page_end": parse_int(row.get("page_end")),
                "logical_source_id": document.get("logical_source_id"),
                "source_version_id": document.get("source_version_id"),
                "extraction_artifact_id": document.get("extraction_artifact_id"),
                "lifecycle_state": (row.get("lifecycle_state") or "active").strip().lower(),
            }
        )

    return sections


def build_chunks(chunk_rows: list[dict], sections: list[dict], anomalies: list[str]) -> list[dict]:
    section_keys = {
        (s["ticker"], s["pdf_stem"], s["section_instance_id"])
        for s in sections
    }
    section_map = {
        (s["ticker"], s["pdf_stem"], s["section_instance_id"]): s
        for s in sections
    }
    section_instances_by_code: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for section in sections:
        section_instances_by_code[
            (section["ticker"], section["pdf_stem"], section["section_code"])
        ].append(section["section_instance_id"])

    chunks: list[dict] = []
    seen: set[tuple[str, str, str, int]] = set()
    seen_external_chunk_ids: set[str] = set()

    for row in chunk_rows:
        ticker = (row.get("ticker") or "").strip().upper()
        pdf_stem = (row.get("pdf_stem") or "").strip()
        section_code = (row.get("section_code") or "").strip()
        section_instance_id = (row.get("section_instance_id") or "").strip()
        chunk_index = parse_int(row.get("chunk_index"))

        if not ticker or not pdf_stem or not section_code or chunk_index is None:
            anomalies.append(f"chunk index row missing ticker/pdf_stem/section_code/chunk_index: {row}")
            continue

        if not section_instance_id:
            candidates = section_instances_by_code.get(
                (ticker, pdf_stem, section_code), []
            )
            if len(candidates) != 1:
                anomalies.append(
                    f"{ticker} {pdf_stem} {section_code} chunk {chunk_index}: legacy "
                    "section_code cannot identify exactly one section instance"
                )
                continue
            section_instance_id = candidates[0]

        key = (ticker, pdf_stem, section_instance_id, chunk_index)
        section_key = (ticker, pdf_stem, section_instance_id)
        if section_key not in section_keys:
            anomalies.append(
                f"{ticker} {pdf_stem} {section_instance_id}: chunk has no matching section instance"
            )
            continue
        if key in seen:
            anomalies.append(
                f"{ticker} {pdf_stem} {section_instance_id} chunk {chunk_index}: duplicate chunk row"
            )
            continue

        external_chunk_id = (
            row.get("external_chunk_id") or row.get("chunk_id") or ""
        ).strip() or None
        if external_chunk_id and external_chunk_id in seen_external_chunk_ids:
            anomalies.append(f"{external_chunk_id}: duplicate external chunk ID")
            continue

        token_count = parse_int(row.get("token_count"))
        if not valid_chunk_token_count(row):
            anomalies.append(f"{ticker} {pdf_stem} {section_code} chunk {chunk_index}: invalid token_count {row.get('token_count')}")
            continue

        chunk_file = row.get("chunk_file") or ""
        path = resolve_path(chunk_file)
        if path is None or not path.exists():
            anomalies.append(f"{ticker} {pdf_stem} {section_code} chunk {chunk_index}: chunk file missing: {chunk_file}")
            continue

        seen.add(key)
        if external_chunk_id:
            seen_external_chunk_ids.add(external_chunk_id)

        section = section_map[section_key]
        old_source_version_id = (row.get("source_version_id") or "").strip() or None
        new_source_version_id = old_source_version_id if old_source_version_id and old_source_version_id.startswith("sv_") else section.get("source_version_id")
        chunks.append(
            {
                "ticker": ticker,
                "pdf_stem": pdf_stem,
                "section_instance_id": section_instance_id,
                "section_code": section_code,
                "chunk_index": chunk_index,
                "external_chunk_id": external_chunk_id,
                "chunk_text": path.read_text(encoding="utf-8", errors="replace"),
                "token_count": token_count,
                "doc_type": row.get("doc_type") or "sustainability",
                "source_id": (row.get("source_id") or "").strip() or None,
                "source_version_id": new_source_version_id,
                "legacy_source_version_id": old_source_version_id if old_source_version_id != new_source_version_id else None,
                "logical_source_id": (row.get("logical_source_id") or "").strip() or section.get("logical_source_id"),
                "extraction_artifact_id": (row.get("extraction_artifact_id") or "").strip() or section.get("extraction_artifact_id"),
                "lifecycle_state": (row.get("lifecycle_state") or "active").strip().lower(),
                "chunk_type": (row.get("chunk_type") or CHUNK_TYPE_NORMAL).strip(),
                "short_section_action": (
                    row.get("short_section_action") or ""
                ).strip() or None,
                "short_section_reason": (
                    row.get("short_section_reason") or ""
                ).strip() or None,
                "merged_section_ids": (
                    row.get("merged_section_ids") or ""
                ).strip() or None,
                "doc_quality_status": row.get("doc_quality_status") or "needs_review",
                "rag_action": row.get("rag_action") or "manual_review_before_indexing",
                "quality_flags": row.get("quality_flags") or "",
                "source_start_char": parse_int(row.get("source_start_char")),
                "source_end_char": parse_int(row.get("source_end_char")),
                "page_start": parse_int(row.get("page_start")),
                "page_end": parse_int(row.get("page_end")),
                "citation_ready": parse_bool(row.get("citation_ready")),
                "citation_validation_status": (
                    row.get("citation_validation_status") or ""
                ).strip() or None,
                "citation_validation_version": (
                    row.get("citation_validation_version") or ""
                ).strip() or None,
            }
        )

    return chunks


def build_plan(args) -> LoadPlan:
    plan = LoadPlan()
    tracker_rows = read_csv(Path(args.tracker))
    parse_rows = read_csv(Path(args.parse_index))
    section_rows = read_csv(Path(args.sections_index))
    chunk_rows = read_csv(Path(args.chunks_index))
    catalog_rows = read_csv(Path(getattr(args, "file_catalog", REFERENCE_DIR / "esg_file_catalog.csv")))
    approval_rows = read_csv(Path(getattr(args, "ocr_approvals", REFERENCE_DIR / "esg_ocr_approval.csv")))
    plan.companies = load_company_map(Path(args.companies))
    local_pdfs = local_pdfs_by_ticker(Path(args.raw_root))
    identity_lookup = build_identity_plan(catalog_rows, approval_rows, plan.companies, plan)

    plan.reports = build_reports(tracker_rows, plan.companies, local_pdfs, plan.anomalies)
    plan.documents = build_documents(parse_rows, plan.companies, local_pdfs, plan.anomalies, identity_lookup)
    plan.sections = build_sections(section_rows, plan.documents, plan.anomalies)
    plan.chunks = build_chunks(chunk_rows, plan.sections, plan.anomalies)
    return plan


def print_plan(plan: LoadPlan) -> None:
    print("ESG DB load plan:")
    print(f"  sustainability_reports upsert candidates: {len(plan.reports)}")
    print(f"  logical_sources upsert candidates: {len(plan.logical_sources)}")
    print(f"  source_versions upsert candidates: {len(plan.source_versions)}")
    print(f"  file_aliases upsert candidates: {len(plan.file_aliases)}")
    print(f"  extraction_artifacts upsert candidates: {len(plan.extraction_artifacts)}")
    print(f"  source_approvals upsert candidates: {len(plan.source_approvals)}")
    print(f"  documents upsert candidates: {len(plan.documents)}")
    print(f"  sections upsert candidates: {len(plan.sections)}")
    print(f"  chunks upsert candidates: {len(plan.chunks)}")
    print(f"  tracker/company rows available: {len(plan.companies)}")
    print(f"  anomalies: {len(plan.anomalies)}")
    for anomaly in plan.anomalies[:50]:
        print(f"  - {anomaly}")
    if len(plan.anomalies) > 50:
        print(f"  ... {len(plan.anomalies) - 50} more")


def load_db_dependencies():
    global Company, SustainabilityReport, LogicalSource, SourceVersion
    global FileAlias, ExtractionArtifact, SourceApproval, Document, Section, Chunk

    try:
        from dotenv import load_dotenv
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from models import Company as _Company
        from models import SustainabilityReport as _SustainabilityReport
        from models import LogicalSource as _LogicalSource
        from models import SourceVersion as _SourceVersion
        from models import FileAlias as _FileAlias
        from models import ExtractionArtifact as _ExtractionArtifact
        from models import SourceApproval as _SourceApproval
        from models import Document as _Document
        from models import Section as _Section
        from models import Chunk as _Chunk
    except ModuleNotFoundError as exc:
        missing = exc.name or str(exc)
        raise ModuleNotFoundError(
            f"Missing dependency '{missing}'. Install project requirements before "
            "running commit mode: pip install -r requirements.txt"
        ) from exc

    Company = _Company
    SustainabilityReport = _SustainabilityReport
    LogicalSource = _LogicalSource
    SourceVersion = _SourceVersion
    FileAlias = _FileAlias
    ExtractionArtifact = _ExtractionArtifact
    SourceApproval = _SourceApproval
    Document = _Document
    Section = _Section
    Chunk = _Chunk

    return load_dotenv, create_engine, sessionmaker


def ensure_identity_rows(session, plan: LoadPlan, companies: dict[str, Company], counts: dict) -> None:
    for row in plan.logical_sources:
        obj = session.get(LogicalSource, row["logical_source_id"])
        if obj is None:
            obj = LogicalSource(logical_source_id=row["logical_source_id"])
            session.add(obj)
            counts["logical_sources_inserted"] += 1
        obj.company_id = companies.get(row["ticker"]).company_id if companies.get(row["ticker"]) else None
        for key in ("policy_source_id", "source_type", "report_year", "title", "lifecycle_state", "ownership_review_required"):
            setattr(obj, key, row.get(key))
    session.flush()
    for row in plan.source_versions:
        obj = session.get(SourceVersion, row["source_version_id"])
        if obj is None:
            obj = SourceVersion(source_version_id=row["source_version_id"])
            session.add(obj)
            counts["source_versions_inserted"] += 1
        for key in ("logical_source_id", "original_sha256", "byte_size", "media_type", "lifecycle_state", "ownership_review_required"):
            setattr(obj, key, row.get(key))
    session.flush()
    for row in plan.extraction_artifacts:
        obj = session.get(ExtractionArtifact, row["extraction_artifact_id"])
        if obj is None:
            obj = ExtractionArtifact(extraction_artifact_id=row["extraction_artifact_id"])
            session.add(obj)
            counts["extraction_artifacts_inserted"] += 1
        for key in ("source_version_id", "artifact_role", "artifact_sha256", "storage_path", "drive_file_id", "parser_or_model", "prompt_version", "source_page_sha256", "verification_state", "lifecycle_state"):
            setattr(obj, key, row.get(key))
    session.flush()
    for row in plan.file_aliases:
        obj = session.get(FileAlias, row["file_alias_id"])
        if obj is None:
            obj = FileAlias(file_alias_id=row["file_alias_id"])
            session.add(obj)
            counts["file_aliases_inserted"] += 1
        company = companies.get(row["observed_ticker"])
        obj.observed_company_id = company.company_id if company else None
        for key in ("source_version_id", "extraction_artifact_id", "file_path", "drive_file_id", "observed_filename", "lifecycle_state"):
            setattr(obj, key, row.get(key))
    session.flush()
    for row in plan.source_approvals:
        obj = session.query(SourceApproval).filter_by(
            source_version_id=row["source_version_id"],
            extraction_artifact_id=row["extraction_artifact_id"],
            approval_type=row["approval_type"],
            approved_source_sha256=row["approved_source_sha256"],
            approved_artifact_sha256=row["approved_artifact_sha256"],
        ).first()
        if obj is None:
            obj = SourceApproval(**row)
            session.add(obj)
            counts["source_approvals_inserted"] += 1


def get_session_factory():
    load_dotenv, create_engine, sessionmaker = load_db_dependencies()
    load_dotenv()
    db_url = os.getenv("DB_URL") or os.getenv("DATABASE_URL")
    if not db_url:
        raise EnvironmentError("DB_URL or DATABASE_URL is not set; commit mode cannot run.")
    engine = create_engine(db_url, future=True, pool_pre_ping=True)
    return sessionmaker(bind=engine, future=True)


def ensure_companies(session, companies: dict[str, dict]) -> dict[str, Company]:
    existing = {
        company.ticker.upper(): company
        for company in session.query(Company).all()
        if company.ticker
    }

    for ticker, row in companies.items():
        if ticker in existing:
            continue
        cik = (row.get("cik") or "").strip()
        name = (row.get("name") or row.get("company_name") or "").strip()
        if not cik or not name:
            continue
        company = Company(
            ticker=ticker,
            cik=cik,
            name=name,
            sector=(row.get("sector") or "").strip() or None,
            exchange=(row.get("exchange") or "").strip() or None,
        )
        session.add(company)
        session.flush()
        existing[ticker] = company

    return existing


def find_report(session, company_id: int, year: int | None) -> SustainabilityReport | None:
    query = session.query(SustainabilityReport).filter(SustainabilityReport.company_id == company_id)
    if year is None:
        query = query.filter(SustainabilityReport.year.is_(None))
    else:
        query = query.filter(SustainabilityReport.year == year)
    return query.first()


def apply_plan(plan: LoadPlan) -> dict[str, int]:
    SessionLocal = get_session_factory()
    counts = defaultdict(int)

    with SessionLocal() as session:
        companies = ensure_companies(session, plan.companies)
        ensure_identity_rows(session, plan, companies, counts)

        for row in plan.reports:
            company = companies.get(row["ticker"])
            if company is None:
                counts["reports_skipped"] += 1
                continue
            report = find_report(session, company.company_id, row["year"])
            if report is None:
                report = SustainabilityReport(company_id=company.company_id, year=row["year"])
                session.add(report)
                counts["reports_inserted"] += 1
            else:
                counts["reports_updated"] += 1
            report.report_url = row["report_url"]
            report.format = row["format"]
            report.download_status = row["download_status"]

        session.flush()

        doc_map: dict[tuple[str, str], Document] = {}
        for row in plan.documents:
            company = companies.get(row["ticker"])
            if company is None:
                counts["documents_skipped"] += 1
                continue
            document = session.query(Document).filter(Document.filepath == row["filepath"]).first()
            if document is None:
                document = Document(
                    company_id=company.company_id,
                    doc_type=row["doc_type"],
                    filepath=row["filepath"],
                    parse_status=row["parse_status"],
                )
                session.add(document)
                counts["documents_inserted"] += 1
            else:
                counts["documents_updated"] += 1
                document.company_id = company.company_id
                document.doc_type = row["doc_type"]
                document.parse_status = row["parse_status"]
            document.quality_flags = row.get("quality_flags") or ""
            document.possible_wrong_doc_type = bool(row.get("possible_wrong_doc_type"))
            document.doc_quality_status = row.get("doc_quality_status")
            document.rag_action = row.get("rag_action")
            for key in ("logical_source_id", "source_version_id", "extraction_artifact_id", "file_alias_id", "lifecycle_state"):
                if row.get(key) is not None:
                    setattr(document, key, row.get(key))
            session.flush()
            doc_map[(row["ticker"], row["pdf_stem"])] = document

        section_map: dict[tuple[str, str, str], Section] = {}
        for row in plan.sections:
            document = doc_map.get((row["ticker"], row["pdf_stem"]))
            if document is None:
                counts["sections_skipped"] += 1
                continue
            section = (
                session.query(Section)
                .filter(
                    Section.doc_id == document.doc_id,
                    Section.section_instance_id == row["section_instance_id"],
                )
                .first()
            )
            # Adopt a pre-V4 row for the first explicit instance instead of
            # leaving stale, duplicate canonical content behind.
            if section is None and row["section_instance_id"] != row["section_code"]:
                section = (
                    session.query(Section)
                    .filter(
                        Section.doc_id == document.doc_id,
                        Section.section_code == row["section_code"],
                        Section.section_instance_id == row["section_code"],
                    )
                    .first()
                )
            if section is None:
                section = Section(
                    doc_id=document.doc_id,
                    section_instance_id=row["section_instance_id"],
                    section_code=row["section_code"],
                )
                session.add(section)
                counts["sections_inserted"] += 1
            else:
                counts["sections_updated"] += 1
                section.section_instance_id = row["section_instance_id"]
                section.section_code = row["section_code"]
            section.section_title = row["section_title"]
            section.section_text = row["section_text"]
            section.char_count = row["char_count"]
            section.source_start_char = row.get("source_start_char")
            section.source_end_char = row.get("source_end_char")
            section.page_start = row.get("page_start")
            section.page_end = row.get("page_end")
            for key in ("logical_source_id", "source_version_id", "extraction_artifact_id", "lifecycle_state"):
                if row.get(key) is not None:
                    setattr(section, key, row.get(key))
            session.flush()
            section_map[
                (row["ticker"], row["pdf_stem"], row["section_instance_id"])
            ] = section

        for row in plan.chunks:
            section = section_map.get(
                (row["ticker"], row["pdf_stem"], row["section_instance_id"])
            )
            document = doc_map.get((row["ticker"], row["pdf_stem"]))
            company = companies.get(row["ticker"])
            if section is None or document is None or company is None:
                counts["chunks_skipped"] += 1
                continue

            chunk_at_location = (
                session.query(Chunk)
                .filter(
                    Chunk.section_id == section.section_id,
                    Chunk.chunk_index == row["chunk_index"],
                )
                .first()
            )
            chunk_by_external_id = None
            if row.get("external_chunk_id"):
                chunk_by_external_id = (
                    session.query(Chunk)
                    .filter(Chunk.external_chunk_id == row["external_chunk_id"])
                    .first()
                )
            if (
                chunk_at_location is not None
                and chunk_by_external_id is not None
                and chunk_at_location.chunk_id != chunk_by_external_id.chunk_id
            ):
                counts["chunks_conflicted"] += 1
                continue

            chunk = chunk_by_external_id or chunk_at_location
            if chunk is None:
                chunk = Chunk(
                    external_chunk_id=row.get("external_chunk_id"),
                    section_id=section.section_id,
                    chunk_index=row["chunk_index"],
                )
                session.add(chunk)
                counts["chunks_inserted"] += 1
            else:
                counts["chunks_updated"] += 1

            if row.get("external_chunk_id"):
                chunk.external_chunk_id = row["external_chunk_id"]
            chunk.section_id = section.section_id
            chunk.doc_id = document.doc_id
            chunk.company_id = company.company_id
            chunk.doc_type = row["doc_type"]
            chunk.section_instance_id = row["section_instance_id"]
            chunk.section_code = row["section_code"]
            if row.get("source_id") is not None:
                chunk.source_id = row["source_id"]
            if row.get("source_version_id") is not None:
                chunk.source_version_id = row["source_version_id"]
            for key in ("logical_source_id", "extraction_artifact_id", "legacy_source_version_id", "lifecycle_state"):
                if row.get(key) is not None:
                    setattr(chunk, key, row.get(key))
            chunk.chunk_type = row.get("chunk_type") or CHUNK_TYPE_NORMAL
            chunk.short_section_action = row.get("short_section_action")
            chunk.short_section_reason = row.get("short_section_reason")
            chunk.merged_section_ids = row.get("merged_section_ids")
            chunk.chunk_index = row["chunk_index"]
            chunk.chunk_text = row["chunk_text"]
            chunk.token_count = row["token_count"]
            chunk.doc_quality_status = row["doc_quality_status"]
            chunk.rag_action = row["rag_action"]
            chunk.quality_flags = row["quality_flags"]
            chunk.source_start_char = row["source_start_char"]
            chunk.source_end_char = row["source_end_char"]
            chunk.page_start = row["page_start"]
            chunk.page_end = row["page_end"]
            chunk.citation_ready = row["citation_ready"]
            if row.get("citation_validation_status") is not None:
                chunk.citation_validation_status = row["citation_validation_status"]
            if row.get("citation_validation_version") is not None:
                chunk.citation_validation_version = row["citation_validation_version"]

        session.commit()

    return dict(sorted(counts.items()))


def main():
    parser = argparse.ArgumentParser(description="Load ESG tracker, documents, sections, and chunks into PostgreSQL.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Build and print the ESG DB load plan without DB writes.")
    mode.add_argument("--commit", action="store_true", help="Write ESG rows to PostgreSQL idempotently.")
    parser.add_argument("--companies", default=str(REFERENCE_DIR / "companies.csv"))
    parser.add_argument("--tracker", default=str(REFERENCE_DIR / "sustainability_report_tracker.csv"))
    parser.add_argument("--parse-index", default=str(REFERENCE_DIR / "esg_parse_index.csv"))
    parser.add_argument("--sections-index", default=str(REFERENCE_DIR / "esg_sections_index.csv"))
    parser.add_argument("--chunks-index", default=str(REFERENCE_DIR / "esg_chunks_index.csv"))
    parser.add_argument("--raw-root", default=str(RAW_ESG_ROOT))
    parser.add_argument("--file-catalog", default=str(REFERENCE_DIR / "esg_file_catalog.csv"))
    parser.add_argument("--ocr-approvals", default=str(REFERENCE_DIR / "esg_ocr_approval.csv"))
    args = parser.parse_args()

    plan = build_plan(args)
    print_plan(plan)

    if args.dry_run:
        print()
        print("Dry run complete. No DB writes performed.")
        return

    counts = apply_plan(plan)
    print()
    print("Commit complete:")
    for name, count in counts.items():
        print(f"  {name}: {count}")


if __name__ == "__main__":
    main()
