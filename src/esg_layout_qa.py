"""Deterministic page-level layout QA for the ESG corpus.

The parser can recover text from visually complex PDFs, but citation validity
alone cannot prove that a multi-column page retained a safe reading order.
This stage audits every physical page without changing parsed text or chunks.
Pages with unresolved multi-column structure are automatically held from
retrieval by the vector-manifest gate while their source evidence remains
preserved in the corpus.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import multiprocessing as mp
import os
import re
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import pdfplumber

try:
    import pypdfium2 as pdfium
except ImportError:  # pragma: no cover - exercised through a fail-closed row.
    pdfium = None

from pdf_parser import (
    MIN_PAGE_CHARS,
    count_cid_artifacts,
    count_garbled_chars,
    layout_grid_risk_from_metrics,
    normalize_extracted_page_text,
    page_layout_grid_metrics,
    reconstruct_region_order,
)
from esg_reading_order import canonical_order_text, reconstruct_column_order


# v7 auto-passes ambiguous navigation/contents-layout pages instead of
# holding them, since they have no defensible prose order and no retrieval
# value worth VLM spend. This changes page decisions, so v6 rows must be
# rebuilt.
AUDIT_VERSION = "layout_v7"
AUTO_PASS = "auto_pass"
AUTO_PASS_PDFIUM_COVERAGE = "auto_pass_pdfium_coverage"
AUTO_PASS_COLUMN_ORDER = "auto_pass_column_order_reconstructed"
AUTO_PASS_REGION_ORDER = "auto_pass_region_order_reconstructed"
AUTO_PASS_VERIFIED_TABLE = "auto_pass_verified_table_extraction"
AUTO_PASS_NAVIGATION = "auto_pass_navigation_contents"
AUTO_HOLD = "auto_hold"
AUDIT_ERROR = "audit_error"

LAYOUT_AUDIT_FIELDS = [
    "ticker",
    "pdf_stem",
    "pdf_file",
    "source_pdf",
    "source_sha256",
    "parsed_text_sha256",
    "current_parser_used",
    "current_parser_policy",
    "page",
    "page_text_chars",
    "native_text_chars",
    "native_word_count",
    "current_reliable_token_count",
    "pdfplumber_reliable_token_count",
    "pdfium_reliable_token_count",
    "left_word_share",
    "right_word_share",
    "center_word_share",
    "column_y_overlap",
    "two_column_candidate",
    "mixed_column_lines",
    "visual_object_count",
    "page_map_parse_status",
    "page_map_repair_method",
    "page_map_table_candidate_count",
    "table_row_count",
    "table_column_count",
    "table_source_token_count",
    "table_output_token_count",
    "table_token_recall",
    "table_extra_token_ratio",
    "reading_order_status",
    "reading_order_columns",
    "reading_order_preservation_ratio",
    "reading_order_current_match",
    "reading_order_reason",
    "candidate_preference",
    "decision",
    "decision_reason",
    "audit_version",
]

WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'/-]*")
Y_TOLERANCE = 3.0
MIN_COLUMN_WORDS = 60
MIN_COLUMN_SHARE = 0.25
MIN_COLUMN_VERTICAL_OVERLAP = 0.35
MIN_MIXED_COLUMN_LINES = 2
LOW_TEXT_MIN_NATIVE_WORDS = 40
TABLE_REPAIR_METHODS = frozenset(
    {"table_aware_xy_cut_order", "pymupdf_table_aware_xy_cut_order"}
)
REGION_REPAIR_METHODS = frozenset(
    {"region_xy_cut_order", "pymupdf_region_xy_cut_order"}
)
TABLE_MIN_TOKEN_RECALL = 0.995
TABLE_MAX_EXTRA_TOKEN_RATIO = 0.005


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv_atomic(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=LAYOUT_AUDIT_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def parse_int(value: str | int | None) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def resolve_path(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else Path.cwd() / path


def page_records_from_map(text_path: Path, page_map_path: Path) -> dict[int, dict]:
    if not text_path.exists() or not page_map_path.exists():
        return {}
    with text_path.open("r", encoding="utf-8", newline="") as handle:
        source_text = handle.read()
    rows = read_csv(page_map_path)
    pages: dict[int, dict] = {}
    for row in rows:
        page = parse_int(row.get("page"))
        start = parse_int(row.get("char_start"))
        end = parse_int(row.get("char_end"))
        if page is None or start is None or end is None or start < 0 or end < start:
            continue
        pages[page] = {**row, "text": source_text[start:end]}
    return pages


def page_texts_from_map(text_path: Path, page_map_path: Path) -> dict[int, str]:
    return {
        page: str(record.get("text") or "")
        for page, record in page_records_from_map(text_path, page_map_path).items()
    }


def reliable_token_count(text: str) -> int:
    return sum(1 for token in WORD_RE.findall(text) if re.search(r"[A-Za-z]", token))


def _semantic_token_counter(text: str) -> Counter[str]:
    return Counter(token.casefold() for token in WORD_RE.findall(text) if token)


def _upright_source_tokens(words: list[dict]) -> Counter[str]:
    source_text = " ".join(
        str(word.get("text") or "")
        for word in words
        if word.get("upright", True) is not False
    )
    return _semantic_token_counter(source_text)


def _markdown_table_shape(text: str) -> tuple[int, int]:
    table_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("|") and line.strip().endswith("|")
    ]
    if len(table_lines) < 3:
        return 0, 0

    def cells(line: str) -> list[str]:
        return [
            value.strip().replace(r"\|", "|")
            for value in re.split(r"(?<!\\)\|", line.strip()[1:-1])
        ]

    rows = [cells(line) for line in table_lines]
    column_count = len(rows[0])
    if column_count < 2 or any(len(row) != column_count for row in rows):
        return 0, 0
    if not all(re.fullmatch(r":?-{3,}:?", value) for value in rows[1]):
        return 0, 0
    body = rows[2:]
    if len(body) < 2 or any(not any(cell for cell in row) for row in body):
        return 0, 0
    return len(body), column_count


def table_extraction_decision(
    page_record: dict,
    current_text: str,
    words: list[dict],
) -> tuple[str | None, str, dict[str, str | int]]:
    """Validate parser-produced tables without counting rotated decoration."""

    method = str(page_record.get("repair_method") or "").strip()
    metrics: dict[str, str | int] = {
        "table_row_count": "",
        "table_column_count": "",
        "table_source_token_count": "",
        "table_output_token_count": "",
        "table_token_recall": "",
        "table_extra_token_ratio": "",
    }
    candidate_count = parse_int(page_record.get("table_candidate_count")) or 0
    if method not in TABLE_REPAIR_METHODS:
        if candidate_count > 0:
            return AUTO_HOLD, "auto_hold_table_candidate_not_extracted", metrics
        return None, "", metrics

    if candidate_count < 1:
        return AUTO_HOLD, "auto_hold_table_extraction_missing_candidate", metrics

    row_count, column_count = _markdown_table_shape(current_text)
    metrics["table_row_count"] = row_count
    metrics["table_column_count"] = column_count
    if row_count < 2 or column_count < 2:
        return AUTO_HOLD, "auto_hold_table_extraction_invalid_markdown_shape", metrics

    source_tokens = _upright_source_tokens(words)
    output_tokens = _semantic_token_counter(current_text)
    source_count = sum(source_tokens.values())
    output_count = sum(output_tokens.values())
    matched_count = sum((source_tokens & output_tokens).values())
    recall = matched_count / max(source_count, 1)
    extra_ratio = sum((output_tokens - source_tokens).values()) / max(source_count, 1)
    metrics.update(
        {
            "table_source_token_count": source_count,
            "table_output_token_count": output_count,
            "table_token_recall": f"{recall:.6f}",
            "table_extra_token_ratio": f"{extra_ratio:.6f}",
        }
    )
    if source_count == 0:
        return AUTO_HOLD, "auto_hold_table_extraction_missing_source_tokens", metrics
    if recall < TABLE_MIN_TOKEN_RECALL:
        return (
            AUTO_HOLD,
            f"auto_hold_table_extraction_token_recall={recall:.4f}",
            metrics,
        )
    if extra_ratio > TABLE_MAX_EXTRA_TOKEN_RATIO:
        return (
            AUTO_HOLD,
            f"auto_hold_table_extraction_extra_token_ratio={extra_ratio:.4f}",
            metrics,
        )
    return (
        AUTO_PASS_VERIFIED_TABLE,
        "auto_pass_verified_table_extraction: "
        f"rows={row_count}; columns={column_count}; "
        f"token_recall={recall:.4f}; extra_token_ratio={extra_ratio:.4f}",
        metrics,
    )


def text_quality_score(text: str) -> int:
    # A simple, deterministic ranking for choosing an extractor candidate.
    # It rewards readable terms and heavily penalizes known parser artifacts.
    return max(
        0,
        reliable_token_count(text)
        - 8 * count_cid_artifacts(text)
        - 8 * count_garbled_chars(text),
    )


def _line_groups(words: list[dict]) -> list[list[dict]]:
    groups: list[list[dict]] = []
    tops: list[float] = []
    for word in sorted(words, key=lambda item: (float(item.get("top", 0.0)), float(item.get("x0", 0.0)))):
        top = float(word.get("top", 0.0))
        if not groups or abs(top - tops[-1]) > Y_TOLERANCE:
            groups.append([word])
            tops.append(top)
        else:
            groups[-1].append(word)
    return groups


def _column_metrics(words: list[dict], page_width: float, visual_object_count: int) -> dict[str, float | int | bool]:
    usable = [word for word in words if str(word.get("text", "")).strip()]
    if not usable or page_width <= 0:
        return {
            "native_word_count": len(usable),
            "left_word_share": 0.0,
            "right_word_share": 0.0,
            "center_word_share": 0.0,
            "column_y_overlap": 0.0,
            "two_column_candidate": False,
            "mixed_column_lines": 0,
            "visual_object_count": visual_object_count,
        }

    left: list[dict] = []
    right: list[dict] = []
    center: list[dict] = []
    for word in usable:
        midpoint = (float(word.get("x0", 0.0)) + float(word.get("x1", 0.0))) / 2
        if midpoint < page_width * 0.45:
            left.append(word)
        elif midpoint > page_width * 0.55:
            right.append(word)
        else:
            center.append(word)

    def vertical_overlap(first: list[dict], second: list[dict]) -> float:
        if not first or not second:
            return 0.0
        first_top = min(float(word.get("top", 0.0)) for word in first)
        first_bottom = max(float(word.get("bottom", word.get("top", 0.0))) for word in first)
        second_top = min(float(word.get("top", 0.0)) for word in second)
        second_bottom = max(float(word.get("bottom", word.get("top", 0.0))) for word in second)
        shared = max(0.0, min(first_bottom, second_bottom) - max(first_top, second_top))
        shorter = min(first_bottom - first_top, second_bottom - second_top)
        return shared / shorter if shorter > 0 else 0.0

    overlap = vertical_overlap(left, right)
    total = len(usable)
    left_share = len(left) / total
    right_share = len(right) / total
    center_share = len(center) / total
    two_column_candidate = (
        total >= MIN_COLUMN_WORDS
        and left_share >= MIN_COLUMN_SHARE
        and right_share >= MIN_COLUMN_SHARE
        and overlap >= MIN_COLUMN_VERTICAL_OVERLAP
    )

    mixed_lines = 0
    for line in _line_groups(usable):
        has_left = any((float(word.get("x0", 0.0)) + float(word.get("x1", 0.0))) / 2 < page_width * 0.45 for word in line)
        has_right = any((float(word.get("x0", 0.0)) + float(word.get("x1", 0.0))) / 2 > page_width * 0.55 for word in line)
        if has_left and has_right:
            mixed_lines += 1

    return {
        "native_word_count": total,
        "left_word_share": left_share,
        "right_word_share": right_share,
        "center_word_share": center_share,
        "column_y_overlap": overlap,
        "two_column_candidate": two_column_candidate,
        "mixed_column_lines": mixed_lines,
        "visual_object_count": visual_object_count,
    }


def _pdfium_page_text(pdf_document, page_number: int) -> str:
    page = None
    text_page = None
    try:
        page = pdf_document[page_number - 1]
        text_page = page.get_textpage()
        return normalize_extracted_page_text(text_page.get_text_range() or "")
    finally:
        if text_page is not None and hasattr(text_page, "close"):
            text_page.close()
        if page is not None and hasattr(page, "close"):
            page.close()


def _candidate_preference(current: str, pdfplumber_text: str, pdfium_text: str) -> tuple[str, dict[str, int]]:
    candidates = {
        "current": text_quality_score(current),
        "pdfplumber": text_quality_score(pdfplumber_text),
    }
    if pdfium_text:
        candidates["pdfium"] = text_quality_score(pdfium_text)
    preference = max(candidates, key=lambda name: (candidates[name], name == "current"))
    return preference, candidates


def automatic_decision(
    metrics: dict[str, float | int | bool],
    current_text: str,
    pdfplumber_text: str,
    pdfium_text: str,
    current_is_pdfium: bool = False,
) -> tuple[str, str, str, dict[str, int]]:
    """Return a conservative automatic decision without rewriting page text.

    A visual multi-column page with mixed coordinate lines is not safe to
    certify from text metrics alone. It is held automatically. Non-structural
    low-text pages can pass only when the selected PDFium text demonstrably
    improves readable coverage over the native text layer.
    """

    preference, scores = _candidate_preference(current_text, pdfplumber_text, pdfium_text)
    current_score = scores["current"]
    native_score = scores["pdfplumber"]
    best_score = max(scores.values(), default=0)
    coverage_disagreement = best_score >= max(native_score * 3, native_score + 80)
    # Occupancy in both page halves and same-y words also occurs in ordinary
    # full-width prose. Hold this coarse signal only when extractors materially
    # disagree; coordinate reconstruction handles genuine stable columns.
    structural_risk = bool(metrics["two_column_candidate"]) and coverage_disagreement
    missing_text = (
        int(metrics["native_word_count"]) >= LOW_TEXT_MIN_NATIVE_WORDS
        and reliable_token_count(current_text) < MIN_PAGE_CHARS
    )

    if structural_risk:
        reason = (
            "auto_hold_structural_multi_column: "
            f"mixed_lines={metrics['mixed_column_lines']}; "
            f"coverage_disagreement={str(coverage_disagreement).lower()}; "
            f"preferred={preference}; current_score={current_score}; native_score={native_score}"
        )
        return AUTO_HOLD, reason, preference, scores

    if missing_text:
        recovered_score = scores.get("pdfium", 0)
        if current_is_pdfium:
            recovered_score = current_score
        if (
            (pdfium_text or current_is_pdfium)
            and recovered_score >= max(native_score * 2, native_score + 30)
        ):
            return (
                AUTO_PASS_PDFIUM_COVERAGE,
                "auto_pass_pdfium_coverage: "
                f"recovered_score={recovered_score}; native_score={native_score}",
                preference,
                scores,
            )
        return (
            AUTO_HOLD,
            "auto_hold_missing_current_text: "
            f"preferred={preference}; current_score={current_score}; native_score={native_score}",
            preference,
            scores,
        )

    return AUTO_PASS, "auto_pass_no_unresolved_layout_signal", preference, scores


def region_order_decision(
    page_record: dict,
    current_text: str,
    words: list[dict],
    page_width: float,
    page_height: float,
) -> tuple[str, str] | None:
    """Verify a parser region-order repair the column reconstructor cannot model.

    The parser's xy-cut region pass is a legitimate reading-order repair, but
    ``reading_order_decision`` only compares against pure column
    reconstruction, so a region-repaired page would always look like a
    mismatch and be held. When the page map says a region repair was applied,
    recompute it here and pass the page only if the stored text matches the
    fresh reconstruction exactly.
    """

    method = str(page_record.get("repair_method") or "").strip()
    if method not in REGION_REPAIR_METHODS:
        return None
    region_order = reconstruct_region_order(words, page_width, page_height)
    if region_order.status != "reconstructed":
        return None
    if canonical_order_text(current_text) != canonical_order_text(region_order.text):
        return None
    return (
        AUTO_PASS_REGION_ORDER,
        "auto_pass_region_order_reconstructed: "
        f"blocks={region_order.block_count}; "
        f"preservation_ratio={region_order.preservation_ratio:.4f}; "
        f"extra_token_ratio={region_order.extra_token_ratio:.4f}",
    )


def reading_order_decision(reading_order, current_text: str) -> tuple[str | None, str, bool]:
    """Return a decisive coordinate-order gate when a page has a layout signal."""

    if reading_order.status == "reconstructed":
        current_matches = (
            canonical_order_text(current_text) == canonical_order_text(reading_order.text)
        )
        if current_matches:
            return (
                AUTO_PASS_COLUMN_ORDER,
                "auto_pass_coordinate_column_order: "
                f"columns={reading_order.column_count}; "
                f"preservation_ratio={reading_order.preservation_ratio:.4f}",
                True,
            )
        return (
            AUTO_HOLD,
            "auto_hold_coordinate_column_order_not_applied: "
            f"columns={reading_order.column_count}; "
            f"preservation_ratio={reading_order.preservation_ratio:.4f}",
            False,
        )
    if reading_order.status == "ambiguous":
        if reading_order.reason == "navigation_contents_layout":
            # Contents/nav pages have no defensible prose order and no
            # retrieval value worth VLM spend; index them as-is.
            return (
                AUTO_PASS_NAVIGATION,
                "auto_pass_navigation_contents_layout: no_prose_order_required",
                False,
            )
        return (
            AUTO_HOLD,
            "auto_hold_ambiguous_coordinate_layout: " + reading_order.reason,
            False,
        )
    return None, "", False


def _error_row(parse_row: dict, message: str) -> dict:
    return {
        "ticker": (parse_row.get("ticker") or "").upper(),
        "pdf_stem": Path(parse_row.get("pdf_file") or "").stem,
        "pdf_file": parse_row.get("pdf_file", ""),
        "source_pdf": parse_row.get("source_pdf", ""),
        "source_sha256": parse_row.get("source_sha256", ""),
        "parsed_text_sha256": parse_row.get("content_hash", ""),
        "current_parser_used": parse_row.get("parser_used", ""),
        "current_parser_policy": parse_row.get("parser_policy", ""),
        "page": "",
        "page_text_chars": "",
        "native_text_chars": "",
        "native_word_count": "",
        "current_reliable_token_count": "",
        "pdfplumber_reliable_token_count": "",
        "pdfium_reliable_token_count": "",
        "left_word_share": "",
        "right_word_share": "",
        "center_word_share": "",
        "column_y_overlap": "",
        "two_column_candidate": "",
        "mixed_column_lines": "",
        "visual_object_count": "",
        "page_map_parse_status": "",
        "page_map_repair_method": "",
        "page_map_table_candidate_count": "",
        "table_row_count": "",
        "table_column_count": "",
        "table_source_token_count": "",
        "table_output_token_count": "",
        "table_token_recall": "",
        "table_extra_token_ratio": "",
        "reading_order_status": "",
        "reading_order_columns": "",
        "reading_order_preservation_ratio": "",
        "reading_order_current_match": "",
        "reading_order_reason": "",
        "candidate_preference": "",
        "decision": AUDIT_ERROR,
        "decision_reason": message,
        "audit_version": AUDIT_VERSION,
    }


def audit_document(parse_row: dict) -> list[dict]:
    source_pdf = resolve_path(parse_row.get("source_pdf"))
    text_path = resolve_path(parse_row.get("parsed_text_file"))
    page_map_path = resolve_path(parse_row.get("page_map_file"))
    if source_pdf is None or not source_pdf.exists():
        return [_error_row(parse_row, "audit_error_source_pdf_missing")]
    if text_path is None or page_map_path is None:
        return [_error_row(parse_row, "audit_error_parse_outputs_missing")]

    try:
        current_page_records = page_records_from_map(text_path, page_map_path)
        current_page_texts = {
            page: str(record.get("text") or "")
            for page, record in current_page_records.items()
        }
        # Use the saved artifact as the source of truth. Older parse indexes may
        # contain a hash of the pre-serialization string (not the exact bytes).
        parsed_text_sha256 = hashlib.sha256(text_path.read_bytes()).hexdigest()
        rows: list[dict] = []
        with pdfplumber.open(source_pdf) as pdf:
            pdfium_document = None
            try:
                if pdfium is not None:
                    pdfium_document = pdfium.PdfDocument(str(source_pdf))
                for page_number, page in enumerate(pdf.pages, start=1):
                    current_text = current_page_texts.get(page_number, "")
                    page_record = current_page_records.get(page_number, {})
                    native_text = normalize_extracted_page_text(page.extract_text_simple() or "")
                    try:
                        # extra_attrs must match the parser's extraction
                        # exactly: pdfplumber only merges adjacent characters
                        # into one word when every requested attr agrees, so a
                        # different attr set here yields a different word
                        # segmentation and a false reading-order mismatch.
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
                    rect_count = len(getattr(page, "rects", []) or [])
                    image_count = len(getattr(page, "images", []) or [])
                    curve_count = len(getattr(page, "curves", []) or [])
                    visual_object_count = rect_count + image_count + min(curve_count, 25)
                    native_layout_metrics = page_layout_grid_metrics(
                        page,
                        native_text,
                        words,
                    )
                    metrics = _column_metrics(words, float(page.width), visual_object_count)
                    current_is_pdfium = "pypdfium" in (parse_row.get("parser_used") or "").lower()
                    needs_pdfium_comparison = (
                        not current_is_pdfium
                        and (
                            bool(metrics["two_column_candidate"])
                            or (
                                int(metrics["native_word_count"]) >= LOW_TEXT_MIN_NATIVE_WORDS
                                and reliable_token_count(current_text) < MIN_PAGE_CHARS
                            )
                        )
                    )
                    pdfium_text = ""
                    if needs_pdfium_comparison and pdfium_document is not None:
                        pdfium_text = _pdfium_page_text(pdfium_document, page_number)

                    reading_order = reconstruct_column_order(
                        words,
                        float(page.width),
                        float(page.height),
                        # Raw vector-object counts are common in decorative ESG
                        # designs. Require the complete table/grid signature.
                        structural_grid_risk=layout_grid_risk_from_metrics(native_layout_metrics),
                    )
                    table_decision, table_reason, table_metrics = table_extraction_decision(
                        page_record,
                        current_text,
                        words,
                    )
                    current_order_matches = False
                    if table_decision is not None:
                        decision, reason = table_decision, table_reason
                    else:
                        decision, reason, current_order_matches = reading_order_decision(
                            reading_order,
                            current_text,
                        )
                        if decision == AUTO_HOLD:
                            region_verdict = region_order_decision(
                                page_record,
                                current_text,
                                words,
                                float(page.width),
                                float(page.height),
                            )
                            if region_verdict is not None:
                                decision, reason = region_verdict
                                current_order_matches = True
                    preference, scores = _candidate_preference(
                        current_text,
                        native_text,
                        pdfium_text,
                    )
                    if decision is None:
                        decision, reason, preference, scores = automatic_decision(
                            metrics,
                            current_text,
                            native_text,
                            pdfium_text,
                            current_is_pdfium=current_is_pdfium,
                        )
                    rows.append(
                        {
                            "ticker": (parse_row.get("ticker") or "").upper(),
                            "pdf_stem": Path(parse_row.get("pdf_file") or "").stem,
                            "pdf_file": parse_row.get("pdf_file", ""),
                            "source_pdf": parse_row.get("source_pdf", ""),
                            "source_sha256": parse_row.get("source_sha256", ""),
                            "parsed_text_sha256": parsed_text_sha256,
                            "current_parser_used": parse_row.get("parser_used", ""),
                            "current_parser_policy": parse_row.get("parser_policy", ""),
                            "page": page_number,
                            "page_text_chars": len(current_text),
                            "native_text_chars": len(native_text),
                            "native_word_count": metrics["native_word_count"],
                            "current_reliable_token_count": scores["current"],
                            "pdfplumber_reliable_token_count": scores["pdfplumber"],
                            "pdfium_reliable_token_count": scores.get("pdfium", ""),
                            "left_word_share": f"{float(metrics['left_word_share']):.6f}",
                            "right_word_share": f"{float(metrics['right_word_share']):.6f}",
                            "center_word_share": f"{float(metrics['center_word_share']):.6f}",
                            "column_y_overlap": f"{float(metrics['column_y_overlap']):.6f}",
                            "two_column_candidate": str(bool(metrics["two_column_candidate"])).lower(),
                            "mixed_column_lines": metrics["mixed_column_lines"],
                            "visual_object_count": metrics["visual_object_count"],
                            "page_map_parse_status": page_record.get("parse_status", ""),
                            "page_map_repair_method": page_record.get("repair_method", ""),
                            "page_map_table_candidate_count": page_record.get(
                                "table_candidate_count", ""
                            ),
                            **table_metrics,
                            "reading_order_status": reading_order.status,
                            "reading_order_columns": reading_order.column_count,
                            "reading_order_preservation_ratio": (
                                f"{reading_order.preservation_ratio:.6f}"
                            ),
                            "reading_order_current_match": str(current_order_matches).lower(),
                            "reading_order_reason": reading_order.reason,
                            "candidate_preference": preference,
                            "decision": decision,
                            "decision_reason": reason,
                            "audit_version": AUDIT_VERSION,
                        }
                    )
            finally:
                if pdfium_document is not None:
                    pdfium_document.close()
        return rows
    except Exception as error:  # A failed audit must become an explicit hold.
        return [_error_row(parse_row, f"audit_error:{type(error).__name__}:{error}")]


def _doc_key(row: dict) -> tuple[str, str]:
    return ((row.get("ticker") or "").upper(), Path(row.get("pdf_file") or "").stem)


def _audit_complete(existing_rows: list[dict], parse_row: dict) -> bool:
    page_count = parse_int(parse_row.get("page_count")) or 0
    if not page_count or len(existing_rows) != page_count:
        return False
    text_path = resolve_path(parse_row.get("parsed_text_file"))
    if text_path is None or not text_path.exists():
        return False
    actual_parsed_text_sha256 = hashlib.sha256(text_path.read_bytes()).hexdigest()
    return all(
        row.get("audit_version") == AUDIT_VERSION
        and row.get("source_sha256") == (parse_row.get("source_sha256") or "")
        and row.get("parsed_text_sha256") == actual_parsed_text_sha256
        and row.get("decision") not in {"", AUDIT_ERROR}
        for row in existing_rows
    )


def run(
    *,
    parse_index: str | Path = "data/00_reference/esg_parse_index.csv",
    out: str | Path = "data/00_reference/esg_page_layout_qa.csv",
    ticker: str | None = None,
    pdf_file: str | None = None,
    workers: int = 4,
    resume: bool = False,
    force: bool = False,
) -> list[dict]:
    if workers < 1:
        raise ValueError("workers must be at least 1")
    if force and not ticker:
        raise ValueError("full-corpus forced layout audit is blocked; supply --ticker")

    selected_ticker = ticker.upper() if ticker else None
    selected_pdf_stem = Path(pdf_file).stem if pdf_file else None
    parse_rows = [
        row
        for row in read_csv(Path(parse_index))
        if row.get("status") == "parsed"
        and (selected_ticker is None or (row.get("ticker") or "").upper() == selected_ticker)
        and (
            selected_pdf_stem is None
            or Path(row.get("pdf_file") or "").stem == selected_pdf_stem
        )
    ]
    parse_rows.sort(key=lambda row: _doc_key(row))

    out_path = Path(out)
    existing_rows = read_csv(out_path)
    existing_by_doc: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in existing_rows:
        existing_by_doc[((row.get("ticker") or "").upper(), row.get("pdf_stem") or "")].append(row)

    to_process: list[dict] = []
    skipped: set[tuple[str, str]] = set()
    for row in parse_rows:
        key = _doc_key(row)
        if resume and not force and _audit_complete(existing_by_doc.get(key, []), row):
            skipped.add(key)
        else:
            to_process.append(row)

    results: list[dict] = []
    if workers == 1:
        for row in to_process:
            results.extend(audit_document(row))
    elif to_process:
        mp_context = mp.get_context("spawn")
        with ProcessPoolExecutor(max_workers=workers, mp_context=mp_context) as executor:
            futures = {executor.submit(audit_document, row): row for row in to_process}
            for future in as_completed(futures):
                results.extend(future.result())

    processed_keys = {_doc_key(row) for row in to_process}
    retained = [
        row
        for row in existing_rows
        if ((row.get("ticker") or "").upper(), row.get("pdf_stem") or "") not in processed_keys
    ]
    combined = retained + results
    combined.sort(
        key=lambda row: (
            (row.get("ticker") or "").upper(),
            row.get("pdf_stem") or "",
            parse_int(row.get("page")) if parse_int(row.get("page")) is not None else -1,
        )
    )
    write_csv_atomic(out_path, combined)

    decisions = Counter(row.get("decision", "") for row in results)
    print(f"Layout QA written: {out_path}")
    print(f"documents_found: {len(parse_rows)}")
    print(f"documents_processed: {len(to_process)}")
    print(f"documents_skipped_complete: {len(skipped)}")
    print(f"pages_written: {len(results)}")
    for decision, count in sorted(decisions.items()):
        print(f"{decision}: {count}")
    return combined


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit ESG PDF page layouts and fail closed on ambiguous reading order.")
    parser.add_argument("--parse-index", default="data/00_reference/esg_parse_index.csv")
    parser.add_argument("--out", default="data/00_reference/esg_page_layout_qa.csv")
    parser.add_argument("--ticker", default=None)
    parser.add_argument("--pdf-file", default=None)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    run(
        parse_index=args.parse_index,
        out=args.out,
        ticker=args.ticker,
        pdf_file=args.pdf_file,
        workers=args.workers,
        resume=args.resume,
        force=args.force,
    )


if __name__ == "__main__":
    main()
