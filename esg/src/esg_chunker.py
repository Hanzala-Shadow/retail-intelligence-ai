from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import time
import unicodedata
import uuid
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tiktoken
import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)

import config
from esg_compact_toc import has_compact_toc_cluster
from esg_year import extract_report_year


ENCODING = "cl100k_base"
CHUNK_SIZE = 500
OVERLAP = 50
MIN_CHUNK_TOKENS = 100
MAX_CHUNK_TOKENS = 600
BGE_MODEL_LIMIT = 512
BGE_INPUT_LIMIT = 500
OVERLAP_BGE_TOKENS = 48
MAX_TABLE_CONTEXT_TOKENS = 64
VALIDATED_TABLE_TRANSITION_REPAIRS = frozenset({("AMZN", "2023"), ("LOW", "2023")})
SHORT_EVIDENCE_MIN_TOKENS = 25
NAVIGATION_TRACE_MAX_TOKENS = 150
# Sections between index checkpoints. Each checkpoint re-sorts and rewrites the
# WHOLE index (write_index's already_canonical=True skips re-canonicalizing,
# but the sort, CSV serialization and fsync are still full-corpus), so a
# checkpoint per section makes the stage quadratic: a 199-document run
# rewrote a 17 MB CSV 5,515 times -- roughly 47 GB of writes for a 17 MB file,
# and the rate decayed from 1,615 to 330 chunks/min as it went. Batching cuts
# that by the batch size, but the per-checkpoint cost still scales with total
# corpus size, so the same batch size buys less as the corpus grows: the index
# is 159 MB and 51k rows as of 2026-08-06, 9x the size that motivated 25, so
# this was raised to keep total checkpoint overhead in the same proportion.
# The cost of a larger batch is bounded and cheap: a crash loses at most this
# many sections of planning, which resume redoes, and the per-section markers
# keep an interrupted section from ever being mistaken for a finished one.
CHECKPOINT_EVERY_DEFAULT = 200
# Large tables of contents sit far above NAVIGATION_TRACE_MAX_TOKENS, so the
# short-section navigation heuristic never inspects them. They are recognised
# instead by how much of their character budget is spent on dot leaders.
# Calibrated against the live corpus (2026-07-29): a TJX appendix index measured
# 0.134 and is genuine navigation, while prose chunks containing incidental dot
# runs measured 0.108 and 0.101. The cut sits between those observations.
NAVIGATION_DOT_LEADER_CHAR_RATIO = 0.12
NAVIGATION_SPACED_DOT_LEADER_CHAR_RATIO = 0.18
DOT_LEADER_RUN = re.compile(r"\.{6,}")
SPACED_DOT_LEADER_RUN = re.compile(r"(?:\.\s+){5,}\.")
CHUNK_TYPE_NORMAL = "normal"
CHUNK_TYPE_SHORT_EVIDENCE = "short_evidence"
SHORT_SECTION_ACTION_PRESERVED = "preserved"
SHORT_SECTION_ACTION_EXCLUDED = "excluded"
QUALITY_FLAG_SHORT_SECTION_EXCLUDED = "short_section_excluded_from_retrieval"
QUALITY_FLAG_UNSAFE_RETRIEVAL_CONTENT = "unsafe_retrieval_content"
QUALITY_FLAG_SECTION_HELD = "section_held_by_manual_review"
SECTION_HOLD_ACTIONS = frozenset(
    {"manual_review_before_indexing", "exclude_from_esg_index"}
)
DOC_TYPE = "sustainability"
CITATION_VALIDATION_VERSION = "semantic_v1"
# Versions the chunking rule set: CHUNK_SIZE, OVERLAP, MAX_CHUNK_TOKENS, the
# short-section and navigation-trace classifiers, and the source-alignment
# logic. Bump when any of those change.
CHUNKER_VERSION = "esg_chunk_v3"
# Frozen release identity. Change it whenever source selection or any
# processing stage changes, so a vector index can be tied to the exact
# corpus that produced it. Override with ESG_DATASET_ID.
DEFAULT_DATASET_ID = "esg_docling_fusion_v1"
# BGE's input window, including [CLS] and [SEP]. Anything longer is
# truncated by the model without warning.
BGE_MAX_INPUT_TOKENS = 512

# How much of a chunk may sit on contents, divider or cover pages before it is
# no longer worth retrieving. Weighted by characters: a chunk straddling a
# contents page and a content page keeps its prose, one that is mostly the
# listing does not.
MAX_FURNITURE_CHAR_SHARE = 0.70
VERIFIED_CITATION_STATUSES = {
    "verified_exact",
    "verified_whitespace_normalized",
}
DOCUMENT_LABELS = {
    "sustainability": "Sustainability Report",
    "annual_report_with_esg": "Annual Report with ESG Disclosure",
    "program_impact_report": "Program Impact Report",
}
TOPIC_LABELS = {
    "ceo_letter": "CEO Letter",
    "about_this_report": "About This Report",
    "environmental": "Environmental",
    "climate": "Climate",
    "energy": "Energy",
    "emissions": "Emissions",
    "waste": "Waste",
    "water": "Water",
    "social": "Social",
    "human_capital": "Human Capital",
    "diversity_equity_inclusion": "Diversity, Equity and Inclusion",
    "supply_chain_ethics": "Supply Chain and Ethics",
    "community": "Community",
    "governance": "Governance",
    "ethics_compliance": "Ethics and Compliance",
    "data_summary": "Data Summary",
    "appendix": "Appendix",
    "other": "Other",
    "full_document": "Full Document",
}
TABLE_SHORT_LINE_RATIO = 0.50
TABLE_DIGIT_RATIO = 0.030
LIST_BULLET_RATIO = 0.30
SHORT_LINE_MAX_WORDS = 4
BULLET_RE = re.compile(r"^([•●▪◦\-–—\*]|\(?\d{1,2}[.)])\s+")

_WORKER_OUTPUT_ROOT: Path | None = None
_WORKER_SECTION_METADATA: dict[tuple[str, str, str], dict] | None = None
_WORKER_DOC_METADATA: dict[tuple[str, str], dict] | None = None
_WORKER_PARSED_TEXT_CACHE: dict[Path, tuple[str, str]] | None = None
_WORKER_PAGE_MAP_CACHE: dict[Path, list[dict]] | None = None
_WORKER_INDEX_ROWS_BY_SECTION: dict[tuple[str, str, str], list[dict]] | None = None
_WORKER_ARTIFACT_KEYS: set[tuple[str, str, str]] | None = None
_WORKER_ENCODER = None
_WORKER_BGE_TOKENIZER = None
_WORKER_COMPANY_NAMES: dict[str, str] | None = None

CHUNKS_INDEX_FIELDS = [
    "chunk_id",
    "source_id",
    "source_version_id",
    "ticker",
    "canonical_ticker",
    "doc_type",
    "source_type",
    "source_scope",
    "retrieval_tier",
    "include_in_esg_index",
    "duplicate_of_source_id",
    "doc_quality_status",
    "rag_action",
    # Roles of the pages this chunk covers, so a furniture exclusion can be
    # audited rather than inferred.
    "page_role",
    "quality_flags",
    "pdf_stem",
    "section_code",
    "section_instance_id",
    "physical_section_title",
    "subsection_context",
    "subsection_contexts_json",
    "chunk_index",
    "chunk_type",
    "short_section_action",
    "short_section_reason",
    "merged_section_ids",
    "table_context",
    "token_count",
    "char_count",
    "chunk_file",
    "source_section_file",
    "source_size_bytes",
    "source_mtime_utc",
    "source_sha256",
    "parsed_text_sha256",
    "section_text_sha256",
    "chunk_text_sha256",
    "embedding_text",
    "embedding_text_sha256",
    "dataset_id",
    "source_start_char",
    "source_end_char",
    "page_start",
    "page_end",
    "citation_ready",
    "citation_validation_status",
    "citation_validation_version",
]


@dataclass(frozen=True)
class SourceFingerprint:
    """Fingerprint of a section input used to validate resumable output."""

    size_bytes: int
    mtime_utc: str
    sha256: str

    def as_index_fields(self) -> dict[str, str]:
        return {
            "source_size_bytes": str(self.size_bytes),
            "source_mtime_utc": self.mtime_utc,
            "source_sha256": self.sha256,
        }


@dataclass
class ChunkOutput:
    path: Path
    text: str
    row: dict


@dataclass(frozen=True)
class CandidateChunk:
    text: str
    source_start: int
    source_end: int
    bge_tokens: int
    cl100k_tokens: int
    table_header_start: int | None = None
    table_context: str = ""
    subsection_context: str = ""
    subsection_contexts: tuple[str, ...] = ()


@dataclass(frozen=True)
class SubsectionContextSpan:
    title: str
    start_char: int
    end_char: int


@dataclass
class SectionPlan:
    ticker: str
    pdf_stem: str
    section_code: str
    section_instance_id: str
    source_file: Path
    source_fingerprint: SourceFingerprint
    source_token_count: int
    outputs: list[ChunkOutput]
    short_section_action: str = ""
    short_section_reason: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        return self.ticker, self.pdf_stem, self.section_instance_id

    @property
    def is_short(self) -> bool:
        return self.source_token_count < MIN_CHUNK_TOKENS

    @property
    def is_unhandled_short(self) -> bool:
        return self.is_short and not self.outputs


def display_path(path: str | Path) -> str:
    path = Path(path)
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def resolve_path(raw_path: str | None) -> Path | None:
    if not raw_path:
        return None
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return Path.cwd() / path


def parse_int(value: str | int | None) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_subsection_spans(metadata: dict[str, Any], text_length: int) -> list[SubsectionContextSpan]:
    """Read validated, ordered heading spans from the physical section row."""
    raw = (metadata.get("subsection_spans_json") or "").strip()
    spans: list[SubsectionContextSpan] = []
    if raw:
        try:
            values = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            values = []
        if isinstance(values, list):
            for value in values:
                if not isinstance(value, dict):
                    continue
                title = str(value.get("title") or "").strip()
                start = parse_int(value.get("start_char"))
                end = parse_int(value.get("end_char"))
                if (
                    title
                    and start is not None
                    and end is not None
                    and 0 <= start < end <= text_length
                ):
                    spans.append(SubsectionContextSpan(title, start, end))
    spans.sort(key=lambda span: (span.start_char, span.end_char, span.title))
    return spans


def subsection_context_for_range(
    spans: list[SubsectionContextSpan],
    start: int,
    end: int,
    fallback_title: str = "",
) -> tuple[str, tuple[str, ...]]:
    """Return the active heading plus any ordered transitions inside a chunk."""
    titles: list[str] = []
    seen_titles: set[str] = set()
    active = None
    for span in spans:
        if span.start_char <= start < span.end_char:
            active = span
        elif span.start_char > start:
            break
    if active is None:
        prior = [span for span in spans if span.start_char <= start]
        active = prior[-1] if prior else None
    if active is not None:
        titles.append(active.title)
        seen_titles.add(active.title.casefold())
    for span in spans:
        if (
            start < span.start_char < end
            and span.title.casefold() not in seen_titles
        ):
            titles.append(span.title)
            seen_titles.add(span.title.casefold())
    if not titles and fallback_title.strip():
        titles.append(fallback_title.strip())
    return " → ".join(titles), tuple(titles)


def metadata_with_subsection(
    metadata: dict[str, Any],
    spans: list[SubsectionContextSpan],
    start: int,
    end: int,
) -> tuple[dict[str, Any], str, tuple[str, ...]]:
    if "subsection_fallback" in metadata:
        fallback = str(metadata.get("subsection_fallback") or "")
    else:
        fallback = str(
            metadata.get("physical_section_title")
            or metadata.get("section_title")
            or metadata.get("section_title_original")
            or ""
        )
    context, titles = subsection_context_for_range(spans, start, end, fallback)
    value = dict(metadata)
    value["section_title_original"] = context or "unknown"
    return value, context, titles


def parse_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return (value or "").strip().lower() in {"true", "1", "yes", "y"}


def valid_token_count_for_chunk_type(
    token_count: int | None,
    chunk_type: str | None,
) -> bool:
    if token_count is None:
        return False
    if (chunk_type or CHUNK_TYPE_NORMAL).strip() == CHUNK_TYPE_SHORT_EVIDENCE:
        return SHORT_EVIDENCE_MIN_TOKENS <= token_count < MIN_CHUNK_TOKENS
    return MIN_CHUNK_TOKENS <= token_count <= MAX_CHUNK_TOKENS


def dot_leader_char_ratio(text: str) -> float:
    """Share of characters spent on dot-leader runs (the '......' of a TOC)."""
    if not text:
        return 0.0
    return sum(len(match.group()) for match in DOT_LEADER_RUN.finditer(text)) / len(text)


def spaced_dot_leader_char_ratio(text: str) -> float:
    """Share of characters that are dots in spaced leaders such as '. . . .' ."""
    if not text:
        return 0.0
    dot_count = sum(
        match.group().count(".") for match in SPACED_DOT_LEADER_RUN.finditer(text)
    )
    return dot_count / len(text)


def is_large_navigation_trace_section(text: str, token_count: int) -> bool:
    """Recognise tables of contents too large for the short-section heuristic.

    Deliberately does NOT reuse that heuristic's `has_prose_tail` guard. Dot
    leaders create `[.!?]\\s` matches, so a table of contents inflates the
    sentence-mark count and its headings supply enough lowercase words to look
    like prose: measured against the live corpus, that guard suppressed
    detection on 13 of 22 genuine navigation chunks.
    """
    if token_count <= NAVIGATION_TRACE_MAX_TOKENS:
        return False  # already covered by classify_navigation_trace_section
    return (
        has_compact_toc_cluster(text)
        or dot_leader_char_ratio(text) >= NAVIGATION_DOT_LEADER_CHAR_RATIO
        or spaced_dot_leader_char_ratio(text)
        >= NAVIGATION_SPACED_DOT_LEADER_CHAR_RATIO
    )


_NUMERIC_ONLY_LINE = re.compile(
    r"^[\s$€£¥+\-–—()\[\],.:;%‰/\\|]+"
    r"(?:\d[\d\s$€£¥+\-–—()\[\],.:;%‰/\\|]*)$"
)


def is_orphan_numeric_fragment(text: str) -> bool:
    """Return true for chunks dominated by values with no usable labels."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    numeric_only = sum(
        bool(
            any(char.isdigit() for char in line)
            and _NUMERIC_ONLY_LINE.fullmatch(line.strip().strip("|").strip())
        )
        for line in lines
    )
    prose_lines = sum(
        bool(re.search(r"[A-Za-z]{3,}\s+[A-Za-z]{3,}", line)) for line in lines
    )
    return numeric_only >= 3 and numeric_only / len(lines) >= 0.60 and prose_lines <= 1


def retrieval_chunk_exclusion_reason(text: str, token_count: int) -> str:
    """High-precision content gates applied after chunk boundaries are known."""
    if is_large_navigation_trace_section(text, token_count):
        return "table_of_contents_or_navigation"
    if is_orphan_numeric_fragment(text):
        return "orphan_numeric_fragment"
    return ""


def classify_navigation_trace_section(text: str, token_count: int) -> str:
    """Identify strong TOC/navigation traces up to the documented 150-token ceiling."""
    if token_count > NAVIGATION_TRACE_MAX_TOKENS:
        return ""

    normalized = normalized_text(text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    dot_leaders = len(re.findall(r"\.{5,}", text))
    page_number_refs = len(re.findall(r"(?:\.{2,}\s*)\b\d{1,3}\b", text))
    sentence_marks = len(re.findall(r"[.!?](?:\s|$)", normalized))
    prose_candidate = re.sub(r"\.{5,}", " ", text)
    prose_candidate = re.sub(r"\b\d{1,3}\b", " ", prose_candidate)
    lower_prose_words = re.findall(r"\b[a-z]{4,}\b", prose_candidate)
    has_prose_tail = sentence_marks > 0 and len(lower_prose_words) >= 7

    if dot_leaders and not has_prose_tail and (page_number_refs >= 1 or len(lines) <= 6):
        return "table_of_contents_or_navigation"
    return ""


def classify_short_section(text: str, token_count: int) -> tuple[str, str]:
    """Keep real short evidence; drop only obvious navigation or tiny fragments."""
    if has_compact_toc_cluster(text):
        return SHORT_SECTION_ACTION_EXCLUDED, "table_of_contents_or_navigation"
    if token_count < SHORT_EVIDENCE_MIN_TOKENS:
        return SHORT_SECTION_ACTION_EXCLUDED, "below_short_evidence_min_tokens"

    navigation_reason = classify_navigation_trace_section(text, token_count)
    if navigation_reason:
        return SHORT_SECTION_ACTION_EXCLUDED, navigation_reason
    if is_orphan_numeric_fragment(text):
        return SHORT_SECTION_ACTION_EXCLUDED, "orphan_numeric_fragment"

    normalized = normalized_text(text)
    sentence_marks = len(re.findall(r"[.!?](?:\s|$)", normalized))
    esg_nav_terms = sum(
        1
        for term in (
            "overview",
            "appendix",
            "climate",
            "environment",
            "social",
            "governance",
            "community",
            "people",
            "supplier",
            "supply",
            "data",
            "index",
            "reporting",
            "progress",
            "water",
            "waste",
            "energy",
        )
        if re.search(rf"\b{re.escape(term)}\b", normalized, flags=re.IGNORECASE)
    )

    if token_count < 60 and sentence_marks == 0 and esg_nav_terms >= 4:
        return SHORT_SECTION_ACTION_EXCLUDED, "navigation_term_cluster"

    return SHORT_SECTION_ACTION_PRESERVED, "meaningful_short_section"


def quality_flags_with_existing(raw_flags: str, *new_flags: str) -> str:
    flags: list[str] = []
    seen: set[str] = set()
    for flag in [*(raw_flags or "").split("|"), *new_flags]:
        cleaned = flag.strip()
        if cleaned and cleaned not in seen:
            flags.append(cleaned)
            seen.add(cleaned)
    return "|".join(flags)


def apply_section_hold(doc_meta: dict, section_meta: dict) -> dict:
    """Let a reviewer stop one section from reaching the index.

    Document-level quality gates cannot express "this document is fine but this
    section is not". A manual review that fails a single section -- a corrupted
    table, a wrong physical boundary, interleaved reading order -- records the
    decision in the section hold registry, and it is applied here so the
    section's chunks are still built and citable but never indexed blind.
    """
    action = (section_meta.get("hold_rag_action") or "").strip()
    if not action:
        return doc_meta
    if action not in SECTION_HOLD_ACTIONS:
        raise ValueError(
            f"unsupported hold action {action!r}; expected one of "
            f"{sorted(SECTION_HOLD_ACTIONS)}"
        )
    held_meta = dict(doc_meta)
    held_meta["include_in_esg_index"] = False
    held_meta["rag_action"] = action
    held_meta["quality_flags"] = quality_flags_with_existing(
        str(doc_meta.get("quality_flags") or ""),
        QUALITY_FLAG_SECTION_HELD,
        (section_meta.get("hold_reason") or "").strip(),
    )
    return held_meta


def load_section_hold_registry(
    hold_path: Path | None,
) -> dict[tuple[str, str, str], dict]:
    """Load sparse per-section review decisions; unlisted sections are untouched."""
    holds: dict[tuple[str, str, str], dict] = {}
    if hold_path is None:
        return holds
    if not hold_path.exists():
        raise FileNotFoundError(f"section hold registry does not exist: {hold_path}")
    with hold_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        required_fields = {
            "ticker",
            "pdf_stem",
            "section_instance_id",
            "rag_action",
            "reason",
        }
        fieldnames = set(reader.fieldnames or ())
        missing_fields = sorted(required_fields - fieldnames)
        if missing_fields:
            raise ValueError(
                f"{hold_path}: malformed section hold registry; missing column(s): "
                f"{', '.join(missing_fields)}"
            )
        for line_number, row in enumerate(reader, start=2):
            if None in row:
                raise ValueError(
                    f"{hold_path}:{line_number}: malformed CSV row with extra field(s)"
                )
            ticker = (row.get("ticker") or "").strip().upper()
            pdf_stem = (row.get("pdf_stem") or "").strip()
            section_instance_id = (row.get("section_instance_id") or "").strip()
            action = (row.get("rag_action") or "").strip()
            if not (ticker and pdf_stem and section_instance_id):
                raise ValueError(
                    f"{hold_path}:{line_number}: ticker, pdf_stem, and "
                    "section_instance_id are required"
                )
            if action not in SECTION_HOLD_ACTIONS:
                raise ValueError(
                    f"{hold_path}: unsupported rag_action {action!r} for "
                    f"{ticker}/{pdf_stem}/{section_instance_id}"
                )
            key = (ticker, pdf_stem, section_instance_id)
            if key in holds:
                raise ValueError(
                    f"{hold_path}:{line_number}: duplicate section hold for "
                    f"{ticker}/{pdf_stem}/{section_instance_id}"
                )
            holds[key] = row
    return holds


def merge_section_holds(
    section_metadata: dict[tuple[str, str, str], dict],
    holds: dict[tuple[str, str, str], dict],
) -> int:
    """Fold hold decisions into section metadata so workers inherit them."""
    applied = 0
    for key, hold_row in holds.items():
        section_meta = section_metadata.get(key)
        if section_meta is None:
            raise ValueError(f"held section is not in the sections index: {key}")
        section_meta["hold_rag_action"] = (hold_row.get("rag_action") or "").strip()
        section_meta["hold_reason"] = (hold_row.get("reason") or "").strip()
        applied += 1
    return applied


def doc_meta_for_excluded_short_section(doc_meta: dict, reason: str) -> dict:
    """Keep a citation trail for non-evidence short sections without indexing them."""
    excluded_meta = dict(doc_meta)
    excluded_meta["include_in_esg_index"] = False
    excluded_meta["rag_action"] = "exclude_from_esg_index"
    excluded_meta["quality_flags"] = quality_flags_with_existing(
        str(doc_meta.get("quality_flags") or ""),
        QUALITY_FLAG_SHORT_SECTION_EXCLUDED,
        reason,
    )
    return excluded_meta


def doc_meta_for_excluded_retrieval_content(doc_meta: dict, reason: str) -> dict:
    """Keep unsafe content citable while preventing retrieval indexing."""
    excluded_meta = dict(doc_meta)
    if parse_bool(excluded_meta.get("include_in_esg_index", True)):
        excluded_meta["include_in_esg_index"] = False
        excluded_meta["rag_action"] = "exclude_from_esg_index"
    excluded_meta["quality_flags"] = quality_flags_with_existing(
        str(doc_meta.get("quality_flags") or ""),
        QUALITY_FLAG_UNSAFE_RETRIEVAL_CONTENT,
        reason,
    )
    return excluded_meta


def row_quality_flags(row: dict) -> set[str]:
    raw_flags = (row.get("quality_flags") or "").strip()
    return {flag for flag in raw_flags.split("|") if flag}


def doc_quality_status(parse_row: dict) -> str:
    flags = row_quality_flags(parse_row)
    if parse_bool(parse_row.get("possible_wrong_doc_type")):
        return "exclude_from_esg_rag"
    if parse_row.get("status") != "parsed" or flags & {
        "garbled_text",
        "low_readable_word_ratio",
        "low_text_per_page",
        # Set by the bridge when most of a document's words landed in no
        # region. The text is present but has no reading order, so it must not
        # enter retrieval unreviewed.
        "high_unplaced_text",
    }:
        return "needs_review"
    return "ok"


def rag_action_for_status(status: str) -> str:
    if status == "exclude_from_esg_rag":
        return "exclude_from_esg_index"
    if status == "needs_review":
        return "manual_review_before_indexing"
    return "index_as_esg"


def doc_type_for_parse_row(parse_row: dict) -> str:
    if parse_bool(parse_row.get("possible_wrong_doc_type")):
        return "annual_report_with_esg"
    return DOC_TYPE


def _default_source_id(ticker: str, pdf_stem: str) -> str:
    """Stable logical ID: unchanged when the bytes are replaced under the same name."""
    normalized_stem = re.sub(r"[^A-Za-z0-9]+", "_", pdf_stem).strip("_")
    return f"{ticker}__{normalized_stem}"


def load_source_registry(registry_path: Path | None) -> dict[tuple[str, str], dict]:
    """Load sparse document decisions; unlisted documents receive safe defaults."""
    metadata: dict[tuple[str, str], dict] = {}
    if registry_path is None or not registry_path.exists():
        return metadata
    with registry_path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ticker = (row.get("observed_ticker") or row.get("ticker") or "").strip().upper()
            pdf_stem = (row.get("pdf_stem") or "").strip()
            if ticker and pdf_stem:
                metadata[(ticker, pdf_stem)] = row
    return metadata


def load_company_names(manifest_path: Path | None) -> dict[str, str]:
    """Load the same canonical company names used by P1 enrichment."""
    if manifest_path is None or not manifest_path.exists():
        return {}
    with manifest_path.open(newline="", encoding="utf-8-sig") as handle:
        return {
            (row.get("ticker") or "").strip().upper():
            (row.get("company_name") or "").strip()
            for row in csv.DictReader(handle)
            if (row.get("ticker") or "").strip()
        }


def load_doc_metadata(
    parse_index_path: Path,
    source_registry_path: Path | None = config.ESG_SOURCE_REGISTRY_CSV,
) -> dict[tuple[str, str], dict]:
    metadata: dict[tuple[str, str], dict] = {}
    if not parse_index_path.exists():
        return metadata
    registry = load_source_registry(source_registry_path)
    with parse_index_path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ticker = (row.get("ticker") or "").strip().upper()
            source_pdf = row.get("source_pdf") or row.get("pdf_file") or ""
            pdf_stem = Path(source_pdf).stem if source_pdf else Path(row.get("pdf_file") or "").stem
            if not ticker or not pdf_stem:
                continue
            quality_status = doc_quality_status(row)
            decision = registry.get((ticker, pdf_stem), {})
            source_type = (
                decision.get("source_type")
                or decision.get("doc_type")
                or doc_type_for_parse_row(row)
            ).strip()
            source_id = (
                decision.get("source_id") or _default_source_id(ticker, pdf_stem)
            ).strip()
            include_in_esg_index = parse_bool(
                decision.get("include_in_esg_index", "true")
            )
            if decision and not include_in_esg_index:
                rag_action = "exclude_from_esg_index"
            else:
                rag_action = rag_action_for_status(quality_status)
            # Inclusion must follow the action. It previously defaulted to true
            # regardless, so a document whose quality status was needs_review
            # carried rag_action=manual_review_before_indexing AND
            # include_in_esg_index=true -- TGT-2019 produced 203 such chunks.
            # Only rows cleared for indexing may enter the retrieval corpus.
            if rag_action != "index_as_esg":
                include_in_esg_index = False
            pdf_sha256 = (row.get("source_sha256") or "").strip()
            version_suffix = pdf_sha256[:12] if pdf_sha256 else "unknown"
            metadata[(ticker, pdf_stem)] = {
                "source_id": source_id,
                "source_version_id": f"{source_id}__{version_suffix}",
                "canonical_ticker": (
                    decision.get("canonical_ticker") or ticker
                ).strip().upper(),
                "doc_type": source_type,
                "source_type": source_type,
                "source_scope": (decision.get("source_scope") or "full_report").strip(),
                "retrieval_tier": (decision.get("retrieval_tier") or "primary").strip(),
                "include_in_esg_index": include_in_esg_index,
                "duplicate_of_source_id": (
                    decision.get("duplicate_of_source_id") or ""
                ).strip(),
                "doc_quality_status": quality_status,
                "rag_action": rag_action,
                "quality_flags": row.get("quality_flags") or "",
                "page_map_file": row.get("page_map_file") or "",
                "parsed_text_file": row.get("parsed_text_file") or "",
                "source_pdf_sha256": pdf_sha256,
            }
    return metadata


def load_section_metadata(sections_index_path: Path) -> dict[tuple[str, str, str], dict]:
    metadata: dict[tuple[str, str, str], dict] = {}
    if not sections_index_path.exists():
        return metadata
    with sections_index_path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ticker = (row.get("ticker") or "").strip().upper()
            pdf_stem = (row.get("pdf_stem") or "").strip()
            section_code = (row.get("section_code") or "").strip()
            section_instance_id = (
                row.get("section_instance_id") or section_code
            ).strip()
            if ticker and pdf_stem and section_instance_id:
                metadata[(ticker, pdf_stem, section_instance_id)] = row
    return metadata


def read_page_map(page_map_file: str | None) -> list[dict]:
    path = resolve_path(page_map_file)
    if path is None or not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_page_map_cached(
    page_map_file: str | None,
    cache: dict[Path, list[dict]] | None,
) -> list[dict]:
    """Read each document page map once per process without weakening validation."""
    path = resolve_path(page_map_file)
    if path is None or not path.exists():
        return []
    if cache is None:
        return read_page_map(page_map_file)
    resolved = path.resolve()
    cached = cache.get(resolved)
    if cached is not None:
        return cached
    with resolved.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    cache[resolved] = rows
    return rows


def page_roles_for_span(
    page_spans: list[dict], start: int | None, end: int | None
) -> tuple[list[str], float]:
    """Roles a span touches, and the share of its characters on furniture pages.

    Weighted by characters rather than counted by page. A section that begins
    on a contents page and runs onto the next one produces a chunk touching
    both, so 'every page is furniture' never fires and the chunk survives with
    a contents listing as most of its text -- URBN and Valvoline both did. What
    matters is how much of THIS chunk is furniture, not how many pages it
    happens to overlap.

    Returns ([], 0.0) when the span resolves to no page. Absent evidence is not
    evidence of furniture.
    """
    if start is None or end is None or not page_spans:
        return [], 0.0

    roles: list[tuple[int, str]] = []
    furniture_chars = 0
    total_chars = 0
    for row in page_spans:
        page_start = parse_int(row.get("char_start"))
        page_end = parse_int(row.get("char_end"))
        page_number = parse_int(row.get("page"))
        if page_start is None or page_end is None or page_number is None:
            continue
        overlap = min(end, page_end) - max(start, page_start)
        if overlap <= 0:
            continue
        role = (row.get("page_role") or "").strip() or "content"
        roles.append((page_number, role))
        total_chars += overlap
        if role != "content":
            furniture_chars += overlap

    share = furniture_chars / total_chars if total_chars else 0.0
    return [role for _, role in sorted(roles)], share


def pages_for_span(page_spans: list[dict], start: int | None, end: int | None) -> tuple[str, str]:
    if start is None or end is None or not page_spans:
        return "", ""

    pages: list[int] = []
    for row in page_spans:
        page_start = parse_int(row.get("char_start"))
        page_end = parse_int(row.get("char_end"))
        page_number = parse_int(row.get("page"))
        if page_start is None or page_end is None or page_number is None:
            continue
        if page_end <= start or page_start >= end:
            continue
        pages.append(page_number)

    if not pages:
        return "", ""
    return str(min(pages)), str(max(pages))


def locate_text_span(source_text: str, needle: str, start_hint: int = 0) -> tuple[int | None, int | None]:
    """Locate the complete needle; a matching prefix is never sufficient."""
    text = needle.strip()
    if not text:
        return None, None
    exact_start = source_text.find(text, start_hint)
    if exact_start < 0:
        exact_start = source_text.find(text)
    if exact_start >= 0:
        return exact_start, exact_start + len(text)
    return locate_text_span_normalized(source_text, text, start_hint=start_hint)


def normalize_with_source_map(text: str) -> tuple[str, list[int]]:
    normalized_chars: list[str] = []
    source_positions: list[int] = []
    pending_space_position: int | None = None

    for index, char in enumerate(text):
        if char.isspace():
            if normalized_chars and pending_space_position is None:
                pending_space_position = index
            continue
        if pending_space_position is not None and normalized_chars:
            normalized_chars.append(" ")
            source_positions.append(pending_space_position)
        pending_space_position = None
        normalized_chars.append(char)
        source_positions.append(index)

    return "".join(normalized_chars), source_positions


def locate_text_span_normalized(
    source_text: str,
    needle: str,
    start_hint: int = 0,
) -> tuple[int | None, int | None]:
    search_offset = max(start_hint, 0)
    search_text = source_text[search_offset:]
    normalized_source, source_map = normalize_with_source_map(search_text)
    normalized_needle, _ = normalize_with_source_map(needle)
    if not normalized_source or not normalized_needle:
        return None, None

    normalized_start = normalized_source.find(normalized_needle)
    if normalized_start < 0 and search_offset:
        search_offset = 0
        normalized_source, source_map = normalize_with_source_map(source_text)
        normalized_start = normalized_source.find(normalized_needle)
    if normalized_start < 0:
        return None, None

    normalized_end = normalized_start + len(normalized_needle) - 1
    if normalized_start >= len(source_map) or normalized_end >= len(source_map):
        return None, None
    return (
        search_offset + source_map[normalized_start],
        search_offset + source_map[normalized_end] + 1,
    )


def normalized_text(text: str) -> str:
    normalized, _ = normalize_with_source_map(text)
    return normalized


def normalize_for_embedding(text: str) -> str:
    """Normalize one metadata value without changing the source chunk body."""
    out = unicodedata.normalize("NFKC", text)
    out = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", out)
    out = re.sub(r"(\w)[\-‐‑]\n(\w)", r"\1\2", out)
    out = re.sub(r"[ \t\u00a0\u2000-\u200a\u202f\u205f\u3000]+", " ", out)
    out = "\n".join(line.strip() for line in out.split("\n"))
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def classify_embedding_content(text: str, table_context: str = "") -> str:
    """Match the live embedding-context classifier for token budgeting."""
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines:
        return "narrative"
    short_ratio = sum(
        len(line.split()) <= SHORT_LINE_MAX_WORDS for line in lines
    ) / len(lines)
    digit_ratio = sum(char.isdigit() for char in text) / max(len(text), 1)
    bullet_ratio = sum(bool(BULLET_RE.match(line)) for line in lines) / len(lines)
    if bullet_ratio >= LIST_BULLET_RATIO:
        return "list"
    if short_ratio >= TABLE_SHORT_LINE_RATIO and digit_ratio >= TABLE_DIGIT_RATIO:
        return "table_continuation" if table_context else "table"
    return "narrative"


def final_embedding_text(
    metadata: dict[str, Any], source_text: str, table_context: str = ""
) -> str:
    """Build the exact embedding header and byte-exact source chunk body."""
    ticker = (metadata.get("canonical_ticker") or metadata.get("ticker") or "").strip()
    year = (metadata.get("report_year") or "").strip() or "unknown"
    if (metadata.get("report_year_status") or "").strip() == "multi_year_range":
        span = (metadata.get("report_year_span") or "").strip()
        if span and span != year:
            year = f"{year} (report covers {span})"
    section_code = (metadata.get("section_code") or "").strip()
    topic = TOPIC_LABELS.get(section_code, section_code.replace("_", " ").title())
    doc_key = (metadata.get("source_type") or metadata.get("doc_type") or "").strip()
    document = DOCUMENT_LABELS.get(
        doc_key, (doc_key or "Sustainability Report").replace("_", " ").title()
    )
    subsection = (
        metadata.get("section_title_original")
        or metadata.get("section_title")
        or "unknown"
    ).strip()
    header_lines = [
        f"Company: {(metadata.get('company_name') or '').strip() or 'unknown'}",
        f"Ticker: {ticker or 'unknown'}",
        f"Document: {document}",
        f"Reporting year: {year}",
        f"ESG topic: {topic}",
        f"Subsection: {subsection}",
        f"Content type: {classify_embedding_content(source_text, table_context)}",
    ]
    if table_context:
        clean_context = " ".join(normalize_for_embedding(table_context).split())
        if clean_context:
            header_lines.append(f"Table context: {clean_context}")
    return f"{'\n'.join(header_lines)}\n\n{source_text}"


def final_bge_token_count(
    metadata: dict[str, Any],
    source_text: str,
    tokenizer,
    table_context: str = "",
) -> int:
    return len(
        tokenizer.encode(
            final_embedding_text(metadata, source_text, table_context),
            add_special_tokens=True,
            truncation=False,
        )
    )


def _is_heading(line: str) -> bool:
    value = line.strip()
    letters = [char for char in value if char.isalpha()]
    if not value or len(value.split()) > 14 or not letters:
        return False
    upper_ratio = sum(char.isupper() for char in letters) / len(letters)
    return value.isupper() or upper_ratio >= 0.75


def _looks_like_table_line(line: str) -> bool:
    value = line.strip()
    if not value:
        return False
    if "|" in value or "\t" in value:
        return True
    words = value.split()
    digit_ratio = sum(char.isdigit() for char in value) / max(len(value), 1)
    return len(words) <= 12 and digit_ratio >= 0.08


def _table_runs(text: str) -> list[tuple[int, int, int, str]]:
    lines = list(re.finditer(r"[^\n]*(?:\n|$)", text))
    table_runs: list[tuple[int, int, int, str]] = []
    run: list[re.Match[str]] = []
    for match in [*lines, None]:
        if match is not None and match.group(0).strip() and _looks_like_table_line(match.group(0)):
            run.append(match)
            continue
        if len(run) >= 3:
            header = run[0].group(0).strip()
            table_runs.append((run[0].start(), run[-1].end(), run[0].start(), header))
        run = []
    return table_runs


def source_boundaries(
    text: str,
) -> tuple[list[int], list[int], list[tuple[int, int, int, str]]]:
    preferred = {0, len(text)}
    table_runs = _table_runs(text)
    table_starts = {
        match.start()
        for match in re.finditer(r"[^\n]*(?:\n|$)", text)
        if any(start <= match.start() < end for start, end, _, _ in table_runs)
    }
    for match in re.finditer(r"[^\n]*(?:\n|$)", text):
        if match.start() in table_starts:
            preferred.add(match.end())
    for match in re.finditer(r"[.!?][\"'\)\]]*(?=\s|$)", text):
        before = text[text.rfind("\n", 0, match.start()) + 1 : match.start()]
        if not _is_heading(before):
            preferred.add(match.end())
    for match in re.finditer(r"\n\s*\n+", text):
        preferred.add(match.start() + 1)
        preferred.add(match.end())

    filtered: list[int] = []
    for boundary in sorted(preferred):
        if filtered and _is_heading(text[filtered[-1] : boundary]):
            continue
        filtered.append(boundary)
    filtered = sorted(set(filtered) | {0, len(text)})
    fallback = set(filtered)
    fallback.update(match.end() for match in re.finditer(r"\s+", text))
    return filtered, sorted(fallback), table_runs


def _table_context_at(
    start: int, table_runs: list[tuple[int, int, int, str]]
) -> tuple[int | None, str]:
    for run_start, run_end, header_start, header_text in table_runs:
        if run_start < start < run_end:
            return header_start, header_text
    return None, ""


def _next_table_start(
    text: str,
    start: int,
    end: int,
    table_runs: list[tuple[int, int, int, str]],
) -> int | None:
    """Start a later table in a new chunk without orphaning its header."""
    for run_start, run_end, _, _ in table_runs:
        if not start < run_start < end:
            continue
        return run_start
    return None


def _credible_table_context(header_text: str, tokenizer) -> str:
    clean = " ".join(normalize_for_embedding(header_text).split())
    # A numeric value is not a table header. Treating it as context gives later
    # values a false label and makes the chunk look safer than it is.
    if not clean or not re.search(r"[A-Za-z]", clean):
        return ""
    tokens = tokenizer.encode(clean, add_special_tokens=False, truncation=False)
    return clean if len(tokens) <= MAX_TABLE_CONTEXT_TOKENS else ""


def _latest_fitting_end(
    metadata: dict[str, Any],
    text: str,
    start: int,
    boundaries: list[int],
    tokenizer,
    table_context: str = "",
    subsection_spans: list[SubsectionContextSpan] | None = None,
) -> int:
    candidates = [boundary for boundary in boundaries if boundary > start]
    first_fit = None
    for index, boundary in enumerate(candidates[:256]):
        candidate_metadata, _, _ = metadata_with_subsection(
            metadata, subsection_spans or [], start, boundary
        )
        if final_bge_token_count(
            candidate_metadata, text[start:boundary], tokenizer, table_context
        ) <= BGE_INPUT_LIMIT:
            first_fit = index
            break
    if first_fit is None:
        prefix_count = final_bge_token_count(metadata, "", tokenizer, table_context)
        raise ValueError(f"no boundary fits from source offset {start}; prefix={prefix_count}")

    # Grow the search window geometrically from first_fit instead of handing
    # the plain bisection below a fixed high=len(candidates). candidates runs
    # from start to the END OF THE WHOLE SECTION, so on a document with many
    # boundaries (a table- or list-heavy section), that fixed upper bound made
    # the very first probe encode roughly half of everything from start to the
    # section's end -- for a chunk near the start of a long section, tens of
    # thousands of characters, repeated once per chunk. The real answer is
    # always close to first_fit (a few hundred tokens away); doubling the
    # window each step finds a bracket that still fits at `lo` and does not
    # fit at `hi` (or exhausts candidates) in O(log distance) encode calls
    # instead of paying for the section's full remaining length on every
    # chunk. Measured on COST-2020's largest section: 56s -> under 1s.
    lo, step = first_fit, 1
    hi = min(first_fit + step, len(candidates))
    while hi < len(candidates):
        candidate_metadata, _, _ = metadata_with_subsection(
            metadata, subsection_spans or [], start, candidates[hi]
        )
        if final_bge_token_count(
            candidate_metadata, text[start : candidates[hi]], tokenizer, table_context
        ) <= BGE_INPUT_LIMIT:
            lo = hi
            step *= 2
            hi = min(hi + step, len(candidates))
        else:
            break
    low, high = lo, hi
    while low < high:
        middle = (low + high) // 2
        candidate_metadata, _, _ = metadata_with_subsection(
            metadata, subsection_spans or [], start, candidates[middle]
        )
        count = final_bge_token_count(
            candidate_metadata, text[start : candidates[middle]], tokenizer, table_context
        )
        if count <= BGE_INPUT_LIMIT:
            low = middle + 1
        else:
            high = middle
    if low:
        return candidates[low - 1]
    raise ValueError("a single source word exceeds the BGE input limit")


def chunk_section_v3(
    text: str,
    metadata: dict[str, Any],
    bge_tokenizer,
    cl100k_encoder,
    subsection_spans: list[SubsectionContextSpan] | None = None,
) -> list[CandidateChunk]:
    """Split at sentence/table boundaries under the final 500-token budget."""
    if not text:
        return []
    subsection_spans = subsection_spans or []
    # Subsections add embedding context, but they are not hard chunk boundaries.
    # Forcing every internal heading into a new chunk creates tiny fragments and
    # can detach table values from their labels. Sentence and table boundaries
    # still control chunking; metadata records all subsection spans a chunk uses.
    preferred, fallback, table_runs = source_boundaries(text)
    use_table_transition_repair = (
        str(metadata.get("ticker") or "").upper(),
        str(metadata.get("report_year") or ""),
    ) in VALIDATED_TABLE_TRANSITION_REPAIRS
    chunks: list[CandidateChunk] = []
    start = 0
    while start < len(text):
        ended_before_new_table = False
        table_header_start, table_context = _table_context_at(start, table_runs)
        table_context = _credible_table_context(table_context, bge_tokenizer)
        if not table_context:
            table_header_start = None
        try:
            end = _latest_fitting_end(
                metadata,
                text,
                start,
                preferred,
                bge_tokenizer,
                table_context,
                subsection_spans,
            )
        except ValueError:
            end = _latest_fitting_end(
                metadata,
                text,
                start,
                fallback,
                bge_tokenizer,
                table_context,
                subsection_spans,
            )
        if end <= start:
            raise ValueError("v3 produced an empty chunk")
        next_table_start = (
            _next_table_start(text, start, end, table_runs)
            if use_table_transition_repair
            else None
        )
        if next_table_start is not None:
            # End before the next table. Its next chunk then contains the real
            # header and its rows together. Cutting after the header creates a
            # header-only chunk followed by orphan values.
            end = next_table_start
            ended_before_new_table = True
        # The overlap back-off below can land on a start whose latest fitting
        # end is still the previous chunk's end, when the next boundary is too
        # far to reach under the token budget. Emitting that chunk would add a
        # strict subset of the previous chunk: a redundant retrieval vector
        # carrying the same subsection context. Give up the overlap at this
        # boundary rather than the forward progress.
        if chunks and end <= chunks[-1].source_end:
            start = chunks[-1].source_end
            continue
        chunk_text = text[start:end]
        chunk_metadata, subsection_context, subsection_contexts = metadata_with_subsection(
            metadata, subsection_spans, start, end
        )
        chunks.append(
            CandidateChunk(
                text=chunk_text,
                source_start=start,
                source_end=end,
                bge_tokens=final_bge_token_count(
                    chunk_metadata, chunk_text, bge_tokenizer, table_context
                ),
                cl100k_tokens=len(cl100k_encoder.encode(chunk_text)),
                table_header_start=table_header_start,
                table_context=table_context,
                subsection_context=subsection_context,
                subsection_contexts=subsection_contexts,
            )
        )
        if end >= len(text):
            break
        if ended_before_new_table:
            start = end
            continue
        next_start = end
        possible_starts = [boundary for boundary in preferred if start < boundary < end]
        for overlap_start in reversed(possible_starts):
            overlap_text = text[overlap_start:end]
            if not overlap_text.strip():
                continue
            if len(cl100k_encoder.encode(overlap_text)) <= OVERLAP_BGE_TOKENS:
                next_start = overlap_start
                break
        start = next_start

    # A greedy split can leave a tiny bridge or final tail. Expand only that
    # small chunk backward into safe overlap. This keeps every source token and
    # avoids moving or cutting the prior sentence/table row.
    for chunk_index, small_chunk in enumerate(chunks):
        if chunk_index == 0 or small_chunk.cl100k_tokens >= SHORT_EVIDENCE_MIN_TOKENS:
            continue
        # Expanding at or past the previous chunk's start would make this chunk
        # a superset of it, which is the same redundancy in the other direction.
        previous_start = chunks[chunk_index - 1].source_start
        for overlap_start in reversed(
            [
                boundary
                for boundary in fallback
                if previous_start < boundary < small_chunk.source_start
            ]
        ):
            expanded_text = text[overlap_start : small_chunk.source_end]
            cl100k_tokens = len(cl100k_encoder.encode(expanded_text))
            if cl100k_tokens < SHORT_EVIDENCE_MIN_TOKENS:
                continue
            table_header_start, table_context = _table_context_at(overlap_start, table_runs)
            table_context = _credible_table_context(table_context, bge_tokenizer)
            if not table_context:
                table_header_start = None
            bge_tokens = final_bge_token_count(
                metadata_with_subsection(
                    metadata, subsection_spans, overlap_start, small_chunk.source_end
                )[0],
                expanded_text,
                bge_tokenizer,
                table_context,
            )
            if bge_tokens > BGE_INPUT_LIMIT:
                continue
            chunks[chunk_index] = CandidateChunk(
                text=expanded_text,
                source_start=overlap_start,
                source_end=small_chunk.source_end,
                bge_tokens=bge_tokens,
                cl100k_tokens=cl100k_tokens,
                table_header_start=table_header_start,
                table_context=table_context,
                subsection_context=metadata_with_subsection(
                    metadata, subsection_spans, overlap_start, small_chunk.source_end
                )[1],
                subsection_contexts=metadata_with_subsection(
                    metadata, subsection_spans, overlap_start, small_chunk.source_end
                )[2],
            )
            break
    return chunks


def validate_v3_tiling(text: str, chunks: list[CandidateChunk]) -> list[str]:
    failures: list[str] = []
    if not chunks:
        return ["no_chunks"] if text else []
    if chunks[0].source_start != 0 or chunks[-1].source_end != len(text):
        failures.append("outer_bounds")
    for previous, current in zip(chunks, chunks[1:]):
        if current.source_start > previous.source_end:
            failures.append("gap")
        # Neither direction of containment may survive: a chunk whose span sits
        # inside its neighbour's is a duplicate embedding, not new evidence.
        if current.source_end <= previous.source_end:
            failures.append("contained_chunk")
        if current.source_start <= previous.source_start:
            failures.append("contains_previous_chunk")
    for chunk in chunks:
        if text[chunk.source_start : chunk.source_end] != chunk.text:
            failures.append("source_text_mismatch")
        if chunk.bge_tokens > BGE_INPUT_LIMIT:
            failures.append("bge_limit")
    return failures


def chunk_token_ranges(total_tokens: int) -> list[tuple[int, int]]:
    if total_tokens < MIN_CHUNK_TOKENS:
        return []

    if total_tokens <= MAX_CHUNK_TOKENS:
        return [(0, total_tokens)]

    ranges: list[tuple[int, int]] = []
    start = 0

    while start < total_tokens:
        end = min(start + CHUNK_SIZE, total_tokens)
        remaining = total_tokens - end

        if 0 < remaining < MIN_CHUNK_TOKENS and total_tokens - start <= MAX_CHUNK_TOKENS:
            end = total_tokens

        if end - start >= MIN_CHUNK_TOKENS:
            ranges.append((start, end))

        if end >= total_tokens:
            break

        start = max(end - OVERLAP, start + 1)

    return ranges


def chunk_tokens(tokens: list[int]) -> list[list[int]]:
    return [tokens[start:end] for start, end in chunk_token_ranges(len(tokens))]


def source_aligned_chunk_text(
    text: str,
    token_offsets: list[int],
    token_start: int,
    token_end: int,
) -> tuple[str, int | None, int | None]:
    if not token_offsets or token_start >= len(token_offsets):
        return "", None, None

    char_start = token_offsets[token_start]
    char_end = len(text) if token_end >= len(token_offsets) else token_offsets[token_end]
    char_start = max(0, min(char_start, len(text)))
    char_end = max(char_start, min(char_end, len(text)))

    raw_text = text[char_start:char_end]
    leading_trim = len(raw_text) - len(raw_text.lstrip())
    trailing_trim = len(raw_text) - len(raw_text.rstrip())
    local_start = char_start + leading_trim
    local_end = char_end - trailing_trim
    if local_end <= local_start:
        return "", None, None

    return text[local_start:local_end], local_start, local_end


def bounded_source_aligned_chunk_text(
    text: str,
    token_offsets: list[int],
    token_start: int,
    token_end: int,
    encoder,
) -> tuple[str, int | None, int | None, int]:
    """Trim within the overlap when a source slice retokenizes larger."""
    minimum_end = max(token_start + MIN_CHUNK_TOKENS, token_end - OVERLAP)
    for candidate_end in range(token_end, minimum_end - 1, -1):
        chunk_text, local_start, local_end = source_aligned_chunk_text(
            text,
            token_offsets,
            token_start,
            candidate_end,
        )
        token_count = len(encoder.encode(chunk_text)) if chunk_text else 0
        if MIN_CHUNK_TOKENS <= token_count <= MAX_CHUNK_TOKENS:
            return chunk_text, local_start, local_end, token_count
    raise ValueError(
        "source-aligned chunk cannot be bounded without exceeding the overlap"
    )


def parse_section_filename(section_file: Path) -> tuple[str, str] | None:
    """Return ``(pdf_stem, section_instance_id)`` for v2 and legacy files."""
    if "__" not in section_file.stem:
        return None

    v2_match = re.match(
        r"^(?P<pdf_stem>.+)__(?P<section_code>[a-z][a-z0-9_]*)__"
        r"(?P<ordinal>\d{4})$",
        section_file.stem,
    )
    if v2_match:
        pdf_stem = v2_match.group("pdf_stem")
        section_instance_id = (
            f"{v2_match.group('section_code')}__{v2_match.group('ordinal')}"
        )
        return pdf_stem, section_instance_id

    pdf_stem, section_instance_id = section_file.stem.rsplit("__", 1)
    if not pdf_stem or not section_instance_id:
        return None
    return pdf_stem, section_instance_id


def read_section_source(section_file: Path) -> tuple[str, SourceFingerprint]:
    """Read one section and make the fingerprint refer to those exact bytes."""
    before = section_file.stat()
    payload = section_file.read_bytes()
    after = section_file.stat()
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise RuntimeError(f"section input changed while being read: {section_file}")

    return (
        payload.decode("utf-8", errors="replace").strip(),
        SourceFingerprint(
            size_bytes=after.st_size,
            mtime_utc=datetime.fromtimestamp(
                after.st_mtime, tz=timezone.utc
            ).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            sha256=hashlib.sha256(payload).hexdigest(),
        ),
    )


def read_parsed_text_source(
    parsed_text_file: str | None,
    cache: dict[Path, tuple[str, str]],
) -> tuple[str | None, str]:
    path = resolve_path(parsed_text_file)
    if path is None or not path.is_file():
        return None, ""
    resolved = path.resolve()
    cached = cache.get(resolved)
    if cached is not None:
        return cached
    payload = resolved.read_bytes()
    value = (
        resolved.read_text(encoding="utf-8", errors="replace"),
        hashlib.sha256(payload).hexdigest(),
    )
    cache[resolved] = value
    return value


def atomic_write_text(path: Path, text: str) -> None:
    """Write one output through a sibling temporary file before replacing it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def section_chunk_files(
    ticker_out: Path,
    pdf_stem: str,
    section_instance_id: str,
) -> list[Path]:
    if not ticker_out.exists():
        return []
    prefix = f"{pdf_stem}__{section_instance_id}__chunk_"
    return sorted(
        path
        for path in ticker_out.glob("*.txt")
        if path.name.startswith(prefix)
    )


def section_marker_path(
    output_root: Path,
    ticker: str,
    pdf_stem: str,
    section_instance_id: str,
) -> Path:
    return output_root / ticker / f".{pdf_stem}__{section_instance_id}.inprogress"


def start_section_update(marker_path: Path, fingerprint: SourceFingerprint) -> None:
    """Persist an in-progress marker so an interrupted force run is resumed safely."""
    atomic_write_text(
        marker_path,
        "\n".join(
            [
                f"source_sha256={fingerprint.sha256}",
                f"started_at={datetime.now(timezone.utc).isoformat()}",
                "",
            ]
        ),
    )


def start_orphan_section_cleanup(marker_path: Path) -> None:
    """Mark targeted cleanup for a section removed by the splitter.

    A source PDF can be re-split into a different set of section instances. This
    marker makes removal of the now-orphaned *one section* restart-safe without
    ever clearing an entire ticker's chunks.
    """
    atomic_write_text(
        marker_path,
        "\n".join(
            [
                "operation=orphan_section_cleanup",
                f"started_at={datetime.now(timezone.utc).isoformat()}",
                "",
            ]
        ),
    )


def clear_section_marker(marker_path: Path) -> None:
    try:
        marker_path.unlink()
    except FileNotFoundError:
        pass


def validate_chunk_citation(
    *,
    parsed_text: str | None,
    parsed_text_sha256: str,
    expected_parsed_text_sha256: str,
    section_text: str,
    section_start: int | None,
    section_end: int | None,
    chunk_text: str,
    local_start: int | None,
    local_end: int | None,
    page_spans: list[dict],
) -> dict[str, str | int | None]:
    """Validate the complete chunk against its declared parsed-text source slice."""
    result: dict[str, str | int | None] = {
        "source_start": None,
        "source_end": None,
        "page_start": "",
        "page_end": "",
        "status": "missing_parsed_text",
    }
    if parsed_text is None:
        return result
    if (
        expected_parsed_text_sha256
        and parsed_text_sha256 != expected_parsed_text_sha256
    ):
        result["status"] = "parsed_text_fingerprint_mismatch"
        return result
    if (
        section_start is None
        or section_end is None
        or section_start < 0
        or section_end <= section_start
        or section_end > len(parsed_text)
    ):
        result["status"] = "invalid_section_bounds"
        return result

    declared_section = parsed_text[section_start:section_end]
    section_exact = declared_section == section_text
    section_normalized = normalized_text(declared_section) == normalized_text(section_text)
    if not section_normalized:
        result["status"] = "section_source_mismatch"
        return result
    if (
        local_start is None
        or local_end is None
        or local_start < 0
        or local_end <= local_start
        or local_end > len(section_text)
    ):
        result["status"] = "chunk_not_found_in_section"
        return result

    local_slice = section_text[local_start:local_end]
    chunk_exact = local_slice == chunk_text
    chunk_normalized = normalized_text(local_slice) == normalized_text(chunk_text)
    if not chunk_normalized:
        result["status"] = "chunk_source_mismatch"
        return result

    source_start = section_start + local_start
    source_end = section_start + local_end
    if source_end > section_end:
        result["status"] = "chunk_outside_section"
        return result
    parsed_slice = parsed_text[source_start:source_end]
    parsed_exact = parsed_slice == chunk_text
    parsed_normalized = normalized_text(parsed_slice) == normalized_text(chunk_text)
    if not parsed_normalized:
        result["status"] = "chunk_source_mismatch"
        return result

    page_start, page_end = pages_for_span(page_spans, source_start, source_end)
    result.update(
        {
            "source_start": source_start,
            "source_end": source_end,
            "page_start": page_start,
            "page_end": page_end,
        }
    )
    if not page_start or not page_end:
        result["status"] = "missing_page_mapping"
    elif section_exact and chunk_exact and parsed_exact:
        result["status"] = "verified_exact"
    else:
        result["status"] = "verified_whitespace_normalized"
    return result


# Tier thresholds. Loose on purpose: they exist to separate obviously-weak
# evidence from ordinary prose, not to make fine distinctions, and should be
# tuned against a retrieval benchmark rather than by argument.
TIER_FIGURE_PAGE_SHARE = 0.60      # pages where empty regions dominate
TIER_UNPLACED_SHARE = 0.15         # words that landed in no region
TIER_NOISE_MIN_TOKENS = 60


def _page_rows_for_span(
    page_spans: list[dict], start: int | None, end: int | None
) -> list[dict]:
    """Page-map rows overlapping a chunk's character span."""
    if start is None or end is None:
        return []
    out = []
    for row in page_spans:
        ps = parse_int(row.get("char_start"))
        pe = parse_int(row.get("char_end"))
        if ps is None or pe is None:
            continue
        if pe > start and ps < end:
            out.append(row)
    return out


def classify_retrieval_tier(
    chunk_text: str,
    token_count: int,
    chunk_type: str,
    table_context: str,
    page_rows: list[dict],
) -> str:
    """Deterministic quality tier for one chunk.

    Order matters: noise is checked first so a tiny fragment on a table page
    is not promoted to layout_sensitive, which would overstate its evidence
    value.
    """
    has_table_rows = any(
        line.strip().startswith("|") and line.strip().endswith("|")
        for line in chunk_text.splitlines()
    )

    figure_pages = 0
    unplaced = 0
    chars = 0
    tables_on_page = 0
    for row in page_rows:
        pics = parse_int(row.get("picture_region_count")) or 0
        empty = parse_int(row.get("empty_region_count")) or 0
        extracted = parse_int(row.get("extracted_char_count")) or 0
        unplaced += parse_int(row.get("unplaced_char_count")) or 0
        chars += extracted
        tables_on_page += parse_int(row.get("table_candidate_count")) or 0
        if pics and empty >= pics:
            figure_pages += 1

    figure_share = figure_pages / len(page_rows) if page_rows else 0.0
    unplaced_share = unplaced / chars if chars else 0.0

    # Noise is judged on the CHUNK, not the page around it. An earlier version
    # demoted anything from a figure-heavy page, which marked a 411-token
    # passage on minerals due diligence as noise because the page it sat on
    # was mostly pictures. The page tells you what surrounded the text, not
    # whether the text is evidence.
    prose_lines = [l for l in chunk_text.splitlines() if len(l.split()) >= 6]
    if chunk_type == CHUNK_TYPE_SHORT_EVIDENCE or token_count < TIER_NOISE_MIN_TOKENS:
        return "noise"
    if not prose_lines and not has_table_rows:
        return "noise"
    if has_table_rows or table_context.strip() or tables_on_page:
        return "layout_sensitive"
    if unplaced_share >= TIER_UNPLACED_SHARE:
        return "layout_sensitive"
    return "narrative"


def build_chunk_output(
    *,
    ticker: str,
    pdf_stem: str,
    section_code: str,
    section_instance_id: str,
    section_file: Path,
    output_root: Path,
    chunk_index: int,
    chunk_text: str,
    token_count: int,
    source_fingerprint: SourceFingerprint,
    parsed_text: str | None,
    parsed_text_sha256: str,
    expected_parsed_text_sha256: str,
    section_text: str,
    section_source_start: int | None,
    section_source_end: int | None,
    local_start: int | None,
    local_end: int | None,
    page_spans: list[dict],
    doc_meta: dict,
    chunk_type: str = CHUNK_TYPE_NORMAL,
    short_section_action: str = "",
    short_section_reason: str = "",
    merged_section_ids: str = "",
    table_context: str = "",
    physical_section_title: str = "",
    subsection_context: str = "",
    subsection_contexts: tuple[str, ...] = (),
    embedding_metadata: dict | None = None,
) -> ChunkOutput:
    citation = validate_chunk_citation(
        parsed_text=parsed_text,
        parsed_text_sha256=parsed_text_sha256,
        expected_parsed_text_sha256=expected_parsed_text_sha256,
        section_text=section_text,
        section_start=section_source_start,
        section_end=section_source_end,
        chunk_text=chunk_text,
        local_start=local_start,
        local_end=local_end,
        page_spans=page_spans,
    )
    source_start = citation["source_start"]
    source_end = citation["source_end"]
    page_start = citation["page_start"]
    page_end = citation["page_end"]
    citation_status = str(citation["status"])
    # A chunk with no page range cannot support a citation, whatever the text
    # match says. This happens when a document has no row in the parse index,
    # so no page map reaches the chunker: TGT-2019 produced 185 chunks marked
    # citation_ready=true with page_start empty. Claiming citability you cannot
    # honour is worse than the missing data itself.
    citation_ready = (
        citation_status in VERIFIED_CITATION_STATUSES
        and page_start not in (None, "")
        and page_end not in (None, "")
    )

    # The caller already assembled everything final_embedding_text needs:
    # company name from the manifest, report year via extract_report_year with
    # its multi-year span handling, canonical ticker. Rebuilding it from
    # doc_meta here produced 'Company: unknown / Reporting year: unknown' on
    # every chunk, because doc_meta carries neither.
    retrieval_tier = classify_retrieval_tier(
        chunk_text,
        token_count,
        chunk_type,
        table_context,
        _page_rows_for_span(page_spans, source_start, source_end),
    )

    meta = dict(embedding_metadata or doc_meta)
    meta.setdefault("ticker", ticker)
    meta["section_code"] = section_code
    if subsection_context:
        meta["section_title_original"] = subsection_context
    embedding_text = final_embedding_text(meta, chunk_text, table_context)
    dataset_id = os.environ.get("ESG_DATASET_ID", DEFAULT_DATASET_ID)

    # Anything over the window is silently truncated at embedding time, and a
    # truncated vector looks fine until answers start missing the end of a
    # chunk. Fail here instead. Measured headroom is thin: median 466 tokens,
    # max 502 against a 512 limit.
    tokenizer_for_guard = _WORKER_BGE_TOKENIZER
    if tokenizer_for_guard is not None:
        embed_tokens = len(
            tokenizer_for_guard.encode(embedding_text, add_special_tokens=True, truncation=False)
        )
        if embed_tokens > BGE_MAX_INPUT_TOKENS:
            raise ValueError(
                f"embedding_text is {embed_tokens} tokens, over the "
                f"{BGE_MAX_INPUT_TOKENS}-token window "
                f"({ticker} {pdf_stem} {section_instance_id} chunk {chunk_index}). "
                "Lower MAX_CHUNK_TOKENS or shorten the embedding header."
            )

    doc_type = doc_meta.get("doc_type") or DOC_TYPE
    quality_status = doc_meta.get("doc_quality_status") or "needs_review"
    rag_action = doc_meta.get("rag_action") or rag_action_for_status(quality_status)

    # A chunk built entirely from contents listings, part-title dividers or a
    # cover has no subject matter to retrieve. Unlike a wrong topic code this
    # corrupts the embedding itself: the vector ends up describing the report's
    # structure rather than anything discussed in it. Casey's table of contents
    # was indexed as 'environmental', URBN's contents page as 'human_capital'.
    # Chunks mixing furniture with a content page are kept -- they carry prose.
    span_roles, furniture_share = page_roles_for_span(
        page_spans, source_start, source_end
    )
    page_role_value = "|".join(sorted(set(span_roles)))
    if span_roles and furniture_share >= MAX_FURNITURE_CHAR_SHARE:
        rag_action = "exclude_from_esg_index"
    source_id = doc_meta.get("source_id") or _default_source_id(ticker, pdf_stem)
    source_version_id = doc_meta.get("source_version_id") or f"{source_id}__unknown"
    chunk_id = f"{source_id}__{section_instance_id}__chunk_{chunk_index:04d}"
    ticker_out = output_root / ticker
    chunk_file = (
        ticker_out
        / f"{pdf_stem}__{section_instance_id}__chunk_{chunk_index:04d}.txt"
    )
    row = {
        "chunk_id": chunk_id,
        "source_id": source_id,
        "source_version_id": source_version_id,
        "ticker": ticker,
        "canonical_ticker": doc_meta.get("canonical_ticker") or ticker,
        "doc_type": doc_type,
        "source_type": doc_meta.get("source_type") or doc_type,
        "source_scope": doc_meta.get("source_scope") or "full_report",
        "retrieval_tier": retrieval_tier,
        # Inclusion follows the action. The default was an unconditional true,
        # so a document with no parse-index row -- which defaults its quality
        # status to needs_review -- emitted rag_action=
        # manual_review_before_indexing alongside include_in_esg_index=true.
        # TGT-2019 produced 203 such chunks, eligible for the retrieval corpus
        # while flagged as unreviewed.
        "include_in_esg_index": (
            "true"
            if doc_meta.get("include_in_esg_index", True) and rag_action == "index_as_esg"
            else "false"
        ),
        "duplicate_of_source_id": doc_meta.get("duplicate_of_source_id") or "",
        "doc_quality_status": quality_status,
        "rag_action": rag_action,
        "page_role": page_role_value,
        "quality_flags": doc_meta.get("quality_flags") or "",
        "pdf_stem": pdf_stem,
        "section_code": section_code,
        "section_instance_id": section_instance_id,
        "physical_section_title": physical_section_title,
        "subsection_context": subsection_context or physical_section_title,
        "subsection_contexts_json": json.dumps(
            list(subsection_contexts or ((subsection_context or physical_section_title),)),
            ensure_ascii=False,
            separators=(",", ":"),
        ) if (subsection_context or physical_section_title) else "[]",
        "chunk_index": chunk_index,
        "chunk_type": chunk_type,
        "short_section_action": short_section_action,
        "short_section_reason": short_section_reason,
        "merged_section_ids": merged_section_ids,
        "table_context": table_context,
        "token_count": token_count,
        "char_count": len(chunk_text),
        "chunk_file": display_path(chunk_file),
        "source_section_file": display_path(section_file),
        **source_fingerprint.as_index_fields(),
        "parsed_text_sha256": parsed_text_sha256,
        "section_text_sha256": source_fingerprint.sha256,
        # Identity of this chunk's own citation text. The section and parsed-text
        # hashes above cover its ancestors, not the chunk itself, which the ESG
        # Chunk Management Contract requires recalculated after any text change.
        "chunk_text_sha256": hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
        # The exact string handed to the embedding model, and its own identity.
        # Kept separate from chunk_text so a change to the context header does
        # not alter citations, and so a stale vector is detectable by hash.
        "embedding_text": embedding_text,
        "embedding_text_sha256": hashlib.sha256(embedding_text.encode("utf-8")).hexdigest(),
        "dataset_id": dataset_id,
        "source_start_char": source_start if source_start is not None else "",
        "source_end_char": source_end if source_end is not None else "",
        "page_start": page_start,
        "page_end": page_end,
        "citation_ready": "true" if citation_ready else "false",
        "citation_validation_status": citation_status,
        "citation_validation_version": CITATION_VALIDATION_VERSION,
    }
    return ChunkOutput(path=chunk_file, text=chunk_text, row=row)


def build_section_plan(
    section_file: Path,
    output_root: Path,
    encoder,
    section_metadata: dict[tuple[str, str, str], dict],
    doc_metadata: dict[tuple[str, str], dict],
    parsed_text_cache: dict[Path, tuple[str, str]] | None = None,
    page_map_cache: dict[Path, list[dict]] | None = None,
    bge_tokenizer=None,
    company_names: dict[str, str] | None = None,
) -> SectionPlan:
    ticker = section_file.parent.name.upper()
    parsed_name = parse_section_filename(section_file)
    if parsed_name is None:
        raise ValueError(f"bad filename: {section_file}")

    pdf_stem, section_instance_id = parsed_name
    section_meta = section_metadata.get(
        (ticker, pdf_stem, section_instance_id), {}
    )
    section_code = (
        section_meta.get("section_code")
        or section_instance_id.rsplit("__", 1)[0]
    ).strip()
    physical_section_title = (section_meta.get("section_title") or "").strip()
    has_subsection_span_metadata = bool(
        (section_meta.get("subsection_spans_json") or "").strip()
    )
    subsection_fallback = (
        "unknown" if has_subsection_span_metadata else physical_section_title
    )
    doc_meta = apply_section_hold(doc_metadata.get((ticker, pdf_stem), {}), section_meta)
    embedding_metadata = None
    if bge_tokenizer is not None:
        year, year_status, year_span = extract_report_year(pdf_stem)
        canonical_ticker = (doc_meta.get("canonical_ticker") or ticker).strip().upper()
        embedding_metadata = {
            **doc_meta,
            **section_meta,
            "ticker": ticker,
            "canonical_ticker": canonical_ticker,
            "company_name": (company_names or {}).get(canonical_ticker, ""),
            "report_year": "" if year is None else str(year),
            "report_year_status": year_status,
            "report_year_span": year_span,
            "section_code": section_code,
            "physical_section_title": physical_section_title,
            "section_title_original": physical_section_title,
            "subsection_fallback": subsection_fallback,
        }
    text, source_fingerprint = read_section_source(section_file)
    subsection_spans = parse_subsection_spans(section_meta, len(text))
    tokens = encoder.encode(text)
    source_token_count = len(tokens)
    page_spans = read_page_map_cached(
        doc_meta.get("page_map_file"),
        page_map_cache,
    )
    section_source_start = parse_int(section_meta.get("source_start_char"))
    section_source_end = parse_int(section_meta.get("source_end_char"))
    parsed_text_cache = parsed_text_cache if parsed_text_cache is not None else {}
    parsed_text, parsed_text_sha256 = read_parsed_text_source(
        doc_meta.get("parsed_text_file"), parsed_text_cache
    )
    expected_parsed_text_sha256 = (
        section_meta.get("source_sha256") or ""
    ).strip()

    def chunk_subsection(start: int, end: int) -> tuple[str, tuple[str, ...]]:
        return subsection_context_for_range(
            subsection_spans, start, end, subsection_fallback
        )

    whole_section_embedding_metadata = embedding_metadata
    if embedding_metadata is not None:
        whole_section_embedding_metadata = metadata_with_subsection(
            embedding_metadata, subsection_spans, 0, len(text)
        )[0]

    navigation_reason = classify_navigation_trace_section(text, source_token_count)
    v3_short_action = ""
    v3_short_reason = ""
    # A large table of contents is chunked normally -- splitting it is harmless
    # and keeps every chunk inside the token bounds the validators enforce --
    # but every chunk it produces is marked excluded, so none reaches the index.
    if is_large_navigation_trace_section(text, source_token_count):
        doc_meta = doc_meta_for_excluded_short_section(
            doc_meta,
            "table_of_contents_or_navigation",
        )
    navigation_output_token_count = (
        final_bge_token_count(whole_section_embedding_metadata, text, bge_tokenizer)
        if navigation_reason and embedding_metadata is not None
        else source_token_count
    )
    if (
        MIN_CHUNK_TOKENS <= source_token_count <= NAVIGATION_TRACE_MAX_TOKENS
        and navigation_reason
        and navigation_output_token_count <= BGE_INPUT_LIMIT
    ):
        subsection_context, subsection_contexts = chunk_subsection(0, len(text))
        output_token_count = navigation_output_token_count
        outputs = [
            build_chunk_output(
                ticker=ticker,
                pdf_stem=pdf_stem,
                section_code=section_code,
                section_instance_id=section_instance_id,
                section_file=section_file,
                output_root=output_root,
                chunk_index=0,
                chunk_text=text,
                token_count=output_token_count,
                source_fingerprint=source_fingerprint,
                parsed_text=parsed_text,
                parsed_text_sha256=parsed_text_sha256,
                expected_parsed_text_sha256=expected_parsed_text_sha256,
                section_text=text,
                section_source_start=section_source_start,
                section_source_end=section_source_end,
                local_start=0,
                local_end=len(text),
                page_spans=page_spans,
                doc_meta=doc_meta_for_excluded_short_section(
                    doc_meta,
                    navigation_reason,
                ),
                chunk_type=(
                    CHUNK_TYPE_SHORT_EVIDENCE
                    if output_token_count < MIN_CHUNK_TOKENS
                    else CHUNK_TYPE_NORMAL
                ),
                short_section_action=SHORT_SECTION_ACTION_EXCLUDED,
                short_section_reason=navigation_reason,
                physical_section_title=physical_section_title,
                subsection_context=subsection_context,
                subsection_contexts=subsection_contexts,
                embedding_metadata=embedding_metadata,
            )
        ]
        return SectionPlan(
            ticker=ticker,
            pdf_stem=pdf_stem,
            section_code=section_code,
            section_instance_id=section_instance_id,
            source_file=section_file,
            source_fingerprint=source_fingerprint,
            source_token_count=source_token_count,
            outputs=outputs,
            short_section_action=SHORT_SECTION_ACTION_EXCLUDED,
            short_section_reason=navigation_reason,
        )

    if (
        MIN_CHUNK_TOKENS <= source_token_count <= NAVIGATION_TRACE_MAX_TOKENS
        and navigation_reason
    ):
        # Dot leaders can make a short cl100k navigation trace exceed the BGE
        # limit. Split it with v3, but keep every resulting chunk excluded.
        doc_meta = doc_meta_for_excluded_short_section(doc_meta, navigation_reason)
        v3_short_action = SHORT_SECTION_ACTION_EXCLUDED
        v3_short_reason = navigation_reason

    short_output_token_count = (
        final_bge_token_count(whole_section_embedding_metadata, text, bge_tokenizer)
        if embedding_metadata is not None
        else source_token_count
    )
    short_bge_overflow = (
        SHORT_EVIDENCE_MIN_TOKENS <= source_token_count < MIN_CHUNK_TOKENS
        and short_output_token_count > BGE_INPUT_LIMIT
    )
    if short_bge_overflow:
        v3_short_action, v3_short_reason = classify_short_section(
            text, source_token_count
        )
        if v3_short_action == SHORT_SECTION_ACTION_EXCLUDED:
            doc_meta = doc_meta_for_excluded_short_section(
                doc_meta, v3_short_reason
            )

    if source_token_count < MIN_CHUNK_TOKENS and not short_bge_overflow:
        short_action, short_reason = classify_short_section(text, source_token_count)
        outputs: list[ChunkOutput] = []
        if source_token_count >= SHORT_EVIDENCE_MIN_TOKENS and text:
            subsection_context, subsection_contexts = chunk_subsection(0, len(text))
            output_token_count = short_output_token_count
            chunk_doc_meta = doc_meta
            if short_action == SHORT_SECTION_ACTION_EXCLUDED:
                chunk_doc_meta = doc_meta_for_excluded_short_section(
                    doc_meta,
                    short_reason,
                )
            outputs.append(
                build_chunk_output(
                    ticker=ticker,
                    pdf_stem=pdf_stem,
                    section_code=section_code,
                    section_instance_id=section_instance_id,
                    section_file=section_file,
                    output_root=output_root,
                    chunk_index=0,
                    chunk_text=text,
                    token_count=output_token_count,
                    source_fingerprint=source_fingerprint,
                    parsed_text=parsed_text,
                    parsed_text_sha256=parsed_text_sha256,
                    expected_parsed_text_sha256=expected_parsed_text_sha256,
                    section_text=text,
                    section_source_start=section_source_start,
                    section_source_end=section_source_end,
                    local_start=0,
                    local_end=len(text),
                    page_spans=page_spans,
                    doc_meta=chunk_doc_meta,
                    chunk_type=(
                        CHUNK_TYPE_SHORT_EVIDENCE
                        if output_token_count < MIN_CHUNK_TOKENS
                        else CHUNK_TYPE_NORMAL
                    ),
                    short_section_action=short_action,
                    short_section_reason=short_reason,
                    physical_section_title=physical_section_title,
                    subsection_context=subsection_context,
                    subsection_contexts=subsection_contexts,
                    embedding_metadata=embedding_metadata,
                )
            )
        return SectionPlan(
            ticker=ticker,
            pdf_stem=pdf_stem,
            section_code=section_code,
            section_instance_id=section_instance_id,
            source_file=section_file,
            source_fingerprint=source_fingerprint,
            source_token_count=source_token_count,
            outputs=outputs,
            short_section_action=short_action,
            short_section_reason=short_reason,
        )

    if bge_tokenizer is not None:
        candidate_chunks = chunk_section_v3(
            text, embedding_metadata, bge_tokenizer, encoder, subsection_spans
        )
        failures = validate_v3_tiling(text, candidate_chunks)
        if failures:
            raise ValueError(f"v3 invariant failure ({'|'.join(failures)}): {section_file}")
        outputs = []
        for chunk_index, chunk in enumerate(candidate_chunks):
            if chunk.bge_tokens < SHORT_EVIDENCE_MIN_TOKENS:
                raise ValueError(
                    f"v3 chunk below {SHORT_EVIDENCE_MIN_TOKENS} BGE tokens: {section_file}"
                )
            chunk_type = (
                CHUNK_TYPE_SHORT_EVIDENCE
                if chunk.bge_tokens < MIN_CHUNK_TOKENS
                else CHUNK_TYPE_NORMAL
            )
            retrieval_reason = retrieval_chunk_exclusion_reason(
                chunk.text, chunk.cl100k_tokens
            )
            chunk_doc_meta = (
                doc_meta_for_excluded_retrieval_content(doc_meta, retrieval_reason)
                if retrieval_reason
                else doc_meta
            )
            outputs.append(
                build_chunk_output(
                    ticker=ticker,
                    pdf_stem=pdf_stem,
                    section_code=section_code,
                    section_instance_id=section_instance_id,
                    section_file=section_file,
                    output_root=output_root,
                    chunk_index=chunk_index,
                    chunk_text=chunk.text,
                    token_count=chunk.bge_tokens,
                    source_fingerprint=source_fingerprint,
                    parsed_text=parsed_text,
                    parsed_text_sha256=parsed_text_sha256,
                    expected_parsed_text_sha256=expected_parsed_text_sha256,
                    section_text=text,
                    section_source_start=section_source_start,
                    section_source_end=section_source_end,
                    local_start=chunk.source_start,
                    local_end=chunk.source_end,
                    page_spans=page_spans,
                    doc_meta=chunk_doc_meta,
                    chunk_type=chunk_type,
                    short_section_action=(
                        SHORT_SECTION_ACTION_EXCLUDED
                        if retrieval_reason
                        else v3_short_action
                    ),
                    short_section_reason=retrieval_reason or v3_short_reason,
                    table_context=chunk.table_context,
                    physical_section_title=physical_section_title,
                    subsection_context=chunk.subsection_context,
                    subsection_contexts=chunk.subsection_contexts,
                    embedding_metadata=embedding_metadata,
                )
            )
        return SectionPlan(
            ticker=ticker,
            pdf_stem=pdf_stem,
            section_code=section_code,
            section_instance_id=section_instance_id,
            source_file=section_file,
            source_fingerprint=source_fingerprint,
            source_token_count=source_token_count,
            outputs=outputs,
            short_section_action=v3_short_action,
            short_section_reason=v3_short_reason,
        )

    token_ranges = chunk_token_ranges(source_token_count)
    if not token_ranges:
        raise ValueError(f"no valid chunks created for non-short section: {section_file}")
    decoded_text, token_offsets = encoder.decode_with_offsets(tokens)
    if decoded_text != text or len(token_offsets) != len(tokens):
        token_offsets = []

    outputs: list[ChunkOutput] = []
    search_pos = 0

    for chunk_index, (token_start, token_end) in enumerate(token_ranges):
        range_token_count = token_end - token_start
        if range_token_count < MIN_CHUNK_TOKENS or range_token_count > MAX_CHUNK_TOKENS:
            raise ValueError(
                f"invalid chunk size ({range_token_count} tokens): {section_file}"
            )

        if token_offsets:
            chunk_text, local_start, local_end, token_count = bounded_source_aligned_chunk_text(
                text,
                token_offsets,
                token_start,
                token_end,
                encoder,
            )
        else:
            chunk_text = encoder.decode(tokens[token_start:token_end]).strip()
            local_start, local_end = locate_text_span(text, chunk_text, search_pos)
            token_count = len(encoder.encode(chunk_text))
        if not chunk_text:
            raise ValueError(f"empty chunk created for: {section_file}")
        if token_count < MIN_CHUNK_TOKENS or token_count > MAX_CHUNK_TOKENS:
            raise ValueError(
                f"invalid source-aligned chunk size ({token_count} tokens): {section_file}"
            )
        if local_start is not None and local_end is not None:
            search_pos = max(search_pos, local_start + 1)

        subsection_context, subsection_contexts = chunk_subsection(
            local_start or 0, local_end or len(text)
        )

        outputs.append(
            build_chunk_output(
                ticker=ticker,
                pdf_stem=pdf_stem,
                section_code=section_code,
                section_instance_id=section_instance_id,
                section_file=section_file,
                output_root=output_root,
                chunk_index=chunk_index,
                chunk_text=chunk_text,
                token_count=token_count,
                source_fingerprint=source_fingerprint,
                parsed_text=parsed_text,
                parsed_text_sha256=parsed_text_sha256,
                expected_parsed_text_sha256=expected_parsed_text_sha256,
                section_text=text,
                section_source_start=section_source_start,
                section_source_end=section_source_end,
                local_start=local_start,
                local_end=local_end,
                page_spans=page_spans,
                doc_meta=doc_meta,
                physical_section_title=physical_section_title,
                subsection_context=subsection_context,
                subsection_contexts=subsection_contexts,
                embedding_metadata=embedding_metadata,
            )
        )

    return SectionPlan(
        ticker=ticker,
        pdf_stem=pdf_stem,
        section_code=section_code,
        section_instance_id=section_instance_id,
        source_file=section_file,
        source_fingerprint=source_fingerprint,
        source_token_count=source_token_count,
        outputs=outputs,
    )


def init_plan_worker(
    output_root: str,
    section_metadata: dict[tuple[str, str, str], dict],
    doc_metadata: dict[tuple[str, str], dict],
    index_rows_by_section: dict[tuple[str, str, str], list[dict]],
    artifact_keys: set[tuple[str, str, str]],
    bge_tokenizer_path: str | None = None,
    company_names: dict[str, str] | None = None,
) -> None:
    """Initialize one worker with the immutable resume-validation snapshot."""
    global _WORKER_OUTPUT_ROOT
    global _WORKER_SECTION_METADATA
    global _WORKER_DOC_METADATA
    global _WORKER_PARSED_TEXT_CACHE
    global _WORKER_PAGE_MAP_CACHE
    global _WORKER_INDEX_ROWS_BY_SECTION
    global _WORKER_ARTIFACT_KEYS
    global _WORKER_ENCODER
    global _WORKER_BGE_TOKENIZER
    global _WORKER_COMPANY_NAMES

    _WORKER_OUTPUT_ROOT = Path(output_root)
    _WORKER_SECTION_METADATA = section_metadata
    _WORKER_DOC_METADATA = doc_metadata
    _WORKER_PARSED_TEXT_CACHE = {}
    _WORKER_PAGE_MAP_CACHE = {}
    _WORKER_INDEX_ROWS_BY_SECTION = index_rows_by_section
    _WORKER_ARTIFACT_KEYS = artifact_keys
    _WORKER_ENCODER = tiktoken.get_encoding(ENCODING)
    _WORKER_COMPANY_NAMES = company_names or {}
    _WORKER_BGE_TOKENIZER = None
    if bge_tokenizer_path:
        from transformers import AutoTokenizer

        _WORKER_BGE_TOKENIZER = AutoTokenizer.from_pretrained(
            bge_tokenizer_path, local_files_only=True
        )
        _WORKER_BGE_TOKENIZER.model_max_length = 1_000_000


def build_section_plan_worker(
    section_file: str,
) -> tuple[str, str, SectionPlan | None, bool, bool, str]:
    """Build and strictly validate one section without writing shared state."""
    if (
        _WORKER_OUTPUT_ROOT is None
        or _WORKER_SECTION_METADATA is None
        or _WORKER_DOC_METADATA is None
        or _WORKER_PARSED_TEXT_CACHE is None
        or _WORKER_PAGE_MAP_CACHE is None
        or _WORKER_INDEX_ROWS_BY_SECTION is None
        or _WORKER_ARTIFACT_KEYS is None
        or _WORKER_ENCODER is None
    ):
        return "error", section_file, None, False, False, "worker not initialized"

    try:
        plan = build_section_plan(
            Path(section_file),
            _WORKER_OUTPUT_ROOT,
            _WORKER_ENCODER,
            _WORKER_SECTION_METADATA,
            _WORKER_DOC_METADATA,
            _WORKER_PARSED_TEXT_CACHE,
            _WORKER_PAGE_MAP_CACHE,
            _WORKER_BGE_TOKENIZER,
            _WORKER_COMPANY_NAMES,
        )
        marker_exists = section_marker_path(
            _WORKER_OUTPUT_ROOT,
            plan.ticker,
            plan.pdf_stem,
            plan.section_instance_id,
        ).exists()
        existing_rows = _WORKER_INDEX_ROWS_BY_SECTION.get(plan.key, [])
        complete = not marker_exists and section_rows_are_complete(
            plan,
            existing_rows,
            _WORKER_ENCODER,
        )
        has_existing_artifacts = bool(
            existing_rows
            or plan.key in _WORKER_ARTIFACT_KEYS
            or marker_exists
        )
    except Exception as exc:
        return "error", section_file, None, False, False, f"{type(exc).__name__}: {exc}"
    return "ok", section_file, plan, complete, has_existing_artifacts, ""


def row_section_key(row: dict) -> tuple[str, str, str] | None:
    ticker = (row.get("ticker") or "").strip().upper()
    pdf_stem = (row.get("pdf_stem") or "").strip()
    section_instance_id = (
        row.get("section_instance_id") or row.get("section_code") or ""
    ).strip()
    if not ticker or not pdf_stem or not section_instance_id:
        return None
    return ticker, pdf_stem, section_instance_id


def section_rows(rows: list[dict], key: tuple[str, str, str]) -> list[dict]:
    return [row for row in rows if row_section_key(row) == key]


def group_index_rows(
    rows: list[dict],
) -> tuple[dict[tuple[str, str, str], list[dict]], list[dict]]:
    """Index chunk rows once so per-section resume lookup is O(1)."""
    grouped: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    unkeyed: list[dict] = []
    for row in rows:
        key = row_section_key(row)
        if key is None:
            unkeyed.append(row)
        else:
            grouped[key].append(row)
    return dict(grouped), unkeyed


def flatten_index_rows(
    grouped: dict[tuple[str, str, str], list[dict]],
    unkeyed: list[dict],
) -> list[dict]:
    return [*unkeyed, *(row for rows in grouped.values() for row in rows)]


def index_section_chunk_files(
    output_root: Path,
    section_keys: set[tuple[str, str, str]],
) -> dict[tuple[str, str, str], list[Path]]:
    """Scan each ticker directory once and key every existing chunk file."""
    identity_to_key = {
        (ticker, f"{pdf_stem}__{section_instance_id}"): (
            ticker,
            pdf_stem,
            section_instance_id,
        )
        for ticker, pdf_stem, section_instance_id in section_keys
    }
    grouped: dict[tuple[str, str, str], list[Path]] = defaultdict(list)
    for ticker in sorted({key[0] for key in section_keys}):
        ticker_out = output_root / ticker
        if not ticker_out.exists():
            continue
        for path in ticker_out.glob("*.txt"):
            if "__chunk_" not in path.stem:
                continue
            identity, chunk_index = path.stem.rsplit("__chunk_", 1)
            if not chunk_index.isdigit():
                continue
            key = identity_to_key.get((ticker, identity))
            if key is not None:
                grouped[key].append(path)
    return {key: sorted(paths) for key, paths in grouped.items()}


def normalize_index_row(row: dict) -> dict:
    normalized = {field: row.get(field, "") for field in CHUNKS_INDEX_FIELDS}
    normalized["ticker"] = (normalized.get("ticker") or "").strip().upper()
    if not (normalized.get("chunk_type") or "").strip():
        normalized["chunk_type"] = CHUNK_TYPE_NORMAL
    if not (normalized.get("chunk_id") or "").strip():
        ticker = normalized.get("ticker") or ""
        pdf_stem = normalized.get("pdf_stem") or ""
        section_instance_id = normalized.get("section_instance_id") or ""
        chunk_index = parse_int(normalized.get("chunk_index"))
        source_id = normalized.get("source_id") or _default_source_id(ticker, pdf_stem)
        if source_id and section_instance_id and chunk_index is not None:
            normalized["chunk_id"] = (
                f"{source_id}__{section_instance_id}__chunk_{chunk_index:04d}"
            )
    return normalized


def index_slot_key(row: dict) -> tuple[str, str, str, int] | None:
    section_key = row_section_key(row)
    chunk_index = parse_int(row.get("chunk_index"))
    if section_key is None or chunk_index is None:
        return None
    return *section_key, chunk_index


def canonicalize_index_rows(rows: list[dict]) -> list[dict]:
    """Make the index a keyed collection, not an append-only log."""
    by_slot: dict[tuple[str, str, str, int], dict] = {}
    without_slot: list[dict] = []
    for row in rows:
        normalized = normalize_index_row(row)
        slot_key = index_slot_key(normalized)
        if slot_key is None:
            without_slot.append(normalized)
        else:
            by_slot[slot_key] = normalized

    by_chunk_id: dict[str, dict] = {}
    without_chunk_id: list[dict] = []
    for row in [*without_slot, *by_slot.values()]:
        chunk_id = (row.get("chunk_id") or "").strip()
        if chunk_id:
            by_chunk_id[chunk_id] = row
        else:
            without_chunk_id.append(row)
    return [*without_chunk_id, *by_chunk_id.values()]


def fingerprint_matches(row: dict, fingerprint: SourceFingerprint) -> bool:
    return all(
        (row.get(field) or "") == expected
        for field, expected in fingerprint.as_index_fields().items()
    )


def valid_chunk_file(
    path: Path,
    expected_text: str,
    encoder,
    chunk_type: str | None = CHUNK_TYPE_NORMAL,
) -> bool:
    try:
        if not path.is_file():
            return False
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if content != expected_text:
        return False
    # The v3 row token count uses the embedding model tokenizer and is checked
    # against a freshly rebuilt plan. This helper only verifies file identity.
    return True


def section_rows_are_complete(
    plan: SectionPlan,
    existing_rows: list[dict],
    encoder,
) -> bool:
    """Strictly validate one section's indexed rows and exact chunk files."""
    if plan.is_unhandled_short:
        return False

    if len(existing_rows) != len(plan.outputs):
        return False

    rows_by_index: dict[int, dict] = {}
    for row in existing_rows:
        chunk_index = parse_int(row.get("chunk_index"))
        if chunk_index is None or chunk_index in rows_by_index:
            return False
        rows_by_index[chunk_index] = row

    seen_chunk_ids: set[str] = set()
    for output in plan.outputs:
        expected = output.row
        chunk_index = parse_int(expected.get("chunk_index"))
        if chunk_index is None:
            return False
        row = rows_by_index.get(chunk_index)
        if row is None:
            return False
        if not fingerprint_matches(row, plan.source_fingerprint):
            return False
        if (row.get("chunk_id") or "") != expected["chunk_id"]:
            return False
        for field in (
            "source_id",
            "section_instance_id",
            "parsed_text_sha256",
            "section_text_sha256",
            "chunk_text_sha256",
            "citation_validation_status",
            "citation_validation_version",
            "citation_ready",
            "chunk_type",
            "short_section_action",
            "short_section_reason",
            "merged_section_ids",
            "table_context",
        ):
            if (row.get(field) or "") != (expected.get(field) or ""):
                return False
        if expected.get("citation_validation_version") != CITATION_VALIDATION_VERSION:
            return False
        if parse_int(row.get("token_count")) != parse_int(expected.get("token_count")):
            return False
        if not valid_token_count_for_chunk_type(
            parse_int(row.get("token_count")),
            row.get("chunk_type"),
        ):
            return False

        recorded_path = resolve_path(row.get("chunk_file"))
        if recorded_path is None:
            return False
        try:
            if recorded_path.resolve() != output.path.resolve():
                return False
        except OSError:
            return False
        if not valid_chunk_file(
            recorded_path,
            output.text,
            encoder,
            expected.get("chunk_type"),
        ):
            return False

        chunk_id = row.get("chunk_id") or ""
        if not chunk_id or chunk_id in seen_chunk_ids:
            return False
        seen_chunk_ids.add(chunk_id)

    return True


def section_is_complete(
    plan: SectionPlan,
    rows: list[dict],
    encoder,
) -> bool:
    """Compatibility wrapper for callers that hold an ungrouped chunk index."""
    return section_rows_are_complete(plan, section_rows(rows, plan.key), encoder)


def commit_section_outputs(
    plan: SectionPlan,
    output_root: Path,
    existing_files: list[Path] | None = None,
) -> None:
    """Atomically replace one section's chunks, then remove only its stale chunks."""
    ticker_out = output_root / plan.ticker
    for output in plan.outputs:
        atomic_write_text(output.path, output.text)

    expected_names = {output.path.name for output in plan.outputs}
    stale_candidates = existing_files
    if stale_candidates is None:
        stale_candidates = section_chunk_files(
            ticker_out,
            plan.pdf_stem,
            plan.section_instance_id,
        )
    for stale_file in stale_candidates:
        if stale_file.name not in expected_names:
            stale_file.unlink()


def clear_section_outputs(
    plan: SectionPlan,
    output_root: Path,
    existing_files: list[Path] | None = None,
) -> None:
    """Remove output only for a source section that is now too short to chunk."""
    ticker_out = output_root / plan.ticker
    stale_candidates = existing_files
    if stale_candidates is None:
        stale_candidates = section_chunk_files(
            ticker_out,
            plan.pdf_stem,
            plan.section_instance_id,
        )
    for stale_file in stale_candidates:
        stale_file.unlink()


def clear_orphan_section_outputs(
    output_root: Path,
    key: tuple[str, str, str],
    existing_files: list[Path] | None = None,
) -> None:
    """Remove chunks for exactly one section no longer present in the section index."""
    ticker, pdf_stem, section_instance_id = key
    ticker_out = output_root / ticker
    stale_candidates = existing_files
    if stale_candidates is None:
        stale_candidates = section_chunk_files(
            ticker_out,
            pdf_stem,
            section_instance_id,
        )
    for stale_file in stale_candidates:
        stale_file.unlink()


def replace_section_rows(
    rows: list[dict], key: tuple[str, str, str], new_rows: list[dict]
) -> list[dict]:
    retained = [row for row in rows if row_section_key(row) != key]
    return canonicalize_index_rows([*retained, *new_rows])


def discover_section_files(
    input_root: Path,
    ticker: str | None = None,
    pdf_stem: str | None = None,
) -> list[Path]:
    if not input_root.exists():
        return []
    files = sorted(input_root.glob("*/*.txt"))
    if ticker:
        ticker = ticker.upper()
        files = [path for path in files if path.parent.name.upper() == ticker]
    if pdf_stem:
        files = [
            path
            for path in files
            if (
                (parsed := parse_section_filename(path)) is not None
                and parsed[0] == pdf_stem
            )
        ]
    return files


def read_existing_index(index_path: Path) -> list[dict]:
    if not index_path.exists():
        return []
    with index_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [{field: row.get(field, "") for field in CHUNKS_INDEX_FIELDS} for row in reader]


def index_sort_key(row: dict) -> tuple[str, str, str, int, str]:
    return (
        (row.get("ticker") or "").upper(),
        row.get("pdf_stem") or "",
        row.get("section_instance_id") or row.get("section_code") or "",
        parse_int(row.get("chunk_index")) or 0,
        row.get("chunk_id") or "",
    )


def write_index(
    index_path: Path, rows: list[dict], already_canonical: bool = False
) -> None:
    """Atomically checkpoint the deduplicated chunks index.

    ``already_canonical`` skips re-running canonicalize_index_rows, which
    rebuilds a fresh dict per row plus two more full-corpus dicts to dedupe by
    slot and by chunk_id. run()'s checkpoint() has nothing to dedupe --
    rows_by_section already holds one clean row list per section key, replaced
    wholesale (never appended to) each time a section is (re)chunked, so
    flatten_index_rows(rows_by_section, unkeyed_rows) is canonical by
    construction. Re-canonicalizing it anyway was pure waste repeated on every
    checkpoint of a run: at 51k rows, several full-corpus passes each time
    checkpoint_every sections complete.
    """
    index_path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(
        rows if already_canonical else canonicalize_index_rows(rows),
        key=index_sort_key,
    )
    tmp_path = index_path.with_name(f"{index_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CHUNKS_INDEX_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
            f.flush()
            os.fsync(f.fileno())
        last_error: OSError | None = None
        for attempt in range(5):
            try:
                os.replace(tmp_path, index_path)
                last_error = None
                break
            except OSError as error:
                last_error = error
                time.sleep(0.2 * (attempt + 1))
        if last_error is not None:
            raise last_error
    except Exception:
        try:
            tmp_path.unlink()
        except (FileNotFoundError, PermissionError):
            pass
        raise


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def run(
    input_root: str | Path,
    out: str | Path,
    index: str | Path,
    sections_index: str | Path = config.ESG_SECTIONS_INDEX_CSV,
    parse_index: str | Path = config.ESG_PARSE_INDEX_CSV,
    source_registry: str | Path = config.ESG_SOURCE_REGISTRY_CSV,
    ticker: str | None = None,
    pdf_stem: str | None = None,
    resume: bool = True,
    force: bool = False,
    checkpoint_every: int = CHECKPOINT_EVERY_DEFAULT,
    workers: int = 1,
    bge_tokenizer_path: str | Path | None = None,
    company_manifest: str | Path = config.ESG_ACCEPTED_COMPANY_MANIFEST_CSV,
    section_hold: str | Path | None = None,
) -> list[dict]:
    if checkpoint_every < 1:
        raise ValueError("checkpoint_every must be at least 1")
    if workers < 1:
        raise ValueError("workers must be at least 1")

    input_root = Path(input_root)
    output_root = Path(out)
    index_path = Path(index)
    encoder = tiktoken.get_encoding(ENCODING)
    bge_tokenizer = None
    if bge_tokenizer_path:
        from transformers import AutoTokenizer

        bge_tokenizer = AutoTokenizer.from_pretrained(
            str(Path(bge_tokenizer_path).resolve()), local_files_only=True
        )
        bge_tokenizer.model_max_length = 1_000_000
    company_names = load_company_names(Path(company_manifest))
    section_metadata = load_section_metadata(Path(sections_index))
    held = merge_section_holds(
        section_metadata,
        load_section_hold_registry(Path(section_hold) if section_hold else None),
    )
    if held:
        print(f"Section holds applied: {held}")
    doc_metadata = load_doc_metadata(Path(parse_index), Path(source_registry))
    section_files = discover_section_files(
        input_root, ticker=ticker, pdf_stem=pdf_stem
    )
    parsed_text_cache: dict[Path, tuple[str, str]] = {}
    print(f"Found {len(section_files)} ESG section file(s) under {input_root}")
    print(f"Plan workers: {workers}")

    raw_rows = read_existing_index(index_path)
    canonical_rows = canonicalize_index_rows(raw_rows)
    rows_by_section, unkeyed_rows = group_index_rows(canonical_rows)
    index_dirty = raw_rows != canonical_rows
    pending_markers: list[Path] = []
    completed_since_checkpoint = 0
    created_rows: list[dict] = []
    summary = {
        "found_section_files": len(section_files),
        "skipped_complete": 0,
        "chunked": 0,
        "reprocessed_stale": 0,
        "short_sections_skipped": 0,
        "short_sections_preserved": 0,
        "short_sections_excluded": 0,
        "failed": 0,
    }

    def checkpoint() -> None:
        nonlocal index_dirty, completed_since_checkpoint
        write_index(
            index_path,
            flatten_index_rows(rows_by_section, unkeyed_rows),
            already_canonical=True,
        )
        for marker in pending_markers:
            clear_section_marker(marker)
        pending_markers.clear()
        index_dirty = False
        completed_since_checkpoint = 0

    # If a re-split PDF no longer produces a previous section instance, the
    # corresponding section file is absent from the authoritative sections
    # index. Remove only that stale section's chunks/index rows. This reconciles
    # changing section sets while retaining every other ticker/PDF/section.
    selected_ticker = ticker.upper() if ticker else None
    valid_section_keys = set(section_metadata)
    files_by_section = index_section_chunk_files(
        output_root,
        valid_section_keys | set(rows_by_section),
    )
    if valid_section_keys:
        orphaned_section_keys = sorted(
            key
            for key in rows_by_section
            if (selected_ticker is None or key[0] == selected_ticker)
            and key not in valid_section_keys
        )
        for orphan_key in orphaned_section_keys:
            orphan_ticker, orphan_pdf_stem, orphan_section_instance_id = orphan_key
            if pdf_stem is not None and orphan_pdf_stem != pdf_stem:
                continue
            marker_path = section_marker_path(
                output_root,
                orphan_ticker,
                orphan_pdf_stem,
                orphan_section_instance_id,
            )
            try:
                start_orphan_section_cleanup(marker_path)
                clear_orphan_section_outputs(
                    output_root,
                    orphan_key,
                    files_by_section.get(orphan_key, []),
                )
                rows_by_section.pop(orphan_key, None)
                files_by_section.pop(orphan_key, None)
                pending_markers.append(marker_path)
                index_dirty = True
                completed_since_checkpoint += 1
                if completed_since_checkpoint >= checkpoint_every:
                    checkpoint()
                print(
                    "  "
                    f"{orphan_ticker} {orphan_pdf_stem}__{orphan_section_instance_id}: "
                    "removed orphaned chunks"
                )
            except Exception as exc:
                summary["failed"] += 1
                print(
                    "  "
                    f"{orphan_ticker} {orphan_pdf_stem}__{orphan_section_instance_id}: "
                    f"failed orphan cleanup ({exc})"
                )

    page_map_cache: dict[Path, list[dict]] = {}
    artifact_keys = set(rows_by_section) | set(files_by_section)

    def iter_plans():
        if workers == 1:
            for section_file in section_files:
                try:
                    plan = build_section_plan(
                        section_file,
                        output_root,
                        encoder,
                        section_metadata,
                        doc_metadata,
                        parsed_text_cache,
                        page_map_cache,
                        bge_tokenizer,
                        company_names,
                    )
                    marker_exists = section_marker_path(
                        output_root,
                        plan.ticker,
                        plan.pdf_stem,
                        plan.section_instance_id,
                    ).exists()
                    existing_rows = rows_by_section.get(plan.key, [])
                    complete = not marker_exists and section_rows_are_complete(
                        plan,
                        existing_rows,
                        encoder,
                    )
                    has_existing_artifacts = bool(
                        existing_rows
                        or plan.key in artifact_keys
                        or marker_exists
                    )
                    yield (
                        "ok",
                        str(section_file),
                        plan,
                        complete,
                        has_existing_artifacts,
                        "",
                    )
                except Exception as exc:
                    yield (
                        "error",
                        str(section_file),
                        None,
                        False,
                        False,
                        f"{type(exc).__name__}: {exc}",
                    )
            return

        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=init_plan_worker,
            initargs=(
                str(output_root),
                section_metadata,
                doc_metadata,
                rows_by_section,
                artifact_keys,
                str(Path(bge_tokenizer_path).resolve()) if bge_tokenizer_path else None,
                company_names,
            ),
        ) as executor:
            yield from executor.map(
                build_section_plan_worker,
                [str(section_file) for section_file in section_files],
                chunksize=8,
            )

    for (
        status,
        section_file_raw,
        plan,
        complete,
        has_existing_artifacts,
        error_message,
    ) in iter_plans():
        section_file = Path(section_file_raw)
        if status != "ok" or plan is None:
            summary["failed"] += 1
            print(
                f"  {section_file.parent.name.upper()} {section_file.stem}: "
                f"failed ({error_message})"
            )
            continue

        marker_path = section_marker_path(
            output_root, plan.ticker, plan.pdf_stem, plan.section_instance_id
        )
        current_rows = rows_by_section.get(plan.key, [])
        current_files = files_by_section.get(plan.key, [])

        if resume and not force and complete:
            summary["skipped_complete"] += 1
            print(f"  {plan.ticker} {section_file.stem}: skipped complete")
            continue

        if has_existing_artifacts and not complete:
            summary["reprocessed_stale"] += 1

        needs_cleanup = plan.is_unhandled_short and has_existing_artifacts
        if plan.is_unhandled_short:
            summary["short_sections_skipped"] += 1
            summary["short_sections_excluded"] += 1
            if needs_cleanup:
                try:
                    start_section_update(marker_path, plan.source_fingerprint)
                    clear_section_outputs(
                        plan,
                        output_root,
                        current_files,
                    )
                    rows_by_section.pop(plan.key, None)
                    files_by_section.pop(plan.key, None)
                    pending_markers.append(marker_path)
                    index_dirty = True
                    completed_since_checkpoint += 1
                    if completed_since_checkpoint >= checkpoint_every:
                        checkpoint()
                except Exception as exc:
                    summary["failed"] += 1
                    print(f"  {plan.ticker} {section_file.stem}: failed ({exc})")
                    continue
            print(
                f"  {plan.ticker} {section_file.stem}: short section excluded "
                f"({plan.source_token_count} tokens; {plan.short_section_reason})"
            )
            continue

        try:
            # The marker remains until the matching index checkpoint succeeds.
            start_section_update(marker_path, plan.source_fingerprint)
            commit_section_outputs(
                plan,
                output_root,
                current_files,
            )
            new_rows = [output.row for output in plan.outputs]
            rows_by_section[plan.key] = new_rows
            files_by_section[plan.key] = [output.path for output in plan.outputs]
            pending_markers.append(marker_path)
            index_dirty = True
            completed_since_checkpoint += 1
            summary["chunked"] += 1
            if plan.is_short:
                if plan.short_section_action == SHORT_SECTION_ACTION_EXCLUDED:
                    summary["short_sections_excluded"] += 1
                else:
                    summary["short_sections_preserved"] += 1
            created_rows.extend(new_rows)
            if completed_since_checkpoint >= checkpoint_every:
                checkpoint()
            print(f"  {plan.ticker} {section_file.stem}: {len(new_rows)} chunk(s)")
        except Exception as exc:
            summary["failed"] += 1
            print(f"  {plan.ticker} {section_file.stem}: failed ({exc})")

    if index_dirty or not index_path.exists():
        checkpoint()

    print()
    for label in (
        "found_section_files",
        "skipped_complete",
        "chunked",
        "reprocessed_stale",
        "short_sections_skipped",
        "short_sections_preserved",
        "short_sections_excluded",
        "failed",
    ):
        print(f"{label}: {summary[label]}")
    print(f"chunks_created: {len(created_rows)}")
    print(f"Index saved to: {index_path}")
    print(f"Chunks saved to: {output_root.resolve()}")
    return created_rows


def main():
    parser = argparse.ArgumentParser(description="Chunk ESG sections into retrieval-ready text chunks.")
    parser.add_argument("--input", default=str(config.ESG_SECTIONS_DIR))
    parser.add_argument("--out", default=str(config.ESG_CHUNKS_DIR))
    parser.add_argument("--index", default=str(config.ESG_CHUNKS_INDEX_CSV))
    parser.add_argument("--sections-index", default=str(config.ESG_SECTIONS_INDEX_CSV))
    parser.add_argument("--parse-index", default=str(config.ESG_PARSE_INDEX_CSV))
    parser.add_argument(
        "--source-registry",
        default=str(config.ESG_SOURCE_REGISTRY_CSV),
    )
    parser.add_argument(
        "--company-manifest",
        default=str(config.ESG_ACCEPTED_COMPANY_MANIFEST_CSV),
    )
    parser.add_argument(
        "--section-hold",
        default=None,
        help=(
            "Sparse CSV of manual-review decisions "
            "(ticker,pdf_stem,section_instance_id,rag_action,reason). Held "
            "sections are still chunked and citable but never indexed."
        ),
    )
    parser.add_argument(
        "--bge-tokenizer",
        # Resolution order, most specific first: the flag, then the
        # environment, then the copy committed at config.ESG_BGE_TOKENIZER_DIR.
        # The config default exists so a fresh clone can chunk without any
        # setup step -- token counts depend on this exact tokenizer, and the
        # only copy used to live in gitignored scratch under tmp/.
        default=os.environ.get("ESG_BGE_TOKENIZER_DIR") or str(config.ESG_BGE_TOKENIZER_DIR),
        help=(
            "Local BGE tokenizer directory. Required for esg_chunk_v3. "
            "Defaults to ESG_BGE_TOKENIZER_DIR, else the copy committed under "
            "models/bge-base-en-v1.5-tokenizer."
        ),
    )
    parser.add_argument("--ticker", default=None)
    parser.add_argument(
        "--pdf-stem",
        default=None,
        help="Limit work to one parsed PDF stem (useful for isolated pilots).",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        default=True,
        help="Skip valid completed sections (default).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild the selected ticker (or all sections) even when complete.",
    )
    parser.add_argument(
        "--checkpoint-every",
        type=positive_int,
        default=CHECKPOINT_EVERY_DEFAULT,
        metavar="N",
        help=(
            "Atomically checkpoint the chunks index after every N updated "
            f"sections (default: {CHECKPOINT_EVERY_DEFAULT}). Each checkpoint "
            "rewrites the whole index, so 1 makes the stage quadratic; pass 1 "
            "only when losing a single section of work matters more than time."
        ),
    )
    parser.add_argument(
        "--workers",
        type=positive_int,
        default=1,
        metavar="N",
        help=(
            "Build chunk/citation plans in N worker processes while the parent "
            "process performs all chunk-file and index writes (default: 1)."
        ),
    )
    args = parser.parse_args()
    if not args.bge_tokenizer:
        parser.error("--bge-tokenizer or ESG_BGE_TOKENIZER_DIR is required")
    # Fail here rather than deep inside the tokenizer loader: a missing
    # directory means every token count would be wrong or absent, and that is
    # far cheaper to notice now than after a full chunking run.
    if not Path(args.bge_tokenizer).is_dir():
        parser.error(f"BGE tokenizer directory not found: {args.bge_tokenizer}")

    run(
        input_root=args.input,
        out=args.out,
        index=args.index,
        sections_index=args.sections_index,
        parse_index=args.parse_index,
        source_registry=args.source_registry,
        ticker=args.ticker.upper() if args.ticker else None,
        pdf_stem=args.pdf_stem,
        resume=args.resume,
        force=args.force,
        checkpoint_every=args.checkpoint_every,
        workers=args.workers,
        bge_tokenizer_path=args.bge_tokenizer,
        company_manifest=args.company_manifest,
        section_hold=args.section_hold,
    )


if __name__ == "__main__":
    main()
