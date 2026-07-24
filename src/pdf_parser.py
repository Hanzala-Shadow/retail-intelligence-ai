from __future__ import annotations

import argparse
import csv
import gc
import hashlib
import os
import multiprocessing as mp
import re
from concurrent.futures import ProcessPoolExecutor, as_completed
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pdfplumber

try:
    import pypdfium2 as pdfium
except ImportError:
    pdfium = None

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

try:
    import psutil
except ImportError:
    psutil = None

from base_parser import ParsedDocument
from esg_reading_order import reconstruct_column_order
from esg_intake_catalog import (
    approved_ocr_rows,
    extraction_artifact_id,
    file_alias_id,
    logical_source_id,
    read_csv as read_intake_csv,
    source_version_id,
)


MIN_PAGE_CHARS = 20
TEXT_LIGHT_MIN_ALPHA_CHARS = 3
OCR_MIN_NONSPACE_CHARS = 500
TEXT_FALLBACK_MIN_PAGES = 5
TEXT_FALLBACK_MAX_CHARS_PER_PAGE = 250
TEXT_FALLBACK_MIN_PAGE_COVERAGE = 0.50
TEXT_FALLBACK_MIN_GAIN_RATIO = 1.50
TEXT_FALLBACK_MIN_CHAR_GAIN = 2000
DEFAULT_OCR_ROOT = None
DEFAULT_FILE_CATALOG = "data/00_reference/esg_file_catalog.csv"
DEFAULT_OCR_APPROVAL = "data/00_reference/esg_ocr_approval.csv"
DEFAULT_PARSER_OVERRIDES = "data/00_reference/esg_parser_overrides.csv"
DEFAULT_AUTO_LAYOUT_PDFIUM = False
# v3 preserves one page-map row per physical page and separates high-risk
# numeric/grid layouts from ordinary ambiguous reading order. v2 rows must be
# rebuilt so the old sparse page maps and over-broad unresolved status do not
# survive resume mode.
AUTO_PDFPLUMBER_COLUMN_POLICY = "auto_pdfplumber_column_order_v3"
AUTO_PDFPLUMBER_COLUMN_REASON = "deterministic_coordinate_reading_order_v3"
AUTO_TEXT_LAYER_FALLBACK_POLICY = "auto_text_layer_fallback_v3"
AUTO_LAYOUT_GRID_FALLBACK_POLICY = "auto_layout_grid_fallback_v3"
AUTO_PYMUPDF_LAYOUT_POLICY = "auto_pymupdf_layout_v2"
AUTO_PYMUPDF_LAYOUT_REASON = "automatic_pymupdf_coordinate_xy_cut_strict_preservation_v2"
REGION_MIN_PRESERVATION_RATIO = 0.995
REGION_MAX_EXTRA_TOKEN_RATIO = 0.005
LEGACY_TEXT_LAYER_FALLBACK_POLICY = "auto_text_layer_fallback"
LEGACY_LAYOUT_GRID_FALLBACK_POLICY = "auto_layout_grid_fallback"
LEGACY_TEXT_LAYER_FALLBACK_POLICY_V2 = "auto_text_layer_fallback_v2"
LEGACY_LAYOUT_GRID_FALLBACK_POLICY_V2 = "auto_layout_grid_fallback_v2"
LAYOUT_GRID_FALLBACK_POLICIES = frozenset(
    {
        LEGACY_LAYOUT_GRID_FALLBACK_POLICY,
        LEGACY_LAYOUT_GRID_FALLBACK_POLICY_V2,
        AUTO_LAYOUT_GRID_FALLBACK_POLICY,
    }
)
PAGE_REASON_COLUMN_ORDER = "coordinate_column_order"
PAGE_REASON_REGION_ORDER = "region_xy_cut_order"
PAGE_REASON_TABLE_ORDER = "table_aware_xy_cut_order"
PAGE_REASON_PDFIUM_PAGE = "pdfium_text_page"
PAGE_REASON_PDFIUM_FORCED = "cli_forced_pdfium_text"
PAGE_REASON_PYMUPDF_REGION_ORDER = "pymupdf_region_xy_cut_order"
PAGE_REASON_PYMUPDF_TABLE_ORDER = "pymupdf_table_aware_xy_cut_order"
LAYOUT_GRID_MIN_WORDS = 80
LAYOUT_GRID_MIN_SHORT_LINES = 12
LAYOUT_GRID_MIN_COMMON_STARTS = 3
LAYOUT_GRID_MIN_HUGE_GAP_LINES = 3
LAYOUT_GRID_MIN_VISUAL_OBJECTS = 8
LAYOUT_GRID_MIN_METRIC_LINES = 2
PARSE_INDEX_FIELDS = [
    "ticker",
    "pdf_file",
    "logical_source_id",
    "source_version_id",
    "file_alias_id",
    "extraction_artifact_id",
    "source_pdf",
    "source_size_bytes",
    "source_mtime_utc",
    "source_sha256",
    "parse_source_kind",
    "parse_source_pdf",
    "parse_source_size_bytes",
    "parse_source_mtime_utc",
    "parse_source_sha256",
    "ocr_approval_status",
    "ocr_selection_reason",
    "parser_used",
    "parser_policy",
    "parser_reason",
    "layout_risk_pages",
    "layout_numeric_risk_pages",
    "complex_reading_order_pages",
    "text_light_pages",
    "visual_only_pages",
    "extraction_failed_pages",
    "reading_order_repaired_pages",
    "reading_order_unresolved_pages",
    "text_layer_fallback_pages",
    "page_text_change_reasons",
    "parsed_text_file",
    "page_map_file",
    "status",
    "error_message",
    "page_count",
    "char_count",
    "table_count",
    "content_hash",
    "parsed_at",
    "quality_flags",
    "possible_wrong_doc_type",
    "readable_word_count",
    "readable_word_ratio",
    "chars_per_page",
    "garbled_char_count",
]
PAGE_MAP_FIELDS = [
    "page",
    "char_start",
    "char_end",
    "char_count",
    "extracted_char_count",
    "emitted",
    "page_type",
    "parse_status",
    "reading_order_status",
    "layout_risk",
    "visual_review_status",
    "repair_method",
    "text_source",
    "table_candidate_count",
]
PARSER_OVERRIDE_FIELDS = ["ticker", "pdf_file", "parser_mode", "reason", "active"]
SOURCE_FINGERPRINT_FIELDS = [
    "source_size_bytes",
    "source_mtime_utc",
    "source_sha256",
]
PARSE_SOURCE_FINGERPRINT_FIELDS = [
    "parse_source_size_bytes",
    "parse_source_mtime_utc",
    "parse_source_sha256",
]

SEC_10K_MARKERS = [
    r"\bFORM\s+10-K\b",
    r"\bUNITED\s+STATES\s+SECURITIES\s+AND\s+EXCHANGE\s+COMMISSION\b",
    r"\bAnnual\s+Report\s+Pursuant\s+to\s+Section\s+13\s+or\s+15\(d\)\b",
    r"\bItem\s+1A\.?\s+Risk\s+Factors\b",
    r"\bItem\s+7\.?\s+Management'?s\s+Discussion\s+and\s+Analysis\b",
]
SEC_10K_ITEM_MARKERS = [
    r"\bItem\s+1\.?\s+Business\b",
    r"\bItem\s+1A\.?\s+Risk\s+Factors\b",
    r"\bItem\s+7\.?\s+Management'?s\s+Discussion\s+and\s+Analysis\b",
    r"\bItem\s+8\.?\s+Financial\s+Statements\b",
]
SEC_IDENTITY_MARKERS = [
    r"\bCENTRAL\s+INDEX\s+KEY\b",
    r"\bCIK\s*(?:No\.?|Number|#|:)\s*\d{6,10}\b",
    r"\b\d{10}-\d{2}-\d{6}\b",
    r"\bCommission\s+File\s+Number\b",
]
GARBLED_SEQUENCES = ["ï¿½", "Ã¢â‚¬", "Ã‚", "ï¿½?", "�"]
CID_ARTIFACT_RE = re.compile(r"\(cid:\d+\)")
# A measured value with its unit. The trailing guard must be a negative lookahead
# rather than ``\b``: ``\b`` after a literal ``%`` can never match at end of line
# or before a space, because ``%`` is already a non-word character. With ``\b``
# the percent branch was dead, so "45%" -- the commonest metric form in ESG
# reporting -- never counted, and metric tables failed the grid signature.
METRIC_VALUE_RE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:%|M\+?|K\+?|million|billion|tons?|metric tons|CO2e|gCO2e)"
    r"(?![A-Za-z])",
    flags=re.IGNORECASE,
)


def mem() -> float:
    if psutil is None:
        return 0.0
    return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)


def display_path(path: str | Path) -> str:
    path = Path(path)
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def count_garbled_chars(text: str) -> int:
    return sum(text.count(sequence) for sequence in GARBLED_SEQUENCES)


def count_cid_artifacts(text: str) -> int:
    return len(CID_ARTIFACT_RE.findall(text))


def is_possible_10k(text: str) -> bool:
    opening = text[:15000]
    first_pages = text[:5000]

    has_form_10k = bool(re.search(r"\bFORM\s+10-K\b", first_pages, flags=re.IGNORECASE))
    has_sec_header = bool(
        re.search(
            r"\bUNITED\s+STATES\s+SECURITIES\s+AND\s+EXCHANGE\s+COMMISSION\b",
            first_pages,
            flags=re.IGNORECASE,
        )
    )
    has_annual_report_pursuant = bool(
        re.search(
            r"\bAnnual\s+Report\s+Pursuant\s+to\s+Section\s+13\s+or\s+15\(d\)\b",
            first_pages,
            flags=re.IGNORECASE,
        )
    )
    sec_item_count = sum(
        1
        for pattern in SEC_10K_ITEM_MARKERS
        if re.search(pattern, opening, flags=re.IGNORECASE)
    )
    identity_count = sum(
        bool(re.search(pattern, opening, flags=re.IGNORECASE))
        for pattern in SEC_IDENTITY_MARKERS
    )
    cover_structure = bool(
        re.search(r"\b(?:Exact name of registrant|State or other jurisdiction|I\.R\.S\. Employer)\b", first_pages, re.I)
    )
    score = (
        3 * has_form_10k
        + 2 * has_sec_header
        + 2 * has_annual_report_pursuant
        + sec_item_count
        + identity_count
        + cover_structure
    )
    return score >= 5 and (has_form_10k or has_annual_report_pursuant or sec_item_count >= 3)


def text_quality_metrics(text: str, page_count: int, char_count: int) -> dict:
    total_wordish_tokens = len(re.findall(r"\S+", text))
    readable_word_count = len(re.findall(r"\b[A-Za-z]{3,}\b", text))
    readable_word_ratio = (
        readable_word_count / total_wordish_tokens
        if total_wordish_tokens
        else 0.0
    )
    chars_per_page = char_count / page_count if page_count else 0.0
    garbled_char_count = count_garbled_chars(text)
    possible_wrong_doc_type = is_possible_10k(text)

    flags = []
    if possible_wrong_doc_type:
        flags.append("possible_10k")
    if garbled_char_count > 100 or (
        char_count > 0 and garbled_char_count / char_count > 0.005
    ):
        flags.append("garbled_text")
    if char_count >= OCR_MIN_NONSPACE_CHARS and readable_word_ratio < 0.45:
        flags.append("low_readable_word_ratio")
    if page_count >= 5 and chars_per_page < 250:
        flags.append("low_text_per_page")

    return {
        "quality_flags": "|".join(flags),
        "possible_wrong_doc_type": "true" if possible_wrong_doc_type else "false",
        "readable_word_count": readable_word_count,
        "readable_word_ratio": f"{readable_word_ratio:.4f}",
        "chars_per_page": f"{chars_per_page:.1f}",
        "garbled_char_count": garbled_char_count,
    }


def classify_page_type(text: str) -> str:
    """Classify the final extracted text without treating page numbers as content.

    ``text_light`` keeps useful divider/title text such as "Opportunity" while
    ``visual_only`` covers empty pages and pages whose text layer contains only
    a printed page number or symbols.
    """

    stripped = text.strip()
    if len(stripped) > MIN_PAGE_CHARS:
        return "text"

    alpha_chars = sum(character.isalpha() for character in stripped)
    readable_words = re.findall(r"\b[A-Za-z]{3,}\b", stripped)
    if alpha_chars >= TEXT_LIGHT_MIN_ALPHA_CHARS and readable_words:
        return "text_light"
    return "visual_only"


def page_text_for_output(text: str) -> str:
    """Emit substantive and title text, but suppress number-only visual pages."""

    return "" if classify_page_type(text) == "visual_only" else text


def build_raw_text_and_page_spans(
    page_texts: dict[int, str],
    page_count: int,
    page_metadata: dict[int, dict] | None = None,
) -> tuple[str, list[dict]]:
    """Build text plus a complete one-row-per-physical-page page map.

    Empty/visual pages receive a zero-length character range instead of being
    dropped. This preserves physical page lineage without injecting bare page
    numbers into the retrieval corpus.
    """

    parts: list[str] = []
    spans: list[dict] = []
    cursor = 0
    page_metadata = page_metadata or {}

    for page_number in range(1, page_count + 1):
        extracted_text = page_texts.get(page_number, "")
        emitted_text = page_text_for_output(extracted_text)
        metadata = page_metadata.get(page_number, {})

        if emitted_text and parts:
            parts.append("\n")
            cursor += 1

        start = cursor
        if emitted_text:
            parts.append(emitted_text)
            cursor += len(emitted_text)
        spans.append(
            {
                "page": page_number,
                "char_start": start,
                "char_end": cursor,
                "char_count": len(emitted_text),
                "extracted_char_count": len(extracted_text),
                "emitted": "true" if emitted_text else "false",
                "page_type": metadata.get(
                    "page_type", classify_page_type(extracted_text)
                ),
                "parse_status": metadata.get("parse_status", "ok"),
                "reading_order_status": metadata.get(
                    "reading_order_status", "not_applicable"
                ),
                "layout_risk": metadata.get("layout_risk", "false"),
                "visual_review_status": metadata.get(
                    "visual_review_status", "not_required"
                ),
                "repair_method": metadata.get("repair_method", "none"),
                "text_source": metadata.get("text_source", "pdfplumber"),
                "table_candidate_count": metadata.get(
                    "table_candidate_count", 0
                ),
            }
        )

    return "".join(parts), spans


def extracted_char_count(pages: list[tuple[int, str]]) -> int:
    return sum(len(text.strip()) for _, text in pages)


def page_coverage(pages: list[tuple[int, str]], page_count: int) -> float:
    if page_count <= 0:
        return 0.0
    return len(pages) / page_count


def should_try_text_layer_fallback(
    pages: list[tuple[int, str]],
    page_count: int,
) -> bool:
    if page_count < TEXT_FALLBACK_MIN_PAGES:
        return False

    chars_per_page = extracted_char_count(pages) / page_count
    coverage = page_coverage(pages, page_count)
    has_cid_artifacts = any(count_cid_artifacts(text) for _, text in pages)
    return (
        chars_per_page < TEXT_FALLBACK_MAX_CHARS_PER_PAGE
        or coverage < TEXT_FALLBACK_MIN_PAGE_COVERAGE
        or has_cid_artifacts
    )


def should_use_text_layer_fallback(
    simple_pages: list[tuple[int, str]],
    fallback_pages: list[tuple[int, str]],
    page_count: int,
) -> bool:
    simple_chars = extracted_char_count(simple_pages)
    fallback_chars = extracted_char_count(fallback_pages)
    simple_cids = sum(count_cid_artifacts(text) for _, text in simple_pages)
    fallback_cids = sum(count_cid_artifacts(text) for _, text in fallback_pages)
    if fallback_chars <= simple_chars and fallback_cids >= simple_cids:
        return False

    if simple_cids > 0 and fallback_cids < simple_cids:
        return True

    char_gain = fallback_chars - simple_chars
    gain_ratio = fallback_chars / max(simple_chars, 1)
    fallback_chars_per_page = fallback_chars / page_count if page_count else 0.0
    fallback_coverage = page_coverage(fallback_pages, page_count)

    if (
        fallback_chars_per_page >= TEXT_FALLBACK_MAX_CHARS_PER_PAGE
        and fallback_coverage >= TEXT_FALLBACK_MIN_PAGE_COVERAGE
    ):
        return True

    return (
        char_gain >= TEXT_FALLBACK_MIN_CHAR_GAIN
        and gain_ratio >= TEXT_FALLBACK_MIN_GAIN_RATIO
    )


def _line_groups(words: list[dict], y_tolerance: float = 3.0) -> list[list[dict]]:
    groups: list[list[dict]] = []
    tops: list[float] = []
    for word in sorted(
        words,
        key=lambda item: (float(item.get("top", 0.0)), float(item.get("x0", 0.0))),
    ):
        top = float(word.get("top", 0.0))
        if not groups or abs(top - tops[-1]) > y_tolerance:
            groups.append([word])
            tops.append(top)
        else:
            groups[-1].append(word)
    return groups


def _largest_horizontal_gap(words: list[dict]) -> float:
    if len(words) < 2:
        return 0.0

    sorted_words = sorted(words, key=lambda item: float(item.get("x0", 0.0)))
    return max(
        float(sorted_words[index + 1].get("x0", 0.0))
        - float(sorted_words[index].get("x1", 0.0))
        for index in range(len(sorted_words) - 1)
    )


@dataclass
class _LayoutBlock:
    x0: float
    top: float
    x1: float
    bottom: float
    text: str
    kind: str = "text"
    font_size: float = 0.0


@dataclass(frozen=True)
class _RegionOrderResult:
    status: str
    text: str
    source_word_count: int
    output_word_count: int
    block_count: int
    preservation_ratio: float = 0.0
    extra_token_ratio: float = 0.0
    reason: str = ""


def _bbox_contains_word(bbox: tuple[float, float, float, float], word: dict) -> bool:
    center_x = (float(word.get("x0", 0.0)) + float(word.get("x1", 0.0))) / 2
    center_y = (float(word.get("top", 0.0)) + float(word.get("bottom", 0.0))) / 2
    return bbox[0] <= center_x <= bbox[2] and bbox[1] <= center_y <= bbox[3]


def _clean_table_cell(value) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip().replace("|", "\\|")


def _table_to_markdown(data: list[list]) -> str:
    rows = [[_clean_table_cell(cell) for cell in row] for row in data]
    rows = [row for row in rows if any(row)]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    header = rows[0]
    if not any(header):
        header = [f"column_{index + 1}" for index in range(width)]
    body = rows[1:]
    rendered = ["| " + " | ".join(header) + " |"]
    rendered.append("| " + " | ".join(["---"] * width) + " |")
    rendered.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(rendered)


def _valid_table_candidates(page) -> list[_LayoutBlock]:
    """Return defensible grid tables, rejecting decorative ESG page geometry.

    pdfplumber's default table finder treats page borders, cards, maps and other
    vector artwork as table edges. We therefore reject out-of-bounds, one-row,
    one-column, sparse and near-full-page candidates. Extremely curve-heavy
    pages are charts/maps and go to visual review instead of an expensive and
    misleading grid extraction pass.
    """

    if len(getattr(page, "curves", []) or []) > 1200:
        return []
    try:
        tables = page.find_tables() or []
    except Exception:
        return []

    candidates: list[_LayoutBlock] = []
    page_area = max(float(page.width) * float(page.height), 1.0)
    for table in tables:
        try:
            x0, top, x1, bottom = map(float, table.bbox)
        except Exception:
            continue
        if x0 < -1 or top < -1 or x1 > float(page.width) + 1 or bottom > float(page.height) + 1:
            continue
        width = x1 - x0
        height = bottom - top
        if width < 60 or height < 20:
            continue
        area_ratio = (width * height) / page_area
        if area_ratio > 0.80:
            continue
        try:
            data = table.extract() or []
        except Exception:
            continue
        rows = [[_clean_table_cell(cell) for cell in row] for row in data]
        rows = [row for row in rows if any(row)]
        row_count = len(rows)
        column_count = max((len(row) for row in rows), default=0)
        nonempty = sum(bool(cell) for row in rows for cell in row)
        fill_ratio = nonempty / max(row_count * column_count, 1)
        if row_count < 2 or column_count < 2 or nonempty < 4 or fill_ratio < 0.30:
            continue
        markdown = _table_to_markdown(rows)
        if not markdown:
            continue
        candidate = _LayoutBlock(x0, top, x1, bottom, markdown, kind="table")
        # Deduplicate near-identical overlapping candidates.
        duplicate = False
        for existing in candidates:
            ix0, iy0 = max(existing.x0, x0), max(existing.top, top)
            ix1, iy1 = min(existing.x1, x1), min(existing.bottom, bottom)
            intersection = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
            union = (existing.x1 - existing.x0) * (existing.bottom - existing.top) + width * height - intersection
            if union and intersection / union >= 0.85:
                duplicate = True
                break
        if not duplicate:
            candidates.append(candidate)
    return candidates


def _make_line_block(words: list[dict]) -> _LayoutBlock:
    sizes = [float(word.get("size") or 0.0) for word in words if word.get("size")]
    sizes.sort()
    median_size = sizes[len(sizes) // 2] if sizes else 0.0
    return _LayoutBlock(
        x0=min(float(word.get("x0", 0.0)) for word in words),
        top=min(float(word.get("top", 0.0)) for word in words),
        x1=max(float(word.get("x1", 0.0)) for word in words),
        bottom=max(float(word.get("bottom", 0.0)) for word in words),
        text=" ".join(str(word.get("text", "")) for word in words).strip(),
        font_size=median_size,
    )


def _line_segments(words: list[dict], page_width: float, y_tolerance: float = 3.0) -> list[_LayoutBlock]:
    # Preserve edge text by default. Earlier versions removed every word inside
    # a five-percent page margin, which could silently discard real captions,
    # row labels, or narrow sidebars. Only genuinely rotated runs are excluded;
    # horizontal edge text remains available to the preservation gate.
    usable = []
    for word in words:
        top = float(word.get("top", 0.0))
        bottom = float(word.get("bottom", top))
        if not bool(word.get("upright", True)) and (bottom - top) > 15.0:
            continue
        usable.append(word)
    groups: list[list[dict]] = []
    tops: list[float] = []
    for word in sorted(
        usable,
        key=lambda item: (float(item.get("top", 0.0)), float(item.get("x0", 0.0))),
    ):
        top = float(word.get("top", 0.0))
        if not groups or abs(top - tops[-1]) > y_tolerance:
            groups.append([word])
            tops.append(top)
        else:
            groups[-1].append(word)

    segments: list[_LayoutBlock] = []
    for group in groups:
        ordered = sorted(group, key=lambda item: float(item.get("x0", 0.0)))
        if not ordered:
            continue
        positive_gaps = [
            float(ordered[index + 1].get("x0", 0.0)) - float(ordered[index].get("x1", 0.0))
            for index in range(len(ordered) - 1)
            if float(ordered[index + 1].get("x0", 0.0)) > float(ordered[index].get("x1", 0.0))
        ]
        positive_gaps.sort()
        median_gap = positive_gaps[len(positive_gaps) // 2] if positive_gaps else 3.0
        split_gap = max(10.0, min(18.0, median_gap * 2.5))
        current = [ordered[0]]
        for word in ordered[1:]:
            gap = float(word.get("x0", 0.0)) - float(current[-1].get("x1", 0.0))
            if gap > split_gap:
                block = _make_line_block(current)
                if block.text:
                    segments.append(block)
                current = [word]
            else:
                current.append(word)
        block = _make_line_block(current)
        if block.text:
            segments.append(block)
    return sorted(segments, key=lambda block: (block.top, block.x0))


def _interval_overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _merge_line_segments(lines: list[_LayoutBlock]) -> list[_LayoutBlock]:
    if not lines:
        return []
    heights = sorted(max(1.0, line.bottom - line.top) for line in lines)
    median_height = heights[len(heights) // 2]
    blocks: list[_LayoutBlock] = []
    for line in sorted(lines, key=lambda item: (item.top, item.x0)):
        best_index = None
        best_score = float("-inf")
        for index, block in enumerate(blocks):
            vertical_gap = line.top - block.bottom
            if vertical_gap < -1 or vertical_gap > max(20.0, median_height * 2.2):
                continue
            overlap = _interval_overlap(line.x0, line.x1, block.x0, block.x1)
            overlap_ratio = overlap / max(1.0, min(line.x1 - line.x0, block.x1 - block.x0))
            left_close = abs(line.x0 - block.x0) <= 16.0
            center_close = abs((line.x0 + line.x1) - (block.x0 + block.x1)) <= 50.0
            font_close = (
                not block.font_size
                or not line.font_size
                or abs(line.font_size - block.font_size) <= 2.5
            )
            if font_close and (overlap_ratio >= 0.45 or left_close or center_close):
                score = (
                    2.0 * overlap_ratio
                    + (0.8 if left_close else 0.0)
                    + (0.4 if center_close else 0.0)
                    - vertical_gap / 40.0
                )
                if score > best_score:
                    best_index = index
                    best_score = score
        if best_index is None:
            blocks.append(line)
        else:
            block = blocks[best_index]
            block.text = f"{block.text}\n{line.text}"
            block.x0 = min(block.x0, line.x0)
            block.top = min(block.top, line.top)
            block.x1 = max(block.x1, line.x1)
            block.bottom = max(block.bottom, line.bottom)
    return blocks


def _axis_gaps(blocks: list[_LayoutBlock], axis: str) -> list[tuple[float, float, float]]:
    if axis == "x":
        intervals = sorted((block.x0, block.x1) for block in blocks)
    else:
        intervals = sorted((block.top, block.bottom) for block in blocks)
    merged: list[list[float]] = []
    for start, end in intervals:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [
        (merged[index][1], merged[index + 1][0], merged[index + 1][0] - merged[index][1])
        for index in range(len(merged) - 1)
    ]


def _xy_cut_order(
    blocks: list[_LayoutBlock],
    page_width: float,
    page_height: float,
    depth: int = 0,
) -> list[_LayoutBlock]:
    if len(blocks) <= 1 or depth >= 24:
        return blocks
    x_gap = max(_axis_gaps(blocks, "x"), key=lambda item: item[2], default=None)
    y_gap = max(_axis_gaps(blocks, "y"), key=lambda item: item[2], default=None)
    valid_x = bool(x_gap and x_gap[2] >= 18.0)
    valid_y = bool(y_gap and y_gap[2] >= 8.0)

    # Horizontal section cuts get priority when they are comparably strong. This
    # keeps full-width headings/aspiration strips before the columns below them.
    if valid_y and (
        not valid_x
        or (y_gap[2] / max(page_height, 1.0))
        >= 0.65 * (x_gap[2] / max(page_width, 1.0))
    ):
        cut = (y_gap[0] + y_gap[1]) / 2
        upper = [block for block in blocks if block.bottom <= cut]
        lower = [block for block in blocks if block.top >= cut]
        if upper and lower and len(upper) + len(lower) == len(blocks):
            return _xy_cut_order(upper, page_width, page_height, depth + 1) + _xy_cut_order(
                lower, page_width, page_height, depth + 1
            )
    if valid_x:
        cut = (x_gap[0] + x_gap[1]) / 2
        left = [block for block in blocks if block.x1 <= cut]
        right = [block for block in blocks if block.x0 >= cut]
        if left and right and len(left) + len(right) == len(blocks):
            return _xy_cut_order(left, page_width, page_height, depth + 1) + _xy_cut_order(
                right, page_width, page_height, depth + 1
            )
    return sorted(blocks, key=lambda block: (block.top, block.x0))


def _canonical_tokens(text: str) -> list[str]:
    """Return punctuation-tolerant tokens for loss/duplication accounting."""

    return re.findall(
        r"[A-Za-z0-9]+(?:[./:%+\-][A-Za-z0-9%+]+)*",
        str(text or "").casefold(),
    )


def _counter_overlap(source: Counter[str], output: Counter[str]) -> int:
    return sum(min(count, output.get(token, 0)) for token, count in source.items())


def reconstruct_region_order(
    words: list[dict],
    page_width: float,
    page_height: float,
    table_blocks: list[_LayoutBlock] | None = None,
) -> _RegionOrderResult:
    """Reconstruct regions only when at least 99.5% of source tokens survive.

    The gate compares token multisets, not just output length. This rejects a
    result that repeats one region while dropping another, even when the total
    number of words looks unchanged. It also limits invented/duplicated output
    to 0.5% of source tokens.
    """

    table_blocks = table_blocks or []
    eligible_words: list[dict] = []
    for word in words:
        token = str(word.get("text", "")).strip()
        if not token:
            continue
        top = float(word.get("top", 0.0))
        bottom = float(word.get("bottom", top))
        # Match the conservative rotated-text rule in esg_reading_order.py.
        # Small non-upright flags can be font-metadata noise; only a tall run is
        # treated as genuinely rotated and excluded from linear prose ordering.
        if not bool(word.get("upright", True)) and (bottom - top) > 15.0:
            continue
        eligible_words.append(word)

    text_words = [
        word
        for word in eligible_words
        if not any(
            _bbox_contains_word((table.x0, table.top, table.x1, table.bottom), word)
            for table in table_blocks
        )
    ]

    lines = _line_segments(text_words, page_width)
    blocks = _merge_line_segments(lines) + list(table_blocks)
    ordered = _xy_cut_order(blocks, page_width, page_height)
    text = "\n\n".join(block.text.strip() for block in ordered if block.text.strip())

    source_counter: Counter[str] = Counter()
    for word in eligible_words:
        source_counter.update(_canonical_tokens(str(word.get("text", ""))))
    output_counter = Counter(_canonical_tokens(text))

    source_total = sum(source_counter.values())
    output_total = sum(output_counter.values())
    preserved = _counter_overlap(source_counter, output_counter)
    preservation_ratio = preserved / max(source_total, 1)
    extra_tokens = sum(
        max(0, count - source_counter.get(token, 0))
        for token, count in output_counter.items()
    )
    extra_token_ratio = extra_tokens / max(source_total, 1)

    accepted = (
        bool(text)
        and source_total > 0
        and preservation_ratio >= REGION_MIN_PRESERVATION_RATIO
        and extra_token_ratio <= REGION_MAX_EXTRA_TOKEN_RATIO
    )
    reason = (
        "strict_multiset_preservation_pass"
        if accepted
        else (
            f"preservation={preservation_ratio:.4f};"
            f"extra={extra_token_ratio:.4f};"
            f"required={REGION_MIN_PRESERVATION_RATIO:.4f}"
        )
    )
    return _RegionOrderResult(
        "reconstructed" if accepted else "ambiguous",
        text,
        source_total,
        output_total,
        len(blocks),
        preservation_ratio,
        extra_token_ratio,
        reason,
    )



def _pymupdf_words(page) -> list[dict]:
    """Convert PyMuPDF word tuples to the coordinate schema used by QA."""

    words: list[dict] = []
    for item in page.get_text("words", sort=False) or []:
        if len(item) < 5:
            continue
        x0, top, x1, bottom, text = item[:5]
        text = str(text or "").strip()
        if not text:
            continue
        width = max(0.0, float(x1) - float(x0))
        height = max(0.0, float(bottom) - float(top))
        likely_vertical_edge_text = (
            float(x0) >= float(page.rect.width) * 0.94
            and height >= 15.0
            and height > width * 1.25
        )
        words.append(
            {
                "x0": float(x0),
                "top": float(top),
                "x1": float(x1),
                "bottom": float(bottom),
                "text": text,
                "upright": not likely_vertical_edge_text,
                # Span size is not present in the word tuple. Bounding-box
                # height is a stable enough proxy for paragraph grouping.
                "size": height,
            }
        )
    return words


def _layout_metrics_from_words(
    text: str,
    words: list[dict],
    *,
    visual_objects: int,
) -> dict[str, int]:
    lines = [line for line in text.splitlines() if line.strip()]
    groups = _line_groups(words)
    huge_gap_lines = sum(
        1 for group in groups if len(group) >= 4 and _largest_horizontal_gap(group) >= 140
    )
    starts: list[int] = []
    for group in groups:
        if len(group) >= 2:
            starts.append(
                round(min(float(word.get("x0", 0.0)) for word in group) / 25) * 25
            )
    common_start_count = sum(1 for start in set(starts) if starts.count(start) >= 3)
    return {
        "word_count": len(words),
        "line_count": len(lines),
        "short_lines": sum(1 for line in lines if 3 <= len(line.strip()) <= 45),
        "metric_lines": sum(1 for line in lines if is_metric_row(line)),
        "huge_gap_lines": huge_gap_lines,
        "common_start_count": common_start_count,
        "visual_objects": int(visual_objects),
    }


def pymupdf_page_layout_grid_metrics(page, text: str, words: list[dict]) -> dict[str, int]:
    """Build the same conservative grid-risk evidence using PyMuPDF."""

    try:
        image_count = len(page.get_images(full=True) or [])
    except Exception:
        image_count = 0
    try:
        drawing_count = len(page.get_drawings() or [])
    except Exception:
        drawing_count = 0
    # Match the pdfplumber diagnostic: enough visual evidence matters, but
    # thousands of vector paths on a map must not dominate the score.
    visual_objects = image_count + min(drawing_count, 25)
    return _layout_metrics_from_words(text, words, visual_objects=visual_objects)


def _valid_pymupdf_table_candidates(page) -> list[_LayoutBlock]:
    """Extract real grid tables while rejecting decorative ESG geometry.

    Continuation pages often omit the left border around an indicator-code
    column. When code-like tokens sit immediately left of a detected two-column
    grid, they are aligned to row boxes and prepended as a first column.
    Adjacent fragments with the same geometry are merged before Markdown output.
    """

    if not hasattr(page, "find_tables"):
        return []
    try:
        finder = page.find_tables()
        tables = list(getattr(finder, "tables", []) or [])
    except Exception:
        return []

    page_words = _pymupdf_words(page)
    page_area = max(float(page.rect.width) * float(page.rect.height), 1.0)
    code_re = re.compile(r"^(?:G\d+-[A-Z0-9]+|[A-Z]{1,4}-[A-Z0-9]+)$", re.I)
    raw: list[dict] = []

    for table in tables:
        try:
            x0, top, x1, bottom = map(float, table.bbox)
            data = table.extract() or []
            row_boxes = [tuple(map(float, row.bbox)) for row in table.rows]
        except Exception:
            continue
        width = x1 - x0
        height = bottom - top
        if width < 60 or height < 20:
            continue
        area_ratio = (width * height) / page_area
        if area_ratio < 0.015 or area_ratio > 0.85:
            continue

        rows = [[_clean_table_cell(cell) for cell in row] for row in data]
        rows = [row for row in rows if any(row)]
        if not rows:
            continue

        # Recover an unbordered code column immediately left of the grid.
        augmented = False
        if max((len(row) for row in rows), default=0) == 2 and row_boxes:
            prefixes: list[str] = []
            for row_box in row_boxes[: len(rows)]:
                rtop, rbottom = row_box[1], row_box[3]
                tokens = [
                    str(word.get("text", "")).strip()
                    for word in page_words
                    if float(word.get("x1", 0.0)) <= x0 + 3.0
                    and float(word.get("x0", 0.0)) >= max(0.0, x0 - 60.0)
                    and rtop <= (float(word.get("top", 0.0)) + float(word.get("bottom", 0.0))) / 2 <= rbottom
                ]
                prefix = " ".join(token for token in tokens if token)
                prefixes.append(prefix if code_re.fullmatch(prefix) else "")
            code_hits = sum(bool(prefix) for prefix in prefixes)
            if code_hits and code_hits >= max(1, len(rows) // 2):
                rows = [[prefix] + row for prefix, row in zip(prefixes, rows)]
                x0 = max(0.0, x0 - 60.0)
                augmented = True

        row_count = len(rows)
        column_count = max((len(row) for row in rows), default=0)
        nonempty = sum(bool(cell) for row in rows for cell in row)
        fill_ratio = nonempty / max(row_count * column_count, 1)
        allow_single_row_fragment = augmented and row_count == 1 and nonempty >= 3
        if (
            column_count < 2
            or (nonempty < 4 and not allow_single_row_fragment)
            or fill_ratio < 0.30
            or (row_count < 2 and not allow_single_row_fragment)
        ):
            continue

        joined = " ".join(cell for row in rows for cell in row if cell)
        tokens = re.findall(r"\S+", joined)
        readable = re.findall(r"\b[A-Za-z]{3,}\b", joined)
        readable_ratio = len(readable) / max(len(tokens), 1)
        if readable_ratio < 0.55:
            continue

        raw.append(
            {
                "x0": x0,
                "top": top,
                "x1": x1,
                "bottom": bottom,
                "rows": rows,
                "cols": column_count,
            }
        )

    # Merge continuation fragments that share the same physical columns.
    merged: list[dict] = []
    for candidate in sorted(raw, key=lambda item: (item["top"], item["x0"])):
        target = None
        for existing in reversed(merged):
            same_width = (
                abs(existing["x0"] - candidate["x0"]) <= 8.0
                and abs(existing["x1"] - candidate["x1"]) <= 8.0
                and existing["cols"] == candidate["cols"]
            )
            gap = candidate["top"] - existing["bottom"]
            if same_width and 0 <= gap <= 28.0:
                target = existing
                break
        if target is None:
            merged.append(candidate)
        else:
            target["rows"].extend(candidate["rows"])
            target["bottom"] = max(target["bottom"], candidate["bottom"])

    # Add standard headers and consume their page words when all labels exist.
    header_tokens = {str(word.get("text", "")).strip().casefold(): word for word in page_words}
    has_gri_header = all(label in header_tokens for label in ("gri", "indicator", "description", "reported"))

    candidates: list[_LayoutBlock] = []
    for item in merged:
        rows = item["rows"]
        x0, top, x1, bottom = item["x0"], item["top"], item["x1"], item["bottom"]
        if has_gri_header and item["cols"] == 3:
            rows = [["GRI indicator", "Description", "Reported"]] + rows
            header_top = min(
                float(header_tokens[label].get("top", top))
                for label in ("gri", "indicator", "description", "reported")
            )
            top = min(top, header_top - 2.0)
        markdown = _table_to_markdown(rows)
        if not markdown:
            continue
        block = _LayoutBlock(x0, top, x1, bottom, markdown, kind="table")
        duplicate = False
        for existing in candidates:
            ix0, iy0 = max(existing.x0, x0), max(existing.top, top)
            ix1, iy1 = min(existing.x1, x1), min(existing.bottom, bottom)
            intersection = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
            union = (
                (existing.x1 - existing.x0) * (existing.bottom - existing.top)
                + (x1 - x0) * (bottom - top)
                - intersection
            )
            if union and intersection / union >= 0.85:
                duplicate = True
                break
        if not duplicate:
            candidates.append(block)
    return candidates


def _has_mixed_width_header(words: list[dict], page_width: float, page_height: float) -> bool:
    """Detect a full-width title above recurring body columns."""

    lines = _line_segments(words, page_width)
    if not lines:
        return False
    heights = sorted(max(1.0, line.bottom - line.top) for line in lines)
    median_height = heights[len(heights) // 2]
    return any(
        line.top <= page_height * 0.22
        and (line.x1 - line.x0) >= page_width * 0.58
        and line.font_size >= median_height * 1.15
        for line in lines
    )


def _finalize_layout_document(
    *,
    file_path: Path,
    company: str | None,
    parser_used: str,
    parser_policy: str,
    parser_reason: str,
    page_texts: dict[int, str],
    page_count: int,
    table_count: int,
    page_layout_metrics: list[tuple[int, dict[str, int]]],
    page_reading_order_status: dict[int, str],
    page_text_sources: dict[int, str],
    page_repair_methods: dict[int, str],
    page_table_candidates: dict[int, int],
    extraction_failed_pages: list[int],
    reading_order_repaired_pages: list[int],
    reading_order_unresolved_pages: list[int],
    text_layer_fallback_pages: list[int],
    page_change_reasons: dict[int, str],
) -> ParsedDocument:
    layout_risk_pages = layout_risk_page_numbers(page_layout_metrics)
    if layout_risk_pages and "layout_grid_risk_reported" not in parser_reason:
        parser_reason = f"{parser_reason};layout_grid_risk_reported"
    if reading_order_repaired_pages:
        parser_used = f"{parser_used}_column_order"
        parser_reason = (
            f"{parser_reason};reading_order_repair_pages="
            f"{len(reading_order_repaired_pages)}"
        )

    layout_numeric_risk_pages = sorted(set(layout_risk_pages))
    complex_reading_order_pages = sorted(
        set(reading_order_unresolved_pages) - set(layout_numeric_risk_pages)
    )
    text_light_pages = [
        page_number
        for page_number in range(1, page_count + 1)
        if classify_page_type(page_texts.get(page_number, "")) == "text_light"
    ]
    visual_only_pages = [
        page_number
        for page_number in range(1, page_count + 1)
        if classify_page_type(page_texts.get(page_number, "")) == "visual_only"
        and page_number not in extraction_failed_pages
    ]

    page_metadata: dict[int, dict] = {}
    repaired = set(reading_order_repaired_pages)
    layout_numeric = set(layout_numeric_risk_pages)
    complex_order = set(complex_reading_order_pages)
    failed = set(extraction_failed_pages)
    text_light = set(text_light_pages)
    visual_only = set(visual_only_pages)
    for page_number in range(1, page_count + 1):
        if page_number in failed:
            parse_status = "extraction_failed"
            visual_review_status = "required"
        elif page_number in layout_numeric:
            parse_status = "layout_numeric_risk"
            visual_review_status = "required"
        elif page_number in complex_order:
            parse_status = "complex_reading_order"
            visual_review_status = "sample_or_review"
        elif page_number in visual_only:
            parse_status = "image_only_nonsemantic"
            visual_review_status = "not_required"
        elif page_number in text_light:
            parse_status = "text_light"
            visual_review_status = "not_required"
        elif page_number in repaired:
            parse_status = "reading_order_repaired"
            visual_review_status = "not_required"
        else:
            parse_status = "ok"
            visual_review_status = "not_required"

        page_metadata[page_number] = {
            "page_type": classify_page_type(page_texts.get(page_number, "")),
            "parse_status": parse_status,
            "reading_order_status": page_reading_order_status.get(
                page_number, "not_applicable"
            ),
            "layout_risk": "true" if page_number in layout_numeric else "false",
            "visual_review_status": visual_review_status,
            "repair_method": page_repair_methods.get(page_number, "none"),
            "text_source": page_text_sources.get(page_number, "none"),
            "table_candidate_count": page_table_candidates.get(page_number, 0),
        }

    raw_text, page_spans = build_raw_text_and_page_spans(
        page_texts,
        page_count,
        page_metadata,
    )
    doc = ParsedDocument(
        source_file=str(file_path),
        company=company,
        doc_type="sustainability",
        parser_used=parser_used,
        raw_text=raw_text,
    ).finalize()
    doc.page_count = page_count
    doc.table_count = table_count
    doc.page_spans = page_spans
    doc.parser_policy = parser_policy
    doc.parser_reason = parser_reason
    doc.layout_risk_pages = ";".join(str(page) for page in layout_risk_pages)
    doc.layout_numeric_risk_pages = ";".join(
        str(page) for page in layout_numeric_risk_pages
    )
    doc.complex_reading_order_pages = ";".join(
        str(page) for page in complex_reading_order_pages
    )
    doc.text_light_pages = ";".join(str(page) for page in text_light_pages)
    doc.visual_only_pages = ";".join(str(page) for page in visual_only_pages)
    doc.extraction_failed_pages = ";".join(
        str(page) for page in extraction_failed_pages
    )
    doc.reading_order_repaired_pages = ";".join(
        str(page) for page in reading_order_repaired_pages
    )
    doc.reading_order_unresolved_pages = ";".join(
        str(page) for page in reading_order_unresolved_pages
    )
    doc.text_layer_fallback_pages = ";".join(
        str(page) for page in text_layer_fallback_pages
    )
    doc.page_text_change_reasons = ";".join(
        f"{page}:{page_change_reasons[page]}" for page in sorted(page_change_reasons)
    )
    return doc

def layout_grid_risk_from_metrics(metrics: dict[str, int]) -> bool:
    """Report pages that warrant targeted reading-order review.

    This is intentionally a conservative diagnostic. A page must contain both
    visual structure and metric-like text before it is considered a grid risk;
    ordinary two-column prose must not qualify merely because it has short
    lines and horizontal whitespace.
    """
    if metrics.get("word_count", 0) < LAYOUT_GRID_MIN_WORDS:
        return False

    common_start_count = metrics.get("common_start_count", 0)
    huge_gap_lines = metrics.get("huge_gap_lines", 0)
    short_lines = metrics.get("short_lines", 0)
    metric_lines = metrics.get("metric_lines", 0)
    visual_objects = metrics.get("visual_objects", 0)

    return (
        visual_objects >= LAYOUT_GRID_MIN_VISUAL_OBJECTS
        and common_start_count >= LAYOUT_GRID_MIN_COMMON_STARTS
        and short_lines >= LAYOUT_GRID_MIN_SHORT_LINES
        and huge_gap_lines >= LAYOUT_GRID_MIN_HUGE_GAP_LINES
        and metric_lines >= LAYOUT_GRID_MIN_METRIC_LINES
    )


def is_metric_row(line: str) -> bool:
    """Report a line that states a measured value.

    Deliberately inclusive: it cannot tell a stat card ("~151M pairs of shoes
    sold worldwide") from prose citing a figure ("up to 95% less water"), which
    are the same sentence in different layouts. Weighting by value density
    separates those two but then lets real card grids through, so this counts
    both and the page stays held. A false hold costs recall; a false pass ships
    a table whose numbers have been detached from their labels.
    """

    return bool(METRIC_VALUE_RE.search(line))


def page_layout_grid_metrics(
    page,
    text: str,
    words: list[dict] | None = None,
) -> dict[str, int]:
    if words is None:
        try:
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False) or []
        except Exception:
            words = []

    lines = [line for line in text.splitlines() if line.strip()]
    groups = _line_groups(words)
    huge_gap_lines = sum(
        1 for group in groups if len(group) >= 4 and _largest_horizontal_gap(group) >= 140
    )

    starts: list[int] = []
    for group in groups:
        if len(group) >= 2:
            starts.append(round(min(float(word.get("x0", 0.0)) for word in group) / 25) * 25)
    common_start_count = sum(1 for start in set(starts) if starts.count(start) >= 3)

    return {
        "word_count": len(words),
        "line_count": len(lines),
        "short_lines": sum(1 for line in lines if 3 <= len(line.strip()) <= 45),
        "metric_lines": sum(1 for line in lines if is_metric_row(line)),
        "huge_gap_lines": huge_gap_lines,
        "common_start_count": common_start_count,
        "visual_objects": len(getattr(page, "rects", []) or [])
        + len(getattr(page, "images", []) or [])
        + min(len(getattr(page, "curves", []) or []), 25),
    }


def layout_risk_page_numbers(page_metrics: list[tuple[int, dict[str, int]]]) -> list[int]:
    """Report the pages carrying the complete table/grid signature.

    A raw vector-object count is not part of this signal. Decorative ESG designs
    and vector-encoded artwork routinely exceed any such count while remaining
    ordinary prose, so counting them as grids quarantined readable pages and
    refused defensible column repairs.
    """

    return [
        page_number
        for page_number, metrics in page_metrics
        if layout_grid_risk_from_metrics(metrics)
    ]


def page_text_quality(text: str) -> int:
    """Rank one page's extracted text; known extractor artifacts count against it."""

    return max(
        0,
        len(text.strip()) - 8 * count_cid_artifacts(text) - 8 * count_garbled_chars(text),
    )


def page_text_is_defective(text: str) -> bool:
    """Report a page whose own extraction failed, independent of its document."""

    if len(text.strip()) <= MIN_PAGE_CHARS:
        return True
    return count_cid_artifacts(text) > 0 or count_garbled_chars(text) > 0


def prefer_pdfium_page(native: str, fallback: str) -> bool:
    """Decide whether PDFium reads one page better than pdfplumber did."""

    if not fallback.strip():
        return False
    native_quality = page_text_quality(native)
    fallback_quality = page_text_quality(fallback)
    if fallback_quality <= native_quality:
        return False
    # A page whose own extraction failed is replaced as soon as PDFium reads it
    # better at all.
    if page_text_is_defective(native):
        return True
    # A healthy page is replaced only when PDFium recovers materially more of it,
    # reusing the document-level gain ratio per page. PDFium is text-only and
    # cannot prove column order, so a marginal gain is never worth discarding a
    # verified coordinate repair.
    return fallback_quality >= native_quality * TEXT_FALLBACK_MIN_GAIN_RATIO


def pdfium_page_replacements(
    page_texts: dict[int, str],
    fallback_by_page: dict[int, str],
) -> list[int]:
    """Select the pages PDFium reads better, one page at a time.

    A document is not a unit of extraction quality: one CID-damaged page among a
    hundred healthy ones does not justify discarding the other ninety-nine. When
    a text layer really is broken throughout, every page fails this test on its
    own and the result is a whole-document replacement anyway -- without needing
    a separate document-wide branch that a single bad page can trigger.
    """

    return [
        page_number
        for page_number, text in sorted(page_texts.items())
        if prefer_pdfium_page(text, fallback_by_page.get(page_number, ""))
    ]


def _pages_with_text(page_texts: dict[int, str]) -> list[tuple[int, str]]:
    return [
        (page_number, text)
        for page_number, text in sorted(page_texts.items())
        if len(text.strip()) > MIN_PAGE_CHARS
    ]


def normalize_extracted_page_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(lines).strip()


def extract_with_pdfium(file_path: Path, log_pages: bool = False, company: str | None = None) -> list[tuple[int, str]]:
    if pdfium is None:
        return []

    pages_text: list[tuple[int, str]] = []
    pdf = pdfium.PdfDocument(str(file_path))
    try:
        for page_index in range(len(pdf)):
            page_number = page_index + 1
            if log_pages:
                print(f"[{company}] pypdfium page {page_number} RAM {mem():.2f}MB", flush=True)

            page = None
            text_page = None
            try:
                page = pdf[page_index]
                text_page = page.get_textpage()
                text = normalize_extracted_page_text(text_page.get_text_range() or "")
                if len(text.strip()) > MIN_PAGE_CHARS:
                    pages_text.append((page_number, text))
            except Exception as error:
                print(f"fail pypdfium page {page_number}: {error}", flush=True)
            finally:
                if text_page is not None and hasattr(text_page, "close"):
                    text_page.close()
                if page is not None and hasattr(page, "close"):
                    page.close()

            if page_number % 10 == 0:
                gc.collect()
    finally:
        pdf.close()

    return pages_text


class PDFParser:
    name = "pdfplumber"

    def _parse_with_pymupdf_layout(
        self,
        file_path: Path,
        company: str | None,
        *,
        log_pages: bool,
        parser_policy: str,
        parser_reason: str,
    ) -> ParsedDocument:
        if fitz is None:
            raise RuntimeError("PyMuPDF extraction was requested but pymupdf is unavailable")

        page_texts: dict[int, str] = {}
        page_layout_metrics: list[tuple[int, dict[str, int]]] = []
        page_reading_order_status: dict[int, str] = {}
        page_text_sources: dict[int, str] = {}
        page_repair_methods: dict[int, str] = {}
        page_table_candidates: dict[int, int] = {}
        extraction_failed_pages: list[int] = []
        reading_order_repaired_pages: list[int] = []
        reading_order_unresolved_pages: list[int] = []
        page_change_reasons: dict[int, str] = {}
        table_count = 0

        document = fitz.open(str(file_path))
        try:
            page_count = int(document.page_count)
            for page_index in range(page_count):
                page_number = page_index + 1
                if log_pages:
                    print(
                        f"[{company}] PyMuPDF page {page_number} RAM {mem():.2f}MB",
                        flush=True,
                    )
                try:
                    page = document.load_page(page_index)
                    text = normalize_extracted_page_text(
                        page.get_text("text", sort=False) or ""
                    )
                    words = _pymupdf_words(page)
                    native_layout_metrics = pymupdf_page_layout_grid_metrics(
                        page, text, words
                    )
                    table_blocks = _valid_pymupdf_table_candidates(page)
                    page_table_count = len(table_blocks)
                    page_table_candidates[page_number] = page_table_count
                    table_count += page_table_count

                    structural_grid_risk = layout_grid_risk_from_metrics(
                        native_layout_metrics
                    )
                    reading_order = reconstruct_column_order(
                        words,
                        float(page.rect.width),
                        float(page.rect.height),
                        structural_grid_risk=structural_grid_risk,
                    )
                    use_region_pass = (
                        bool(table_blocks)
                        or (
                            reading_order.status == "ambiguous"
                            and reading_order.reason != "navigation_contents_layout"
                            and not structural_grid_risk
                        )
                        or (
                            reading_order.status == "reconstructed"
                            and _has_mixed_width_header(
                                words,
                                float(page.rect.width),
                                float(page.rect.height),
                            )
                        )
                    )
                    region_order = (
                        reconstruct_region_order(
                            words,
                            float(page.rect.width),
                            float(page.rect.height),
                            table_blocks,
                        )
                        if use_region_pass
                        else None
                    )

                    if region_order is not None and region_order.status == "reconstructed":
                        text = normalize_extracted_page_text(region_order.text)
                        reading_order_repaired_pages.append(page_number)
                        page_reading_order_status[page_number] = "region_reconstructed"
                        repair_reason = (
                            PAGE_REASON_PYMUPDF_TABLE_ORDER
                            if table_blocks
                            else PAGE_REASON_PYMUPDF_REGION_ORDER
                        )
                        page_repair_methods[page_number] = repair_reason
                        page_change_reasons[page_number] = repair_reason
                    elif reading_order.status == "reconstructed":
                        text = normalize_extracted_page_text(reading_order.text)
                        reading_order_repaired_pages.append(page_number)
                        page_reading_order_status[page_number] = "reconstructed"
                        page_repair_methods[page_number] = PAGE_REASON_COLUMN_ORDER
                        page_change_reasons[page_number] = PAGE_REASON_COLUMN_ORDER
                    elif reading_order.status == "ambiguous":
                        reading_order_unresolved_pages.append(page_number)
                        page_reading_order_status[page_number] = "ambiguous"
                    else:
                        page_reading_order_status[page_number] = str(
                            reading_order.status or "native"
                        )

                    page_texts[page_number] = text
                    page_text_sources[page_number] = "pymupdf"
                    page_layout_metrics.append((page_number, native_layout_metrics))
                except Exception as error:
                    print(f"fail pymupdf page {page_number}: {error}", flush=True)
                    extraction_failed_pages.append(page_number)
                    page_texts[page_number] = ""
                    page_reading_order_status[page_number] = "extraction_failed"
                    page_text_sources[page_number] = "none"
                    page_table_candidates[page_number] = 0
        finally:
            document.close()

        return _finalize_layout_document(
            file_path=file_path,
            company=company,
            parser_used="pymupdf_layout",
            parser_policy=parser_policy,
            parser_reason=parser_reason,
            page_texts=page_texts,
            page_count=page_count,
            table_count=table_count,
            page_layout_metrics=page_layout_metrics,
            page_reading_order_status=page_reading_order_status,
            page_text_sources=page_text_sources,
            page_repair_methods=page_repair_methods,
            page_table_candidates=page_table_candidates,
            extraction_failed_pages=extraction_failed_pages,
            reading_order_repaired_pages=reading_order_repaired_pages,
            reading_order_unresolved_pages=reading_order_unresolved_pages,
            text_layer_fallback_pages=[],
            page_change_reasons=page_change_reasons,
        )

    def parse(self, file_path, company=None, **kwargs):
        file_path = Path(file_path)
        log_pages = kwargs.get("log_pages", False)
        prefer_pymupdf = bool(kwargs.get("prefer_pymupdf", False))
        prefer_pdfium = bool(kwargs.get("prefer_pdfium", False))
        if prefer_pymupdf and prefer_pdfium:
            raise ValueError("choose only one forced extraction backend")
        if prefer_pymupdf:
            pymupdf_policy = str(
                kwargs.get("pymupdf_policy") or AUTO_PYMUPDF_LAYOUT_POLICY
            )
            pymupdf_reason = str(
                kwargs.get("pymupdf_reason") or AUTO_PYMUPDF_LAYOUT_REASON
            )
            try:
                return self._parse_with_pymupdf_layout(
                    file_path,
                    company,
                    log_pages=bool(log_pages),
                    parser_policy=pymupdf_policy,
                    parser_reason=pymupdf_reason,
                )
            except Exception as error:
                # The automatic backend may fall back safely; an explicitly
                # forced/overridden backend must fail visibly rather than hide a
                # configuration error.
                if pymupdf_policy != AUTO_PYMUPDF_LAYOUT_POLICY:
                    raise
                print(
                    f"automatic PyMuPDF layout failed; falling back to "
                    f"pdfplumber: {type(error).__name__}: {error}",
                    flush=True,
                )
        prefer_pdfium_policy = str(kwargs.get("prefer_pdfium_policy") or "cli_forced_pdfium")
        prefer_pdfium_reason = str(kwargs.get("prefer_pdfium_reason") or "cli_forced_pdfium")
        pdfplumber_policy = str(
            kwargs.get("pdfplumber_policy") or AUTO_PDFPLUMBER_COLUMN_POLICY
        )
        pdfplumber_reason = str(
            kwargs.get("pdfplumber_reason") or AUTO_PDFPLUMBER_COLUMN_REASON
        )
        auto_layout_pdfium = bool(
            kwargs.get("auto_layout_pdfium", DEFAULT_AUTO_LAYOUT_PDFIUM)
        )
        pages_text: list[tuple[int, str]] = []
        page_texts: dict[int, str] = {}
        page_layout_metrics: list[tuple[int, dict[str, int]]] = []
        page_reading_order_status: dict[int, str] = {}
        page_text_sources: dict[int, str] = {}
        page_repair_methods: dict[int, str] = {}
        page_table_candidates: dict[int, int] = {}
        extraction_failed_pages: list[int] = []
        reading_order_repaired_pages: list[int] = []
        reading_order_unresolved_pages: list[int] = []
        text_layer_fallback_pages: list[int] = []
        page_change_reasons: dict[int, str] = {}
        page_count = 0
        table_count = 0

        with pdfplumber.open(file_path) as pdf:
            page_count = len(pdf.pages)

            for i in range(page_count):
                if log_pages:
                    print(f"[{company}] Page {i + 1} RAM {mem():.2f}MB", flush=True)
                page = None
                text = ""

                try:
                    page = pdf.pages[i]
                    text = normalize_extracted_page_text(page.extract_text_simple() or "")
                    try:
                        # "upright" must match the layout-QA extraction exactly:
                        # the reading-order reconstructor drops tall rotated
                        # runs, so parser and auditor must see the same words
                        # or every rotated-sidebar page is held on mismatch.
                        words = page.extract_words(
                            use_text_flow=False,
                            keep_blank_chars=False,
                            extra_attrs=["size", "upright"],
                        ) or []
                    except TypeError:
                        words = page.extract_words(
                            use_text_flow=False,
                            keep_blank_chars=False,
                        ) or []
                    native_layout_metrics = page_layout_grid_metrics(
                        page,
                        text,
                        words,
                    )
                    table_blocks = _valid_table_candidates(page)
                    page_table_count = len(table_blocks)
                    page_table_candidates[i + 1] = page_table_count
                    table_count += page_table_count

                    structural_grid_risk = layout_grid_risk_from_metrics(
                        native_layout_metrics
                    )
                    reading_order = reconstruct_column_order(
                        words,
                        float(page.width),
                        float(page.height),
                        structural_grid_risk=structural_grid_risk,
                    )
                    use_region_pass = (
                        bool(table_blocks)
                        or (
                            reading_order.status == "ambiguous"
                            and reading_order.reason != "navigation_contents_layout"
                            and not structural_grid_risk
                        )
                        or (
                            reading_order.status == "reconstructed"
                            and _has_mixed_width_header(
                                words, float(page.width), float(page.height)
                            )
                        )
                    )
                    if use_region_pass:
                        region_order = reconstruct_region_order(
                            words,
                            float(page.width),
                            float(page.height),
                            table_blocks,
                        )
                    else:
                        region_order = None

                    if region_order is not None and region_order.status == "reconstructed":
                        text = normalize_extracted_page_text(region_order.text)
                        reading_order_repaired_pages.append(i + 1)
                        page_reading_order_status[i + 1] = "region_reconstructed"
                        repair_reason = (
                            PAGE_REASON_TABLE_ORDER if table_blocks else PAGE_REASON_REGION_ORDER
                        )
                        page_repair_methods[i + 1] = repair_reason
                        page_change_reasons[i + 1] = repair_reason
                    elif reading_order.status == "reconstructed":
                        text = normalize_extracted_page_text(reading_order.text)
                        reading_order_repaired_pages.append(i + 1)
                        page_reading_order_status[i + 1] = "reconstructed"
                        page_repair_methods[i + 1] = PAGE_REASON_COLUMN_ORDER
                        page_change_reasons[i + 1] = PAGE_REASON_COLUMN_ORDER
                    elif reading_order.status == "ambiguous":
                        reading_order_unresolved_pages.append(i + 1)
                        page_reading_order_status[i + 1] = "ambiguous"
                    else:
                        page_reading_order_status[i + 1] = str(
                            reading_order.status or "native"
                        )

                    page_texts[i + 1] = text
                    page_text_sources[i + 1] = "pdfplumber"

                    page_layout_metrics.append(
                        (i + 1, native_layout_metrics)
                    )

                except Exception as e:
                    print(f"fail page {i + 1}: {e}", flush=True)
                    extraction_failed_pages.append(i + 1)
                    page_texts[i + 1] = ""
                    page_reading_order_status[i + 1] = "extraction_failed"
                    page_text_sources[i + 1] = "none"
                    page_table_candidates[i + 1] = 0

                finally:
                    if page is not None:
                        # pdfplumber keeps page/layout caches alive for the life
                        # of the PDF object. Clear them so large PDF batches stay
                        # bounded inside each short-lived worker process.
                        try:
                            page.flush_cache()
                        except AttributeError:
                            for attr in (
                                "_objects",
                                "_layout",
                                "_chars",
                                "_lines",
                                "_rects",
                                "_curves",
                                "_images",
                            ):
                                if hasattr(page, attr):
                                    try:
                                        delattr(page, attr)
                                    except AttributeError:
                                        pass

                    if hasattr(pdf, "_pages") and pdf._pages is not None:
                        pdf._pages[i] = None

                    page = None
                    text = None

                    if (i + 1) % 10 == 0:
                        gc.collect()

        pages_text = _pages_with_text(page_texts)
        parser_used = self.name
        parser_policy = pdfplumber_policy
        parser_reason = pdfplumber_reason
        # Keep layout-risk evidence in the parse index, but do not replace the
        # document text unless a caller explicitly opts into a targeted pilot.
        layout_risk_pages = layout_risk_page_numbers(page_layout_metrics)
        if prefer_pdfium:
            fallback_pages = extract_with_pdfium(file_path, log_pages=log_pages, company=company)
            if not fallback_pages:
                raise RuntimeError(
                    "pypdfium extraction was requested but returned no extractable text"
                )
            fallback_by_page = dict(fallback_pages)
            # Keep one physical-page record even when PDFium emits no text for a
            # photo/divider page. Replace only pages PDFium actually extracted.
            for page_number, fallback_text in fallback_pages:
                page_texts[page_number] = fallback_text
                page_text_sources[page_number] = "pypdfium"
                page_repair_methods[page_number] = PAGE_REASON_PDFIUM_FORCED
                page_reading_order_status[page_number] = "text_layer_forced"
            pages_text = _pages_with_text(page_texts)
            parser_used = "pypdfium_text_forced"
            parser_policy = prefer_pdfium_policy
            parser_reason = prefer_pdfium_reason
            reading_order_repaired_pages = []
            text_layer_fallback_pages = [page for page, _ in fallback_pages]
            page_change_reasons = {
                page: PAGE_REASON_PDFIUM_FORCED for page, _ in fallback_pages
            }
        elif should_try_text_layer_fallback(pages_text, page_count) or (
            auto_layout_pdfium and layout_risk_pages
        ):
            fallback_pages = extract_with_pdfium(file_path, log_pages=log_pages, company=company)
            fallback_by_page = dict(fallback_pages)
            prefers_text_layer = should_use_text_layer_fallback(
                pages_text,
                fallback_pages,
                page_count,
            )
            prefers_layout_grid = bool(
                auto_layout_pdfium and layout_risk_pages and fallback_pages
            )
            if prefers_text_layer or prefers_layout_grid:
                text_layer_fallback_pages = pdfium_page_replacements(
                    page_texts,
                    fallback_by_page,
                )

            if text_layer_fallback_pages:
                for page_number in text_layer_fallback_pages:
                    page_texts[page_number] = fallback_by_page[page_number]
                    page_text_sources[page_number] = "pypdfium"
                    page_repair_methods[page_number] = PAGE_REASON_PDFIUM_PAGE
                    page_reading_order_status[page_number] = "text_layer_replaced"
                    page_change_reasons[page_number] = PAGE_REASON_PDFIUM_PAGE
                replaced = set(text_layer_fallback_pages)
                # A repaired page can only be replaced when its own pdfplumber
                # glyphs were broken. Drop those repairs from the evidence rather
                # than reporting a repair the emitted text does not contain.
                reading_order_repaired_pages = [
                    page for page in reading_order_repaired_pages if page not in replaced
                ]
                pages_text = _pages_with_text(page_texts)
                parser_used = f"{self.name}+pypdfium_text_pages"
                parser_policy = (
                    AUTO_TEXT_LAYER_FALLBACK_POLICY
                    if prefers_text_layer
                    else AUTO_LAYOUT_GRID_FALLBACK_POLICY
                )
                parser_reason = (
                    "low_text_or_cid_text_layer_risk"
                    if prefers_text_layer
                    else "layout_grid_risk"
                )
                parser_reason = (
                    f"{parser_reason};pypdfium_text_pages={len(text_layer_fallback_pages)}"
                )
            elif prefers_text_layer:
                # PDFium looked better across the document, but no individual
                # page is actually improved by it. That is a healthy text layer
                # whose document-level comparison was skewed by a handful of
                # artifacts, so nothing is replaced.
                parser_reason = f"{parser_reason};pypdfium_text_no_page_improved"
            elif auto_layout_pdfium and layout_risk_pages and not fallback_pages:
                parser_reason = "layout_grid_risk_pdfium_unavailable"

        if layout_risk_pages and not auto_layout_pdfium:
            parser_reason = f"{parser_reason};layout_grid_risk_reported"

        if reading_order_repaired_pages:
            parser_used = f"{parser_used}_column_order"
            parser_reason = (
                f"{parser_reason};coordinate_column_order_pages="
                f"{len(reading_order_repaired_pages)}"
            )

        layout_numeric_risk_pages = sorted(set(layout_risk_pages))
        complex_reading_order_pages = sorted(
            set(reading_order_unresolved_pages) - set(layout_numeric_risk_pages)
        )
        text_light_pages = [
            page_number
            for page_number in range(1, page_count + 1)
            if classify_page_type(page_texts.get(page_number, "")) == "text_light"
        ]
        visual_only_pages = [
            page_number
            for page_number in range(1, page_count + 1)
            if classify_page_type(page_texts.get(page_number, "")) == "visual_only"
            and page_number not in extraction_failed_pages
        ]

        page_metadata: dict[int, dict] = {}
        repaired = set(reading_order_repaired_pages)
        layout_numeric = set(layout_numeric_risk_pages)
        complex_order = set(complex_reading_order_pages)
        failed = set(extraction_failed_pages)
        text_light = set(text_light_pages)
        visual_only = set(visual_only_pages)
        for page_number in range(1, page_count + 1):
            if page_number in failed:
                parse_status = "extraction_failed"
                visual_review_status = "required"
            elif page_number in layout_numeric:
                parse_status = "layout_numeric_risk"
                visual_review_status = "required"
            elif page_number in complex_order:
                parse_status = "complex_reading_order"
                visual_review_status = "sample_or_review"
            elif page_number in visual_only:
                parse_status = "image_only_nonsemantic"
                visual_review_status = "not_required"
            elif page_number in text_light:
                parse_status = "text_light"
                visual_review_status = "not_required"
            elif page_number in repaired:
                parse_status = "reading_order_repaired"
                visual_review_status = "not_required"
            else:
                parse_status = "ok"
                visual_review_status = "not_required"

            page_metadata[page_number] = {
                "page_type": classify_page_type(page_texts.get(page_number, "")),
                "parse_status": parse_status,
                "reading_order_status": page_reading_order_status.get(
                    page_number, "not_applicable"
                ),
                "layout_risk": "true" if page_number in layout_numeric else "false",
                "visual_review_status": visual_review_status,
                "repair_method": page_repair_methods.get(page_number, "none"),
                "text_source": page_text_sources.get(page_number, "none"),
                "table_candidate_count": page_table_candidates.get(page_number, 0),
            }

        raw_text, page_spans = build_raw_text_and_page_spans(
            page_texts,
            page_count,
            page_metadata,
        )
        doc = ParsedDocument(
            source_file=str(file_path),
            company=company,
            doc_type="sustainability",
            parser_used=parser_used,
            raw_text=raw_text,
        ).finalize()
        doc.page_count = page_count
        doc.table_count = table_count
        doc.page_spans = page_spans
        doc.parser_policy = parser_policy
        doc.parser_reason = parser_reason
        doc.layout_risk_pages = ";".join(str(page) for page in layout_risk_pages)
        doc.layout_numeric_risk_pages = ";".join(
            str(page) for page in layout_numeric_risk_pages
        )
        doc.complex_reading_order_pages = ";".join(
            str(page) for page in complex_reading_order_pages
        )
        doc.text_light_pages = ";".join(str(page) for page in text_light_pages)
        doc.visual_only_pages = ";".join(str(page) for page in visual_only_pages)
        doc.extraction_failed_pages = ";".join(
            str(page) for page in extraction_failed_pages
        )
        doc.reading_order_repaired_pages = ";".join(
            str(page) for page in reading_order_repaired_pages
        )
        doc.reading_order_unresolved_pages = ";".join(
            str(page) for page in reading_order_unresolved_pages
        )
        doc.text_layer_fallback_pages = ";".join(
            str(page) for page in text_layer_fallback_pages
        )
        doc.page_text_change_reasons = ";".join(
            f"{page}:{page_change_reasons[page]}" for page in sorted(page_change_reasons)
        )
        return doc


def discover(
    root: str | Path,
    ticker: str | None = None,
    pdf_file: str | None = None,
) -> dict[str, list[Path]]:
    root = Path(root)
    out: dict[str, list[Path]] = {}
    selected_pdf = (pdf_file or "").strip()

    if not root.exists():
        return out

    ticker_dirs = [root / ticker.upper()] if ticker else [p for p in root.iterdir() if p.is_dir()]
    for ticker_dir in ticker_dirs:
        if not ticker_dir.exists() or not ticker_dir.is_dir():
            continue
        pdfs = sorted(ticker_dir.glob("*.pdf"))
        if selected_pdf:
            selected_stem = Path(selected_pdf).stem
            pdfs = [
                pdf
                for pdf in pdfs
                if pdf.name == selected_pdf or pdf.stem == selected_stem
            ]
        if pdfs:
            out[ticker_dir.name.upper()] = pdfs

    return out


def load_parser_overrides(path: str | Path | None) -> dict[tuple[str, str], dict]:
    if path is None:
        return {}

    override_path = Path(path)
    if not override_path.exists():
        return {}

    overrides: dict[tuple[str, str], dict] = {}
    with override_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ticker = str(row.get("ticker", "")).strip().upper()
            pdf_file = str(row.get("pdf_file", "")).strip()
            mode = str(row.get("parser_mode", "")).strip().lower()
            active = str(row.get("active", "true")).strip().lower()
            if not ticker or not pdf_file or active in {"false", "0", "no", "n"}:
                continue
            if mode not in {"auto", "pdfplumber", "pypdfium", "pymupdf"}:
                raise ValueError(
                    f"Unsupported parser_mode={mode!r} in {override_path} "
                    f"for {ticker} {pdf_file}"
                )

            normalised = {
                field: str(row.get(field, "")).strip()
                for field in PARSER_OVERRIDE_FIELDS
            }
            normalised["ticker"] = ticker
            normalised["parser_mode"] = mode
            overrides[(ticker, pdf_file)] = normalised
            overrides[(ticker, Path(pdf_file).stem)] = normalised

    return overrides


def parser_override_for(
    overrides: dict[tuple[str, str], dict],
    ticker: str,
    pdf: Path,
) -> dict | None:
    ticker = ticker.upper()
    return overrides.get((ticker, pdf.name)) or overrides.get((ticker, pdf.stem))


def parser_request_for_pdf(
    *,
    ticker: str,
    pdf: Path,
    overrides: dict[tuple[str, str], dict],
    prefer_pdfium: bool,
    prefer_pymupdf: bool = False,
) -> dict[str, str | bool]:
    if prefer_pdfium and prefer_pymupdf:
        raise ValueError("choose only one forced extraction backend")
    if prefer_pymupdf:
        return {
            "prefer_pdfium": False,
            "prefer_pymupdf": True,
            "parser_policy": "cli_forced_pymupdf_layout",
            "parser_reason": "cli_forced_pymupdf_layout",
        }
    if prefer_pdfium:
        return {
            "prefer_pdfium": True,
            "prefer_pymupdf": False,
            "parser_policy": "cli_forced_pdfium",
            "parser_reason": "cli_forced_pdfium",
        }

    override = parser_override_for(overrides, ticker, pdf)
    if override and override.get("parser_mode") == "pymupdf":
        reason = str(override.get("reason") or "parser_override_pymupdf")
        return {
            "prefer_pdfium": False,
            "prefer_pymupdf": True,
            "parser_policy": "override_pymupdf_layout",
            "parser_reason": reason,
        }
    if override and override.get("parser_mode") == "pypdfium":
        reason = str(override.get("reason") or "parser_override")
        return {
            "prefer_pdfium": True,
            "prefer_pymupdf": False,
            "parser_policy": "override_pdfium",
            "parser_reason": reason,
        }

    if override and override.get("parser_mode") == "pdfplumber":
        reason = str(override.get("reason") or "parser_override_pdfplumber")
        return {
            "prefer_pdfium": False,
            "prefer_pymupdf": False,
            "parser_policy": "override_pdfplumber",
            "parser_reason": reason,
        }

    # Keep the default deterministic across machines. PyMuPDF is an opt-in
    # table/layout backend until its table-specific QA path is calibrated on a
    # wider visual sample. Merely installing ``fitz`` must not change output.
    return {
        "prefer_pdfium": False,
        "prefer_pymupdf": False,
        "parser_policy": AUTO_PDFPLUMBER_COLUMN_POLICY,
        "parser_reason": AUTO_PDFPLUMBER_COLUMN_REASON,
    }


def _set_memory_limit(max_mb=2048):
    try:
        import resource
    except ImportError:
        return

    try:
        resource.setrlimit(
            resource.RLIMIT_AS,
            (max_mb * 1024 * 1024, resource.RLIM_INFINITY),
        )
    except (OSError, ValueError):
        return


def source_fingerprint(file_path: str | Path) -> dict[str, str | int]:
    """Return a stable, content-based fingerprint for a source PDF."""
    path = Path(file_path)

    # A PDF replacement can occur while a long-running process is alive. Retry
    # once if the file changes while it is being hashed so the index never
    # records metadata from one version with a hash from another.
    for _ in range(2):
        before = path.stat()
        hasher = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                hasher.update(chunk)
        after = path.stat()

        if (
            before.st_size == after.st_size
            and before.st_mtime_ns == after.st_mtime_ns
        ):
            return {
                "source_size_bytes": after.st_size,
                "source_mtime_utc": datetime.fromtimestamp(
                    after.st_mtime, UTC
                ).isoformat(timespec="microseconds"),
                "source_sha256": hasher.hexdigest(),
            }

    raise RuntimeError(f"Source PDF changed while fingerprinting: {path}")


@dataclass(frozen=True)
class ParseSourceSelection:
    path: Path
    kind: str
    approval_status: str
    reason: str
    approval: dict | None = None


def _normalized_locator(value: str | Path) -> str:
    try:
        return os.path.normcase(os.path.normpath(str(Path(value).resolve())))
    except (OSError, RuntimeError):
        return os.path.normcase(os.path.normpath(str(value)))


def select_parse_source(
    source_pdf: Path,
    ticker: str,
    ocr_root: str | Path | None,
    *,
    source_sha256: str,
    approvals: list[dict] | None,
) -> ParseSourceSelection:
    """Return the PDF to parse while keeping ``source_pdf`` canonical.

    The normal pipeline parses the downloaded PDF from the raw Drive mirror.
    When ``--ocr-root`` is explicitly supplied, a sidecar searchable PDF can be
    used as the extraction source. The caller still uses the raw PDF for its
    canonical filename, output stem, and downstream ``source_pdf`` identity.
    """
    if ocr_root is not None:
        ocr_root_path = Path(ocr_root).resolve()
        ocr_ticker_root = (ocr_root_path / ticker.upper()).resolve()
        candidate_paths = {
            _normalized_locator(path): path
            for path in ocr_ticker_root.glob("*.pdf")
            if path.is_file()
        }
        saw_ocr_candidate = bool(candidate_paths)
        saw_stale_original = False
        for approval in approvals or []:
            status = str(approval.get("approval_status") or "").strip().lower()
            state = str(approval.get("state") or "").strip().lower()
            if status not in {"approved", "approve"} or state not in {"active", "current"}:
                continue
            required = (
                "logical_source_id",
                "original_source_version_id",
                "original_sha256",
                "ocr_artifact_id",
                "ocr_artifact_sha256",
                "ocr_path",
                "reviewer",
                "approval_date",
                "reason",
            )
            if any(not str(approval.get(field) or "").strip() for field in required):
                continue
            approved_original_hash = str(approval.get("original_sha256") or "").lower()
            approved_version = str(approval.get("original_source_version_id") or "")
            if approved_original_hash != source_sha256.lower() or approved_version != source_version_id(source_sha256):
                saw_stale_original = True
                continue
            raw_ocr_path = Path(str(approval.get("ocr_path")))
            candidate = raw_ocr_path if raw_ocr_path.is_absolute() else ocr_root_path / raw_ocr_path
            candidate = candidate.resolve()
            candidate_path = _normalized_locator(candidate)
            if candidate_path not in candidate_paths:
                continue
            candidate = candidate_paths[candidate_path]
            candidate_hash = str(source_fingerprint(candidate)["source_sha256"])
            if str(approval.get("ocr_artifact_sha256") or "").lower() != candidate_hash.lower():
                return ParseSourceSelection(source_pdf, "raw", "ignored", "approved_ocr_hash_mismatch", approval)
            if str(approval.get("ocr_artifact_id") or "") != extraction_artifact_id(
                "ocr_derivative", candidate_hash
            ):
                continue
            return ParseSourceSelection(
                candidate,
                "ocr",
                "approved",
                "approved_hash_bound_ocr_selected",
                approval,
            )
        if saw_stale_original:
            return ParseSourceSelection(source_pdf, "raw", "ignored", "approved_ocr_original_hash_mismatch", None)
        if saw_ocr_candidate:
            return ParseSourceSelection(source_pdf, "raw", "ignored", "unapproved_ocr_ignored", None)
    return ParseSourceSelection(source_pdf, "raw", "not_applicable", "raw_source_selected")


def _select_parse_source(
    source_pdf: Path,
    ticker: str,
    ocr_root: str | Path | None,
    *,
    source_sha256: str = "",
    approvals: list[dict] | None = None,
) -> tuple[Path, str]:
    """Compatibility wrapper; filename matches alone can never select OCR."""

    selection = select_parse_source(
        source_pdf,
        ticker,
        ocr_root,
        source_sha256=source_sha256,
        approvals=approvals,
    )
    return selection.path, selection.kind


def load_catalog(path: str | Path | None) -> dict[tuple[str, str], dict]:
    catalog: dict[tuple[str, str], dict] = {}
    for row in read_intake_csv(path):
        if str(row.get("artifact_role") or "").strip() != "original":
            continue
        ticker = str(row.get("observed_ticker") or "").strip().upper()
        pdf_file = str(row.get("pdf_file") or "").strip()
        if ticker and pdf_file:
            catalog[(ticker, pdf_file)] = row
    return catalog


def identity_metadata(
    source_pdf: Path,
    ticker: str,
    source_sha256: str,
    parse_source_kind: str,
    parse_source_sha256: str,
    catalog_row: dict | None,
    selection: ParseSourceSelection,
) -> dict[str, str]:
    catalog_row = catalog_row or {}
    role = "ocr_derivative" if parse_source_kind == "ocr" else "original"
    return {
        "logical_source_id": str(catalog_row.get("logical_source_id") or logical_source_id(f"sha256:{source_sha256}")),
        "source_version_id": str(catalog_row.get("source_version_id") or source_version_id(source_sha256)),
        "file_alias_id": str(catalog_row.get("file_alias_id") or file_alias_id(str(source_pdf.resolve()))),
        "extraction_artifact_id": extraction_artifact_id(role, parse_source_sha256),
        "ocr_approval_status": selection.approval_status,
        "ocr_selection_reason": selection.reason,
    }


def _parse_source_metadata(
    parse_source_pdf: Path,
    parse_source_kind: str,
    fingerprint: dict[str, str | int] | None,
) -> dict[str, str | int]:
    """Convert an extraction-input fingerprint to its index field names."""
    metadata: dict[str, str | int] = {
        "parse_source_kind": parse_source_kind,
        "parse_source_pdf": display_path(parse_source_pdf),
    }
    if fingerprint is not None:
        metadata.update(
            {
                "parse_source_size_bytes": fingerprint["source_size_bytes"],
                "parse_source_mtime_utc": fingerprint["source_mtime_utc"],
                "parse_source_sha256": fingerprint["source_sha256"],
            }
        )
    return metadata


def _output_paths(out_root: str | Path, ticker: str, file_path: Path) -> tuple[Path, Path]:
    base = Path(out_root) / ticker
    return base / f"{file_path.stem}.txt", base / f"{file_path.stem}.pages.csv"


def _atomic_write(out_path: Path, write_contents) -> None:
    """Write one file through ``<filename>.tmp`` and atomically replace it."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(f"{out_path.name}.tmp")
    try:
        with tmp_path.open("w", newline="", encoding="utf-8") as f:
            write_contents(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, out_path)
    finally:
        # A stale temporary file is safe to overwrite on the next attempt, but
        # remove it after ordinary write failures to keep the output tree tidy.
        if tmp_path.exists():
            tmp_path.unlink()


def _write_text(out_path: Path, text: str) -> None:
    _atomic_write(out_path, lambda f: f.write(text))


def _write_page_map(out_path: Path, page_spans: list[dict]) -> None:
    def write_contents(f) -> None:
        writer = csv.DictWriter(f, fieldnames=PAGE_MAP_FIELDS)
        writer.writeheader()
        writer.writerows(page_spans)

    _atomic_write(out_path, write_contents)


def _normalise_index_row(row: dict) -> dict:
    return {
        field: "" if row.get(field) is None else row.get(field, "")
        for field in PARSE_INDEX_FIELDS
    }


def _index_row_key(row: dict) -> tuple[str, str]:
    """Use the source folder's natural unique identifier for parser rows."""
    return (str(row.get("ticker", "")).upper(), str(row.get("pdf_file", "")))


def _index_rows_by_key(rows: list[dict]) -> dict[tuple[str, str], dict]:
    # Last row wins for legacy duplicates, which is normally the most recent
    # checkpoint. New checkpoints are always unique by this key.
    return {
        _index_row_key(normalised): normalised
        for row in rows
        if (normalised := _normalise_index_row(row))
    }


def _fingerprints_match(
    row: dict,
    fingerprint: dict[str, str | int],
    row_fields: list[str],
) -> bool:
    return all(
        row.get(row_field, "") not in ("", None)
        and str(row.get(row_field)) == str(fingerprint.get(source_field, ""))
        for row_field, source_field in zip(row_fields, SOURCE_FINGERPRINT_FIELDS)
    )


def _source_fingerprints_match(row: dict, fingerprint: dict[str, str | int]) -> bool:
    return _fingerprints_match(row, fingerprint, SOURCE_FINGERPRINT_FIELDS)


def _parse_source_fingerprints_match(
    row: dict, fingerprint: dict[str, str | int]
) -> bool:
    return _fingerprints_match(row, fingerprint, PARSE_SOURCE_FINGERPRINT_FIELDS)


def _outputs_exist(parsed_text_file: Path, page_map_file: Path) -> bool:
    return parsed_text_file.is_file() and page_map_file.is_file()


def _parser_policy_matches_request(
    row: dict,
    expected_parser_policy: str,
    auto_layout_pdfium: bool,
) -> bool:
    """Decide whether a completed row remains valid under the requested policy.

    The layout fallback is now report-only by default. Rows produced by the
    former automatic layout replacement must be rebuilt on the next resume so
    the old text/page-map baseline cannot survive silently.
    """
    actual_policy = row.get("parser_policy", "").strip()
    if expected_parser_policy == AUTO_PDFPLUMBER_COLUMN_POLICY:
        # Only versioned text-layer rows may be resumed. Their unversioned
        # predecessors replaced every page of a document with PDFium text as soon
        # as one page tripped the document-level check, so they carry none of the
        # coordinate repairs that do apply to the rest of the document. Treating
        # them as current is what kept that stale text alive across resumes.
        return actual_policy in {
            AUTO_PDFPLUMBER_COLUMN_POLICY,
            AUTO_TEXT_LAYER_FALLBACK_POLICY,
        }
    if expected_parser_policy:
        return actual_policy == expected_parser_policy
    if actual_policy in {
        "override_pdfium",
        "override_pdfplumber",
        "cli_forced_pdfium",
    }:
        return False
    return auto_layout_pdfium or actual_policy not in LAYOUT_GRID_FALLBACK_POLICIES


def _is_complete_row(
    row: dict | None,
    source_pdf: Path,
    source_metadata: dict[str, str | int],
    parse_source_pdf: Path,
    parse_source_kind: str,
    parse_source_metadata: dict[str, str | int],
    parsed_text_file: Path,
    page_map_file: Path,
    prefer_pdfium: bool = False,
    prefer_pymupdf: bool = False,
    expected_parser_policy: str = "",
    auto_layout_pdfium: bool = DEFAULT_AUTO_LAYOUT_PDFIUM,
    identity: dict[str, str] | None = None,
) -> bool:
    if row is None:
        return False

    identity = identity or {}
    return (
        row.get("status", "").strip().lower() in {"parsed", "ocr_required"}
        and row.get("source_pdf", "") == display_path(source_pdf)
        and row.get("parse_source_kind", "") == parse_source_kind
        and row.get("parse_source_pdf", "") == display_path(parse_source_pdf)
        and row.get("parsed_text_file", "") == display_path(parsed_text_file)
        and row.get("page_map_file", "") == display_path(page_map_file)
        and (
            not prefer_pdfium
            or "pypdfium" in row.get("parser_used", "").strip()
        )
        and (
            not prefer_pymupdf
            or "pymupdf" in row.get("parser_used", "").strip()
        )
        and _parser_policy_matches_request(
            row,
            expected_parser_policy,
            auto_layout_pdfium,
        )
        and _source_fingerprints_match(row, source_metadata)
        and _parse_source_fingerprints_match(row, parse_source_metadata)
        and all(str(row.get(field) or "") == value for field, value in identity.items())
        and _outputs_exist(parsed_text_file, page_map_file)
    )


def _failed_row(
    file_path: Path,
    ticker: str,
    out_root: str | Path,
    source_metadata: dict[str, str | int] | None,
    error_message: str,
    *,
    parse_source_pdf: Path | None = None,
    parse_source_kind: str = "raw",
    parse_source_metadata: dict[str, str | int] | None = None,
    identity: dict[str, str] | None = None,
) -> dict:
    parse_source_pdf = parse_source_pdf or file_path
    out_file, page_map_file = _output_paths(out_root, ticker, file_path)
    return {
        "ticker": ticker,
        "pdf_file": file_path.name,
        "source_pdf": display_path(file_path),
        **(identity or {}),
        **(source_metadata or {}),
        **_parse_source_metadata(
            parse_source_pdf,
            parse_source_kind,
            parse_source_metadata,
        ),
        "parsed_text_file": display_path(out_file),
        "page_map_file": display_path(page_map_file),
        "status": "failed",
        "error_message": error_message,
        "page_count": 0,
        "char_count": 0,
        "table_count": 0,
        "parser_used": "",
        "parser_policy": "",
        "parser_reason": "",
        "layout_risk_pages": "",
        "layout_numeric_risk_pages": "",
        "complex_reading_order_pages": "",
        "text_light_pages": "",
        "visual_only_pages": "",
        "extraction_failed_pages": "",
        "reading_order_repaired_pages": "",
        "reading_order_unresolved_pages": "",
        "text_layer_fallback_pages": "",
        "page_text_change_reasons": "",
        "content_hash": "",
        "parsed_at": "",
        "quality_flags": "",
        "possible_wrong_doc_type": "false",
        "readable_word_count": 0,
        "readable_word_ratio": "0.0000",
        "chars_per_page": "0.0",
        "garbled_char_count": 0,
    }


def _parse_one(args):
    _set_memory_limit(2048)
    (
        file_path,
        parse_source_pdf,
        parse_source_kind,
        ticker,
        out_root,
        log_pages,
        source_metadata,
        parse_source_metadata,
        prefer_pdfium,
        prefer_pymupdf,
        parser_policy,
        parser_reason,
        auto_layout_pdfium,
        identity,
    ) = args
    file_path = Path(file_path)
    parse_source_pdf = Path(parse_source_pdf)
    out_root = Path(out_root)
    out_file, page_map_file = _output_paths(out_root, ticker, file_path)
    parser = PDFParser()

    try:
        doc = parser.parse(
            parse_source_pdf,
            company=ticker,
            log_pages=log_pages,
            prefer_pdfium=prefer_pdfium,
            prefer_pymupdf=prefer_pymupdf,
            prefer_pdfium_policy=parser_policy if prefer_pdfium else None,
            prefer_pdfium_reason=parser_reason if prefer_pdfium else None,
            pymupdf_policy=parser_policy if prefer_pymupdf else None,
            pymupdf_reason=parser_reason if prefer_pymupdf else None,
            pdfplumber_policy=(
                parser_policy if not prefer_pdfium and not prefer_pymupdf else None
            ),
            pdfplumber_reason=(
                parser_reason if not prefer_pdfium and not prefer_pymupdf else None
            ),
            auto_layout_pdfium=auto_layout_pdfium,
        )
        nonspace_chars = len("".join(doc.raw_text.split()))
        status = "parsed" if nonspace_chars >= OCR_MIN_NONSPACE_CHARS else "ocr_required"

        _write_text(out_file, doc.raw_text)
        _write_page_map(page_map_file, getattr(doc, "page_spans", []))
        quality = text_quality_metrics(
            doc.raw_text,
            getattr(doc, "page_count", 0),
            doc.char_count,
        )
        parse_risk_flags = [
            flag for flag in quality["quality_flags"].split("|") if flag
        ]
        if getattr(doc, "layout_numeric_risk_pages", ""):
            parse_risk_flags.append("layout_numeric_risk")
        if getattr(doc, "complex_reading_order_pages", ""):
            parse_risk_flags.append("complex_reading_order")
        if getattr(doc, "extraction_failed_pages", ""):
            parse_risk_flags.append("page_extraction_failed")
        quality["quality_flags"] = "|".join(dict.fromkeys(parse_risk_flags))

        return {
            "ticker": ticker,
            "pdf_file": file_path.name,
            "source_pdf": display_path(file_path),
            **identity,
            **source_metadata,
            **_parse_source_metadata(
                parse_source_pdf,
                parse_source_kind,
                parse_source_metadata,
            ),
            "parsed_text_file": display_path(out_file),
            "page_map_file": display_path(page_map_file),
            "status": status,
            "error_message": "",
            "page_count": getattr(doc, "page_count", 0),
            "char_count": doc.char_count,
            "table_count": doc.table_count,
            "parser_used": getattr(doc, "parser_used", ""),
            "parser_policy": getattr(doc, "parser_policy", ""),
            "parser_reason": getattr(doc, "parser_reason", ""),
            "layout_risk_pages": getattr(doc, "layout_risk_pages", ""),
            "layout_numeric_risk_pages": getattr(
                doc, "layout_numeric_risk_pages", ""
            ),
            "complex_reading_order_pages": getattr(
                doc, "complex_reading_order_pages", ""
            ),
            "text_light_pages": getattr(doc, "text_light_pages", ""),
            "visual_only_pages": getattr(doc, "visual_only_pages", ""),
            "extraction_failed_pages": getattr(
                doc, "extraction_failed_pages", ""
            ),
            "reading_order_repaired_pages": getattr(
                doc, "reading_order_repaired_pages", ""
            ),
            "reading_order_unresolved_pages": getattr(
                doc, "reading_order_unresolved_pages", ""
            ),
            "text_layer_fallback_pages": getattr(doc, "text_layer_fallback_pages", ""),
            "page_text_change_reasons": getattr(doc, "page_text_change_reasons", ""),
            # Hash the exact bytes downstream stages read. Text-mode writes can
            # change line endings on Windows, so hashing doc.raw_text can create
            # false lineage mismatches even when parsing succeeded.
            "content_hash": hashlib.sha256(out_file.read_bytes()).hexdigest(),
            "parsed_at": doc.parsed_at,
            **quality,
        }

    except Exception as e:
        return _failed_row(
            file_path,
            ticker,
            out_root,
            source_metadata,
            f"{type(e).__name__}: {e}",
            parse_source_pdf=parse_source_pdf,
            parse_source_kind=parse_source_kind,
            parse_source_metadata=parse_source_metadata,
            identity=identity,
        )


def read_existing_index(index_path: Path) -> list[dict]:
    if not index_path.exists():
        return []

    with index_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [_normalise_index_row(row) for row in reader]


def _add_missing_source_fingerprint(row: dict) -> dict:
    """Keep externally updated index rows compatible with raw-source checks.

    Do not infer the new ``parse_source_*`` fields here: an older OCR workflow
    may have written text using a different extractor. Leaving those fields
    empty intentionally makes the next parser run re-establish provenance.
    """
    row = _normalise_index_row(row)
    if all(row.get(field, "") not in ("", None) for field in SOURCE_FINGERPRINT_FIELDS):
        return row

    source_pdf = row.get("source_pdf", "")
    if not source_pdf:
        return row

    try:
        fingerprint = source_fingerprint(Path(source_pdf))
    except (OSError, RuntimeError):
        return row

    for field, value in fingerprint.items():
        if row.get(field, "") in ("", None):
            row[field] = value
    return row


def write_index(index_path: Path, rows: list[dict]) -> None:
    rows = sorted(
        _index_rows_by_key(rows).values(),
        key=lambda r: (r.get("ticker", ""), r.get("pdf_file", ""), r.get("source_pdf", "")),
    )

    def write_contents(f) -> None:
        writer = csv.DictWriter(f, fieldnames=PARSE_INDEX_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    _atomic_write(index_path, write_contents)


def upsert_index_rows(index_path: Path, new_rows: list[dict], replace_all: bool) -> None:
    rows_by_key = {} if replace_all else _index_rows_by_key(read_existing_index(index_path))
    for row in new_rows:
        row = _add_missing_source_fingerprint(row)
        rows_by_key[_index_row_key(row)] = row

    write_index(index_path, list(rows_by_key.values()))


def run(
    root: str | Path,
    out: str | Path,
    index: str | Path,
    ticker: str | None = None,
    workers: int = 1,
    num_companies: int | None = None,
    log_pages: bool = False,
    resume: bool = True,
    force: bool = False,
    checkpoint_every: int = 1,
    ocr_root: str | Path | None = DEFAULT_OCR_ROOT,
    pdf_file: str | None = None,
    prefer_pdfium: bool = False,
    prefer_pymupdf: bool = False,
    parser_overrides: str | Path | None = DEFAULT_PARSER_OVERRIDES,
    auto_layout_pdfium: bool = DEFAULT_AUTO_LAYOUT_PDFIUM,
    file_catalog: str | Path | None = DEFAULT_FILE_CATALOG,
    ocr_approval: str | Path | None = DEFAULT_OCR_APPROVAL,
) -> list[dict]:
    if checkpoint_every < 1:
        raise ValueError("checkpoint_every must be at least 1")

    ticker = ticker.upper() if ticker else None
    data = discover(root, ticker=ticker, pdf_file=pdf_file)
    if num_companies is not None and ticker is None:
        data = dict(list(sorted(data.items()))[:num_companies])

    discovered = [
        (pdf, ticker_name)
        for ticker_name, files in sorted(data.items())
        for pdf in files
    ]
    print(f"Found {len(discovered)} ESG PDF(s) under {root}")

    index_path = Path(index)
    rows_by_key = _index_rows_by_key(read_existing_index(index_path))
    parser_override_rows = load_parser_overrides(parser_overrides)
    catalog_rows = load_catalog(file_catalog)
    approval_rows = approved_ocr_rows(ocr_approval)
    results: list[dict] = []
    jobs: list[tuple] = []
    summary = {
        "found": len(discovered),
        "ocr_sources_selected": 0,
        "parser_overrides_selected": 0,
        "skipped_complete": 0,
        "processed": 0,
        "failed": 0,
        "ocr_required": 0,
        "reprocessed_stale": 0,
        "reprocessed_parser_policy": 0,
        "excluded_duplicate": 0,
    }
    completed_since_checkpoint = 0

    def checkpoint() -> None:
        write_index(index_path, list(rows_by_key.values()))

    def record_completed(row: dict) -> None:
        nonlocal completed_since_checkpoint
        row = _normalise_index_row(row)
        rows_by_key[_index_row_key(row)] = row
        results.append(row)
        completed_since_checkpoint += 1
        if completed_since_checkpoint >= checkpoint_every:
            checkpoint()
            completed_since_checkpoint = 0

    def report_completed(row: dict) -> None:
        print()
        print(f"==== {row['ticker']} {row['pdf_file']}")
        if row["status"] == "failed":
            summary["failed"] += 1
            print("FAILED:", row["error_message"])
            return

        summary["processed"] += 1
        if row["status"] == "ocr_required":
            summary["ocr_required"] += 1
        print(f"{row['status'].upper()} {row['char_count']} chars")

    for pdf, ticker_name in discovered:
        catalog_row = catalog_rows.get((ticker_name, pdf.name))
        if catalog_row and str(catalog_row.get("processing_state") or "") == "excluded_duplicate":
            summary["excluded_duplicate"] += 1
            print(f"SKIP exact duplicate alias {ticker_name} {pdf.name}")
            continue
        out_file, page_map_file = _output_paths(out, ticker_name, pdf)
        parser_request = parser_request_for_pdf(
            ticker=ticker_name,
            pdf=pdf,
            overrides=parser_override_rows,
            prefer_pdfium=prefer_pdfium,
            prefer_pymupdf=prefer_pymupdf,
        )
        job_prefer_pdfium = bool(parser_request["prefer_pdfium"])
        job_prefer_pymupdf = bool(parser_request["prefer_pymupdf"])
        job_parser_policy = str(parser_request["parser_policy"])
        job_parser_reason = str(parser_request["parser_reason"])
        job_auto_layout_pdfium = (
            auto_layout_pdfium
            and job_parser_policy != "override_pdfplumber"
            and not job_prefer_pymupdf
        )
        expected_parser_policy = job_parser_policy
        if job_parser_policy.startswith("override_"):
            summary["parser_overrides_selected"] += 1

        try:
            source_metadata = source_fingerprint(pdf)
        except Exception as error:
            row = _failed_row(
                pdf,
                ticker_name,
                out,
                None,
                f"{type(error).__name__}: {error}",
                parse_source_pdf=pdf,
                parse_source_kind="raw",
            )
            record_completed(row)
            report_completed(row)
            continue

        selection = select_parse_source(
            pdf,
            ticker_name,
            ocr_root,
            source_sha256=str(source_metadata["source_sha256"]),
            approvals=approval_rows,
        )
        parse_source_pdf, parse_source_kind = selection.path, selection.kind
        if parse_source_kind == "ocr":
            summary["ocr_sources_selected"] += 1
        elif selection.approval_status == "ignored":
            print(f"IGNORED OCR {ticker_name} {pdf.name}: {selection.reason}")

        try:
            parse_source_metadata = (
                source_metadata
                if parse_source_pdf == pdf
                else source_fingerprint(parse_source_pdf)
            )
        except Exception as error:
            row = _failed_row(
                pdf,
                ticker_name,
                out,
                source_metadata,
                f"{type(error).__name__}: {error}",
                parse_source_pdf=parse_source_pdf,
                parse_source_kind=parse_source_kind,
            )
            record_completed(row)
            report_completed(row)
            continue

        identity = identity_metadata(
            pdf,
            ticker_name,
            str(source_metadata["source_sha256"]),
            parse_source_kind,
            str(parse_source_metadata["source_sha256"]),
            catalog_row,
            selection,
        )

        existing_row = rows_by_key.get(
            _index_row_key({"ticker": ticker_name, "pdf_file": pdf.name})
        )
        if resume and not force and _is_complete_row(
            existing_row,
            pdf,
            source_metadata,
            parse_source_pdf,
            parse_source_kind,
            parse_source_metadata,
            out_file,
            page_map_file,
            prefer_pdfium=job_prefer_pdfium,
            prefer_pymupdf=job_prefer_pymupdf,
            expected_parser_policy=expected_parser_policy,
            auto_layout_pdfium=job_auto_layout_pdfium,
            identity=identity,
        ):
            summary["skipped_complete"] += 1
            continue

        if (
            resume
            and not force
            and existing_row is not None
            and existing_row.get("status", "").strip().lower()
            in {"parsed", "ocr_required"}
        ):
            if not _parser_policy_matches_request(
                existing_row,
                expected_parser_policy,
                job_auto_layout_pdfium,
            ):
                summary["reprocessed_parser_policy"] += 1
            else:
                summary["reprocessed_stale"] += 1

        jobs.append(
            (
                pdf,
                parse_source_pdf,
                parse_source_kind,
                ticker_name,
                Path(out),
                log_pages,
                source_metadata,
                parse_source_metadata,
                job_prefer_pdfium,
                job_prefer_pymupdf,
                job_parser_policy,
                job_parser_reason,
                job_auto_layout_pdfium,
                identity,
            )
        )

    if jobs:
        ctx = mp.get_context("spawn")
        with ProcessPoolExecutor(
            max_workers=max(1, workers),
            mp_context=ctx,
            max_tasks_per_child=1,
        ) as pool:
            futures = {pool.submit(_parse_one, job): job for job in jobs}
            for future in as_completed(futures):
                job = futures[future]
                try:
                    row = future.result()
                except Exception as error:
                    row = _failed_row(
                        Path(job[0]),
                        job[3],
                        job[4],
                        job[6],
                        f"{type(error).__name__}: {error}",
                        parse_source_pdf=Path(job[1]),
                        parse_source_kind=job[2],
                        parse_source_metadata=job[7],
                    )
                record_completed(row)
                report_completed(row)

    # Also commits deduplication when all discovered files were already valid.
    checkpoint()
    print(f"Index saved to: {index_path}")
    print("Summary:")
    for field in (
        "found",
        "ocr_sources_selected",
        "parser_overrides_selected",
        "skipped_complete",
        "processed",
        "failed",
        "ocr_required",
        "reprocessed_stale",
        "reprocessed_parser_policy",
        "excluded_duplicate",
    ):
        print(f"{field}: {summary[field]}")
    return results


def main():
    ap = argparse.ArgumentParser(description="Parse ESG/sustainability PDFs to text.")
    ap.add_argument("--root", default="data/01_raw/sustainability")
    ap.add_argument("--out", default="data/02_interim/esg_text")
    ap.add_argument("--index", default="data/00_reference/esg_parse_index.csv")
    ap.add_argument(
        "--ocr-root",
        default=DEFAULT_OCR_ROOT,
        help=(
            "Optional searchable-OCR derivative root. A matching filename is "
            "used only when --ocr-approval has an active reviewer approval "
            "bound to both original and OCR SHA-256 hashes."
        ),
    )
    ap.add_argument("--file-catalog", default=DEFAULT_FILE_CATALOG)
    ap.add_argument("--ocr-approval", default=DEFAULT_OCR_APPROVAL)
    ap.add_argument("--ticker", default=None, help="Process one ticker folder, e.g. GAP")
    ap.add_argument(
        "--pdf-file",
        default=None,
        help=(
            "Process one PDF filename or stem within the selected root/ticker, "
            "for example AMZN-Amazon-2022.pdf."
        ),
    )
    backend_mode = ap.add_mutually_exclusive_group()
    backend_mode.add_argument(
        "--prefer-pdfium",
        action="store_true",
        help=(
            "Force pypdfium text extraction for selected PDFs. This repairs "
            "damaged text layers but does not certify column or table order."
        ),
    )
    backend_mode.add_argument(
        "--prefer-pymupdf",
        action="store_true",
        help=(
            "Use the fast PyMuPDF coordinate backend with XY-cut reading order "
            "and strict table validation. Recommended for design-heavy reports "
            "that stall or over-detect tables under pdfplumber."
        ),
    )
    ap.add_argument(
        "--parser-overrides",
        default=DEFAULT_PARSER_OVERRIDES,
        help=(
            "CSV of per-PDF parser overrides with columns "
            "ticker,pdf_file,parser_mode,reason,active."
        ),
    )
    layout_mode = ap.add_mutually_exclusive_group()
    layout_mode.add_argument(
        "--auto-layout-pdfium",
        dest="auto_layout_pdfium",
        action="store_true",
        default=DEFAULT_AUTO_LAYOUT_PDFIUM,
        help=(
            "Opt into automatic pypdfium replacement for detected grid/tile "
            "layouts. Use only for a scoped, reviewed parser calibration."
        ),
    )
    layout_mode.add_argument(
        "--no-auto-layout-pdfium",
        dest="auto_layout_pdfium",
        action="store_false",
        help="Keep detected grid/tile pages as report-only layout risks (default).",
    )
    ap.add_argument(
        "--num-companies",
        type=int,
        default=None,
        help="Optional limit for compatibility with earlier parser smoke tests.",
    )
    ap.add_argument(
        "--workers",
        type=int,
        default=1,
        help=(
            "Parallel worker processes. Each PDF still gets its own process "
            "lifetime to release pdfplumber/pdfminer memory."
        ),
    )
    ap.add_argument("--log-pages", action="store_true", help="Print per-page parser progress and RAM usage.")
    resume_group = ap.add_mutually_exclusive_group()
    resume_group.add_argument(
        "--resume",
        dest="resume",
        action="store_true",
        default=True,
        help="Skip complete current outputs and continue missing, failed, or stale PDFs (default).",
    )
    resume_group.add_argument(
        "--no-resume",
        dest="resume",
        action="store_false",
        help="Reparse selected PDFs even when their current outputs are complete.",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Ignore resume state and rebuild every PDF selected by --ticker/root.",
    )
    ap.add_argument(
        "--checkpoint-every",
        type=int,
        default=1,
        metavar="N",
        help="Atomically checkpoint the parse index after every N completed PDFs (default: 1).",
    )
    args = ap.parse_args()

    if args.checkpoint_every < 1:
        ap.error("--checkpoint-every must be at least 1")

    run(
        root=args.root,
        out=args.out,
        index=args.index,
        ticker=args.ticker.upper() if args.ticker else None,
        workers=args.workers,
        num_companies=args.num_companies,
        log_pages=args.log_pages,
        resume=args.resume,
        force=args.force,
        checkpoint_every=args.checkpoint_every,
        ocr_root=args.ocr_root,
        pdf_file=args.pdf_file,
        prefer_pdfium=args.prefer_pdfium,
        prefer_pymupdf=args.prefer_pymupdf,
        parser_overrides=args.parser_overrides,
        auto_layout_pdfium=args.auto_layout_pdfium,
        file_catalog=args.file_catalog,
        ocr_approval=args.ocr_approval,
    )


if __name__ == "__main__":
    main()
