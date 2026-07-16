from __future__ import annotations

import argparse
import csv
import gc
import hashlib
import os
import multiprocessing as mp
import re
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

import pdfplumber

try:
    import pypdfium2 as pdfium
except ImportError:
    pdfium = None

try:
    import psutil
except ImportError:
    psutil = None

from base_parser import ParsedDocument
from esg_reading_order import reconstruct_column_order


MIN_PAGE_CHARS = 20
OCR_MIN_NONSPACE_CHARS = 500
TEXT_FALLBACK_MIN_PAGES = 5
TEXT_FALLBACK_MAX_CHARS_PER_PAGE = 250
TEXT_FALLBACK_MIN_PAGE_COVERAGE = 0.50
TEXT_FALLBACK_MIN_GAIN_RATIO = 1.50
TEXT_FALLBACK_MIN_CHAR_GAIN = 2000
DEFAULT_OCR_ROOT = None
DEFAULT_PARSER_OVERRIDES = "data/00_reference/esg_parser_overrides.csv"
DEFAULT_AUTO_LAYOUT_PDFIUM = False
AUTO_PDFPLUMBER_COLUMN_POLICY = "auto_pdfplumber_column_order_v1"
AUTO_PDFPLUMBER_COLUMN_REASON = "deterministic_coordinate_reading_order_v1"
LAYOUT_GRID_MIN_WORDS = 80
LAYOUT_GRID_MIN_SHORT_LINES = 12
LAYOUT_GRID_MIN_COMMON_STARTS = 3
LAYOUT_GRID_MIN_HUGE_GAP_LINES = 3
LAYOUT_GRID_MIN_VISUAL_OBJECTS = 8
LAYOUT_GRID_MIN_METRIC_LINES = 2
STRUCTURAL_LAYOUT_MIN_VISUAL_OBJECTS = 60
PARSE_INDEX_FIELDS = [
    "ticker",
    "pdf_file",
    "source_pdf",
    "source_size_bytes",
    "source_mtime_utc",
    "source_sha256",
    "parse_source_kind",
    "parse_source_pdf",
    "parse_source_size_bytes",
    "parse_source_mtime_utc",
    "parse_source_sha256",
    "parser_used",
    "parser_policy",
    "parser_reason",
    "layout_risk_pages",
    "reading_order_repaired_pages",
    "reading_order_unresolved_pages",
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
PAGE_MAP_FIELDS = ["page", "char_start", "char_end", "char_count"]
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
GARBLED_SEQUENCES = ["ï¿½", "Ã¢â‚¬", "Ã‚", "ï¿½?", "�"]
CID_ARTIFACT_RE = re.compile(r"\(cid:\d+\)")


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

    return has_form_10k and (has_sec_header or has_annual_report_pursuant or sec_item_count >= 2)


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


def build_raw_text_and_page_spans(pages: list[tuple[int, str]]) -> tuple[str, list[dict]]:
    parts: list[str] = []
    spans: list[dict] = []
    cursor = 0

    for page_number, text in pages:
        if parts:
            parts.append("\n")
            cursor += 1

        start = cursor
        parts.append(text)
        cursor += len(text)
        spans.append(
            {
                "page": page_number,
                "char_start": start,
                "char_end": cursor,
                "char_count": len(text),
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


def structural_layout_risk_from_metrics(metrics: dict[str, int]) -> bool:
    """Identify grid/table pages without mistaking vector-font curves for risk."""

    return (
        layout_grid_risk_from_metrics(metrics)
        or metrics.get("visual_objects", 0) >= STRUCTURAL_LAYOUT_MIN_VISUAL_OBJECTS
    )


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

    metric_pattern = re.compile(
        r"\b\d+(?:\.\d+)?\s*(?:%|M\+?|K\+?|million|billion|tons?|metric tons|CO2e|gCO2e)\b",
        flags=re.IGNORECASE,
    )
    return {
        "word_count": len(words),
        "line_count": len(lines),
        "short_lines": sum(1 for line in lines if 3 <= len(line.strip()) <= 45),
        "metric_lines": sum(1 for line in lines if metric_pattern.search(line)),
        "huge_gap_lines": huge_gap_lines,
        "common_start_count": common_start_count,
        "visual_objects": len(getattr(page, "rects", []) or [])
        + len(getattr(page, "images", []) or [])
        + min(len(getattr(page, "curves", []) or []), 25),
    }


def layout_risk_page_numbers(page_metrics: list[tuple[int, dict[str, int]]]) -> list[int]:
    return [
        page_number
        for page_number, metrics in page_metrics
        if structural_layout_risk_from_metrics(metrics)
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

    def parse(self, file_path, company=None, **kwargs):
        file_path = Path(file_path)
        log_pages = kwargs.get("log_pages", False)
        prefer_pdfium = bool(kwargs.get("prefer_pdfium", False))
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
        page_layout_metrics: list[tuple[int, dict[str, int]]] = []
        reading_order_repaired_pages: list[int] = []
        reading_order_unresolved_pages: list[int] = []
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
                    words = page.extract_words(
                        use_text_flow=False,
                        keep_blank_chars=False,
                    ) or []
                    native_layout_metrics = page_layout_grid_metrics(
                        page,
                        text,
                        words,
                    )
                    reading_order = reconstruct_column_order(
                        words,
                        float(page.width),
                        float(page.height),
                        structural_grid_risk=structural_layout_risk_from_metrics(
                            native_layout_metrics
                        ),
                    )
                    if reading_order.status == "reconstructed":
                        text = normalize_extracted_page_text(reading_order.text)
                        reading_order_repaired_pages.append(i + 1)
                    elif reading_order.status == "ambiguous":
                        reading_order_unresolved_pages.append(i + 1)

                    if len(text.strip()) > MIN_PAGE_CHARS:
                        pages_text.append((i + 1, text))

                    page_layout_metrics.append(
                        (i + 1, native_layout_metrics)
                    )

                    try:
                        table_count += len(page.find_tables() or [])
                    except Exception:
                        pass

                except Exception as e:
                    print(f"fail page {i + 1}: {e}", flush=True)

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
            pages_text = fallback_pages
            parser_used = "pypdfium_text_forced"
            parser_policy = prefer_pdfium_policy
            parser_reason = prefer_pdfium_reason
            reading_order_repaired_pages = []
        elif should_try_text_layer_fallback(pages_text, page_count) or (
            auto_layout_pdfium and layout_risk_pages
        ):
            fallback_pages = extract_with_pdfium(file_path, log_pages=log_pages, company=company)
            if should_use_text_layer_fallback(pages_text, fallback_pages, page_count):
                pages_text = fallback_pages
                parser_used = f"{self.name}+pypdfium_text"
                parser_policy = "auto_text_layer_fallback"
                parser_reason = "low_text_or_cid_text_layer_risk"
                reading_order_repaired_pages = []
            elif auto_layout_pdfium and layout_risk_pages and fallback_pages:
                pages_text = fallback_pages
                parser_used = f"{self.name}+pypdfium_layout"
                parser_policy = "auto_layout_grid_fallback"
                parser_reason = "layout_grid_risk"
                reading_order_repaired_pages = []
            elif auto_layout_pdfium and layout_risk_pages:
                parser_reason = "layout_grid_risk_pdfium_unavailable"

        if layout_risk_pages and not auto_layout_pdfium:
            parser_reason = f"{parser_reason};layout_grid_risk_reported"

        if reading_order_repaired_pages:
            parser_used = f"{parser_used}_column_order"
            parser_reason = (
                f"{parser_reason};coordinate_column_order_pages="
                f"{len(reading_order_repaired_pages)}"
            )

        raw_text, page_spans = build_raw_text_and_page_spans(pages_text)
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
        doc.reading_order_repaired_pages = ";".join(
            str(page) for page in reading_order_repaired_pages
        )
        doc.reading_order_unresolved_pages = ";".join(
            str(page) for page in reading_order_unresolved_pages
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
            if mode not in {"auto", "pdfplumber", "pypdfium"}:
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
) -> dict[str, str | bool]:
    if prefer_pdfium:
        return {
            "prefer_pdfium": True,
            "parser_policy": "cli_forced_pdfium",
            "parser_reason": "cli_forced_pdfium",
        }

    override = parser_override_for(overrides, ticker, pdf)
    if override and override.get("parser_mode") == "pypdfium":
        reason = str(override.get("reason") or "parser_override")
        return {
            "prefer_pdfium": True,
            "parser_policy": "override_pdfium",
            "parser_reason": reason,
        }

    if override and override.get("parser_mode") == "pdfplumber":
        reason = str(override.get("reason") or "parser_override_pdfplumber")
        return {
            "prefer_pdfium": False,
            "parser_policy": "override_pdfplumber",
            "parser_reason": reason,
        }

    return {
        "prefer_pdfium": False,
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


def _select_parse_source(
    source_pdf: Path,
    ticker: str,
    ocr_root: str | Path | None,
) -> tuple[Path, str]:
    """Return the PDF to parse while keeping ``source_pdf`` canonical.

    The normal pipeline parses the downloaded PDF from the raw Drive mirror.
    When ``--ocr-root`` is explicitly supplied, a sidecar searchable PDF can be
    used as the extraction source. The caller still uses the raw PDF for its
    canonical filename, output stem, and downstream ``source_pdf`` identity.
    """
    if ocr_root is not None:
        ocr_ticker_root = Path(ocr_root) / ticker.upper()
        for candidate in (
            ocr_ticker_root / source_pdf.name,
            ocr_ticker_root / f"{source_pdf.stem}_ocr.pdf",
        ):
            if candidate.is_file():
                return candidate, "ocr"
    return source_pdf, "raw"


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
    the old text/page-map baseline cannot survive silently. Text-layer fallback
    rows remain current because they are selected from document quality, not
    from the disabled layout-routing policy.
    """
    actual_policy = row.get("parser_policy", "").strip()
    if expected_parser_policy == AUTO_PDFPLUMBER_COLUMN_POLICY:
        # Documents that needed full text-layer recovery still use PDFium as
        # the final source; rerunning them cannot apply native coordinates and
        # would only churn an otherwise current OCR/text-layer baseline.
        return actual_policy in {
            AUTO_PDFPLUMBER_COLUMN_POLICY,
            "auto_text_layer_fallback",
        }
    if expected_parser_policy:
        return actual_policy == expected_parser_policy
    if actual_policy in {
        "override_pdfium",
        "override_pdfplumber",
        "cli_forced_pdfium",
    }:
        return False
    return auto_layout_pdfium or actual_policy != "auto_layout_grid_fallback"


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
    expected_parser_policy: str = "",
    auto_layout_pdfium: bool = DEFAULT_AUTO_LAYOUT_PDFIUM,
) -> bool:
    if row is None:
        return False

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
        and _parser_policy_matches_request(
            row,
            expected_parser_policy,
            auto_layout_pdfium,
        )
        and _source_fingerprints_match(row, source_metadata)
        and _parse_source_fingerprints_match(row, parse_source_metadata)
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
) -> dict:
    parse_source_pdf = parse_source_pdf or file_path
    out_file, page_map_file = _output_paths(out_root, ticker, file_path)
    return {
        "ticker": ticker,
        "pdf_file": file_path.name,
        "source_pdf": display_path(file_path),
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
        "reading_order_repaired_pages": "",
        "reading_order_unresolved_pages": "",
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
        parser_policy,
        parser_reason,
        auto_layout_pdfium,
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
            prefer_pdfium_policy=parser_policy if prefer_pdfium else None,
            prefer_pdfium_reason=parser_reason if prefer_pdfium else None,
            pdfplumber_policy=parser_policy if not prefer_pdfium else None,
            pdfplumber_reason=parser_reason if not prefer_pdfium else None,
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

        return {
            "ticker": ticker,
            "pdf_file": file_path.name,
            "source_pdf": display_path(file_path),
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
            "reading_order_repaired_pages": getattr(
                doc, "reading_order_repaired_pages", ""
            ),
            "reading_order_unresolved_pages": getattr(
                doc, "reading_order_unresolved_pages", ""
            ),
            "content_hash": doc.content_hash
            or hashlib.sha256(doc.raw_text.encode("utf-8", "ignore")).hexdigest(),
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
    parser_overrides: str | Path | None = DEFAULT_PARSER_OVERRIDES,
    auto_layout_pdfium: bool = DEFAULT_AUTO_LAYOUT_PDFIUM,
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
        out_file, page_map_file = _output_paths(out, ticker_name, pdf)
        parser_request = parser_request_for_pdf(
            ticker=ticker_name,
            pdf=pdf,
            overrides=parser_override_rows,
            prefer_pdfium=prefer_pdfium,
        )
        job_prefer_pdfium = bool(parser_request["prefer_pdfium"])
        job_parser_policy = str(parser_request["parser_policy"])
        job_parser_reason = str(parser_request["parser_reason"])
        job_auto_layout_pdfium = (
            auto_layout_pdfium and job_parser_policy != "override_pdfplumber"
        )
        expected_parser_policy = job_parser_policy
        if job_parser_policy.startswith("override_"):
            summary["parser_overrides_selected"] += 1

        parse_source_pdf, parse_source_kind = _select_parse_source(
            pdf,
            ticker_name,
            ocr_root,
        )
        if parse_source_kind == "ocr":
            summary["ocr_sources_selected"] += 1

        try:
            source_metadata = source_fingerprint(pdf)
        except Exception as error:
            row = _failed_row(
                pdf,
                ticker_name,
                out,
                None,
                f"{type(error).__name__}: {error}",
                parse_source_pdf=parse_source_pdf,
                parse_source_kind=parse_source_kind,
            )
            record_completed(row)
            report_completed(row)
            continue

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
            expected_parser_policy=expected_parser_policy,
            auto_layout_pdfium=job_auto_layout_pdfium,
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
                job_parser_policy,
                job_parser_reason,
                job_auto_layout_pdfium,
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
            "Optional searchable-OCR sidecar root. When supplied, a matching "
            "{ticker}/{raw_filename}.pdf or {ticker}/{raw_stem}_ocr.pdf "
            "is parsed when present. By default the parser reads only the "
            "downloaded raw PDF."
        ),
    )
    ap.add_argument("--ticker", default=None, help="Process one ticker folder, e.g. GAP")
    ap.add_argument(
        "--pdf-file",
        default=None,
        help=(
            "Process one PDF filename or stem within the selected root/ticker, "
            "for example AMZN-Amazon-2022.pdf."
        ),
    )
    ap.add_argument(
        "--prefer-pdfium",
        action="store_true",
        help=(
            "Force pypdfium text extraction for the selected PDFs. Use for "
            "targeted layout-order repairs after confirming pdfplumber "
            "linearized a visual grid or multi-column page incorrectly."
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
        parser_overrides=args.parser_overrides,
        auto_layout_pdfium=args.auto_layout_pdfium,
    )


if __name__ == "__main__":
    main()
