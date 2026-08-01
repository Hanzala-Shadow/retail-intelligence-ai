from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from statistics import median

import pypdfium2 as pdfium
import pytesseract
from PIL import ImageEnhance, ImageOps

from pdf_parser import (
    OCR_MIN_NONSPACE_CHARS,
    _parse_source_metadata,
    _write_page_map,
    _write_text,
    display_path,
    source_fingerprint,
    text_quality_metrics,
    upsert_index_rows,
)


OCR_CONFIG = "--oem 3 --psm 3"
DEFAULT_MIN_CONFIDENCE = 45.0
SHORT_KEEP = {"AI", "CEO", "COO", "DEI", "ESG"}
METRIC_RE = re.compile(r"^\$?\d[\d,]*(?:\.\d+)?[KMB+%]?$", re.IGNORECASE)
NAV_LABELS = ("LETTER", "APPROACH", "ENVIRONMENT", "SOCIAL", "GOVERNANCE")


@dataclass
class OCRResult:
    full_text: str
    page_spans: list[dict]
    page_count: int
    page_results: list["OCRPageResult"]


@dataclass
class LineSegment:
    top: int
    left: int
    right: int
    bottom: int
    order: tuple[int, int, int, int]
    text: str
    words: list[dict] = field(default_factory=list)
    split_line: bool = False


@dataclass
class OCRPageResult:
    page_number: int
    text: str
    lines: list[LineSegment]
    image_width: int
    image_height: int


def write_page_map(page_map_path: Path, page_spans: list[dict]) -> None:
    """Use the parser's atomic page-map write path for OCR replacements."""
    _write_page_map(page_map_path, page_spans)


def pdf_literal(text: str) -> str:
    """Encode text as a WinAnsi PDF literal string for a simple Type1 font."""
    safe = (
        text.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u00a0", " ")
    )
    safe = safe.encode("cp1252", errors="replace").decode("cp1252")
    safe = safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    safe = safe.replace("\r", " ").replace("\n", " ")
    return f"({safe})"


def _line_pdf_bbox(
    line: LineSegment,
    page_width: float,
    page_height: float,
    image_width: int,
    image_height: int,
) -> tuple[float, float, float, float]:
    x0 = line.left / max(image_width, 1) * page_width
    x1 = line.right / max(image_width, 1) * page_width
    y_top = line.top / max(image_height, 1) * page_height
    y_bottom = line.bottom / max(image_height, 1) * page_height
    return x0, page_height - y_bottom, x1, page_height - y_top


def _hidden_text_command(text: str, x: float, y: float, font_size: float, scale_x: float = 100.0) -> str:
    if not text:
        return ""
    scale_x = max(20.0, min(300.0, scale_x))
    return (
        "q\n"
        "BT\n"
        "/Focr %.4f Tf\n"
        "3 Tr\n"
        "%.4f Tz\n"
        "1 0 0 1 %.4f %.4f Tm\n"
        "%s Tj\n"
        "ET\n"
        "Q\n"
    ) % (font_size, scale_x, x, y, pdf_literal(text))


def _ordered_text_layer(page_result: OCRPageResult, page_width: float, page_height: float) -> bytes:
    lines = [line for line in page_result.lines if line.text.strip()]
    if not lines:
        return b""

    # Keep every line inside the page so PDF search/copy sees a stable reading
    # order. The layer is invisible, so the font can shrink on dense pages.
    usable_height = max(page_height - 48.0, 24.0)
    font_size = max(2.0, min(8.0, usable_height / max(len(lines), 1) * 0.85))
    line_gap = font_size * 1.18
    x = 24.0
    y = page_height - 24.0 - font_size

    parts = []
    max_text_width = max(page_width - 48.0, 1.0)
    for line in lines:
        approx_width = max(len(line.text) * font_size * 0.48, 1.0)
        scale_x = min(100.0, max_text_width / approx_width * 100.0)
        parts.append(_hidden_text_command(line.text, x, max(y, 2.0), font_size, scale_x))
        y -= line_gap

    return "".join(parts).encode("latin-1", errors="replace")


def _positioned_text_layer(page_result: OCRPageResult, page_width: float, page_height: float) -> bytes:
    parts = []
    for line in page_result.lines:
        text = line.text.strip()
        if not text:
            continue
        x0, y0, x1, y1 = _line_pdf_bbox(
            line,
            page_width,
            page_height,
            page_result.image_width,
            page_result.image_height,
        )
        box_width = max(x1 - x0, 1.0)
        box_height = max(y1 - y0, 2.0)
        font_size = max(2.0, min(14.0, box_height * 0.85))
        approx_width = max(len(text) * font_size * 0.48, 1.0)
        scale_x = box_width / approx_width * 100.0
        parts.append(_hidden_text_command(text, x0, y0, font_size, scale_x))
    return "".join(parts).encode("latin-1", errors="replace")


def write_searchable_pdf(
    input_pdf: Path,
    output_pdf: Path,
    page_results: list[OCRPageResult],
    text_mode: str,
) -> None:
    """Write a searchable PDF with a hidden text layer from ordered OCR lines."""
    try:
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject
    except ImportError as exc:
        raise RuntimeError(
            "Writing searchable OCR PDFs requires pypdf. Install it with: pip install pypdf"
        ) from exc

    if text_mode not in {"ordered", "positioned"}:
        raise ValueError("text_mode must be 'ordered' or 'positioned'")

    reader = PdfReader(str(input_pdf))
    writer = PdfWriter()
    result_by_page = {result.page_number: result for result in page_results}

    for page_number, source_page in enumerate(reader.pages, start=1):
        writer.add_page(source_page)
        page = writer.pages[-1]
        page_result = result_by_page.get(page_number)
        if page_result is None:
            continue

        page_width = float(page.mediabox.width)
        page_height = float(page.mediabox.height)
        if text_mode == "ordered":
            layer = _ordered_text_layer(page_result, page_width, page_height)
        else:
            layer = _positioned_text_layer(page_result, page_width, page_height)
        if not layer:
            continue

        resources = page.get("/Resources")
        if resources is None:
            resources = DictionaryObject()
            page[NameObject("/Resources")] = resources
        else:
            resources = resources.get_object()

        fonts = resources.get("/Font")
        if fonts is None:
            fonts = DictionaryObject()
            resources[NameObject("/Font")] = fonts
        else:
            fonts = fonts.get_object()

        fonts[NameObject("/Focr")] = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
                NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
            }
        )

        stream = DecodedStreamObject()
        stream.set_data(layer)
        stream_ref = writer._add_object(stream)

        contents = page.get("/Contents")
        if contents is None:
            page[NameObject("/Contents")] = stream_ref
        else:
            contents_obj = contents.get_object() if hasattr(contents, "get_object") else contents
            if isinstance(contents_obj, ArrayObject):
                page[NameObject("/Contents")] = ArrayObject(list(contents_obj) + [stream_ref])
            else:
                page[NameObject("/Contents")] = ArrayObject([contents, stream_ref])

    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_pdf.with_name(f"{output_pdf.name}.tmp")
    try:
        with tmp_path.open("wb") as handle:
            writer.write(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, output_pdf)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def write_image_based_searchable_pdf(
    input_pdf: Path,
    output_pdf: Path,
    page_results: list[OCRPageResult],
    text_mode: str,
    scale: float,
) -> None:
    """Write a searchable PDF after discarding the source PDF text layer.

    This mode is useful for PDFs whose embedded text is corrupted, for example
    heavy ``(cid:...)`` extraction artifacts. It renders each source page to an
    image-only PDF, then adds the OCR text layer to that clean base.
    """
    image_pdf = output_pdf.with_name(f"{output_pdf.stem}.image_base.tmp.pdf")
    pdf = pdfium.PdfDocument(str(input_pdf))
    images = []
    try:
        for page_index in range(len(pdf)):
            page = pdf[page_index]
            bitmap = page.render(scale=scale)
            image = bitmap.to_pil()
            try:
                images.append(image.convert("RGB"))
            finally:
                image.close()
                bitmap.close()
                page.close()

        if not images:
            raise RuntimeError("cannot build image-based PDF from a zero-page document")

        images[0].save(
            image_pdf,
            "PDF",
            save_all=True,
            append_images=images[1:],
            resolution=72.0 * scale,
        )
        write_searchable_pdf(image_pdf, output_pdf, page_results, text_mode)
    finally:
        pdf.close()
        for image in images:
            image.close()
        if image_pdf.exists():
            image_pdf.unlink()


def update_parse_index(
    parse_index: Path,
    input_pdf: Path,
    output_text: Path,
    page_map_file: Path,
    result: OCRResult,
    output_pdf: Path | None = None,
) -> None:
    ticker = output_text.parent.name.upper()
    char_count = len(result.full_text)
    nonspace_chars = len("".join(result.full_text.split()))
    status = "parsed" if nonspace_chars >= OCR_MIN_NONSPACE_CHARS else "ocr_required"
    quality = text_quality_metrics(result.full_text, result.page_count, char_count)
    source_metadata = source_fingerprint(input_pdf)
    parse_source_pdf = output_pdf if output_pdf is not None else input_pdf
    parse_source_metadata = source_fingerprint(parse_source_pdf)
    row = {
        "ticker": ticker,
        "pdf_file": input_pdf.name,
        "source_pdf": display_path(input_pdf),
        **source_metadata,
        **_parse_source_metadata(parse_source_pdf, "ocr", parse_source_metadata),
        "parsed_text_file": display_path(output_text),
        "page_map_file": display_path(page_map_file),
        "status": status,
        "error_message": "",
        "page_count": result.page_count,
        "char_count": char_count,
        "table_count": 0,
        "content_hash": hashlib.sha256(
            result.full_text.encode("utf-8", "ignore")
        ).hexdigest(),
        "parsed_at": datetime.now(UTC).isoformat(),
        **quality,
    }
    upsert_index_rows(parse_index, [row], replace_all=False)


def preprocess_image(image):
    gray = ImageOps.grayscale(image)
    gray = ImageOps.autocontrast(gray, cutoff=1)
    return ImageEnhance.Sharpness(gray).enhance(1.4)


def parse_confidence(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return -1.0


def clean_ocr_line(line: str) -> str:
    line = " ".join(line.split())
    line = re.sub(r"^(?:ol|wy|[=|~>\\]+)\s+", "", line, flags=re.IGNORECASE)
    line = re.sub(r"(\d)\s+\.(\d)", r"\1.\2", line)
    line = re.sub(r"\bmore_\b", "more", line)
    line = re.sub(r"\bAl\b", "AI", line)
    line = re.sub(r"\bIbs\b", "lbs", line)
    line = re.sub(r"\bunlass\b", "unless", line, flags=re.IGNORECASE)
    line = re.sub(r"\bacore\b", "a core", line, flags=re.IGNORECASE)
    line = re.sub(r"\b10 THE\b", "TO THE", line, flags=re.IGNORECASE)
    line = re.sub(r"\bTOIMPACT\b", "TO IMPACT", line, flags=re.IGNORECASE)
    line = re.sub(r"\bON THE LISE\b", "ON THE LTSE", line, flags=re.IGNORECASE)
    line = re.sub(r"\bESG\s+\d{1,2}\b", "ESG", line, flags=re.IGNORECASE)
    line = re.sub(r"\bLTSE\s+uP\b", "LTSE", line)
    return line.strip()


def looks_like_nav_line(line: str) -> bool:
    upper = line.upper()
    label_count = sum(label in upper for label in NAV_LABELS)
    if label_count >= 3:
        return True
    if upper.startswith("THREDUP ") and re.search(
        r"(?:O|0)?[0-4O]\s+(?:LETTER|APPROACH|ENVIRONMENT|SOCIAL|GOVERNANCE)",
        upper,
    ):
        return True
    if re.fullmatch(r"(?:O|0){1,2}\s+LETTER", upper):
        return True
    if re.fullmatch(
        r"(?:O|0)?[0-4O]\s+(?:LETTER|APPROACH|ENVIRONMENT|SOCIAL|GOVERNANCE)",
        upper,
    ):
        return True
    return False


def should_drop_line(line: str) -> bool:
    if not line:
        return True

    upper = line.upper()
    compact = re.sub(r"\W+", "", upper)
    if looks_like_nav_line(line):
        return True
    if re.fullmatch(r"\d{4}\s+IMPACT\s+REPORT", upper):
        return True
    if re.fullmatch(r"ESG\s*\d+", upper):
        return True
    if re.fullmatch(r"\d{1,2}", line):
        return True

    alnum_count = sum(ch.isalnum() for ch in line)
    if alnum_count == 0:
        return True
    if len(compact) <= 2 and compact not in SHORT_KEEP and not METRIC_RE.fullmatch(line):
        return True
    if alnum_count / max(len(line), 1) < 0.35 and not METRIC_RE.fullmatch(line):
        return True
    return False


def enough_text_for_column_split(words: list[dict]) -> bool:
    text = "".join(word["text"] for word in words)
    return len(words) >= 2 and sum(char.isalnum() for char in text) >= 12


def split_words_on_large_gaps(words: list[dict], image_width: int) -> tuple[list[list[dict]], list[float]]:
    if not words:
        return [], []

    gap_threshold = max(26, image_width * 0.012)
    split_indices: list[int] = []
    gap_midpoints: list[float] = []
    previous_right = words[0]["left"] + words[0]["width"]

    for index, word in enumerate(words[1:], start=1):
        gap = word["left"] - previous_right
        if (
            gap >= gap_threshold
            and enough_text_for_column_split(words[:index])
            and enough_text_for_column_split(words[index:])
        ):
            split_indices.append(index)
            gap_midpoints.append((previous_right + word["left"]) / 2)
        previous_right = max(previous_right, word["left"] + word["width"])

    if not split_indices:
        return [words], []

    segments: list[list[dict]] = []
    start = 0
    for split_index in split_indices:
        segments.append(words[start:split_index])
        start = split_index
    segments.append(words[start:])
    return segments, gap_midpoints


def words_to_text(words: list[dict]) -> str:
    return clean_ocr_line(" ".join(word["text"] for word in words))


def order_segments(segments: list[LineSegment], gap_midpoints: list[float]) -> list[LineSegment]:
    if len(gap_midpoints) < 4:
        return sorted(segments, key=lambda item: item.order)

    split_x = median(gap_midpoints)
    split_segments = [segment for segment in segments if segment.split_line]
    band_start = min(segment.top for segment in split_segments) - 20
    band_end = max(segment.top for segment in split_segments) + 20
    before = [segment for segment in segments if segment.top < band_start]
    band = [segment for segment in segments if band_start <= segment.top <= band_end]
    after = [segment for segment in segments if segment.top > band_end]
    left_column = [segment for segment in band if segment.left < split_x]
    right_column = [segment for segment in band if segment.left >= split_x]

    if len(left_column) < 3 or len(right_column) < 3:
        return sorted(segments, key=lambda item: item.order)

    return (
        sorted(before, key=lambda item: item.order)
        + sorted(left_column, key=lambda item: (item.top, item.left))
        + sorted(right_column, key=lambda item: (item.top, item.left))
        + sorted(after, key=lambda item: item.order)
    )


def ocr_image_page(image, page_number: int, min_confidence: float, raw_text: bool) -> OCRPageResult:
    data = pytesseract.image_to_data(
        image,
        lang="eng",
        config=OCR_CONFIG,
        output_type=pytesseract.Output.DICT,
    )
    grouped_words: dict[tuple[int, int, int], list[dict]] = {}
    for index, text in enumerate(data["text"]):
        text = (text or "").strip()
        if not text:
            continue
        confidence = parse_confidence(data["conf"][index])
        if confidence < min_confidence:
            continue

        key = (
            int(data["block_num"][index]),
            int(data["par_num"][index]),
            int(data["line_num"][index]),
        )
        grouped_words.setdefault(key, []).append(
            {
                "text": text,
                "left": int(data["left"][index]),
                "top": int(data["top"][index]),
                "width": int(data["width"][index]),
                "height": int(data["height"][index]),
                "conf": confidence,
            }
        )

    line_segments: list[LineSegment] = []
    gap_midpoints: list[float] = []
    for source_order, key in enumerate(sorted(grouped_words)):
        words = sorted(grouped_words[key], key=lambda word: word["left"])
        full_line = words_to_text(words)
        if should_drop_line(full_line):
            continue

        split_word_groups, split_gaps = split_words_on_large_gaps(words, image.width)
        split_line = len(split_word_groups) > 1
        gap_midpoints.extend(split_gaps)
        for segment_order, segment_words in enumerate(split_word_groups):
            text = words_to_text(segment_words)
            if should_drop_line(text):
                continue
            left = min(word["left"] for word in segment_words)
            top = min(word["top"] for word in segment_words)
            right = max(word["left"] + word["width"] for word in segment_words)
            bottom = max(word["top"] + word["height"] for word in segment_words)
            line_segments.append(
                LineSegment(
                    top=top,
                    left=left,
                    right=right,
                    bottom=bottom,
                    order=(*key, source_order * 10 + segment_order),
                    text=text,
                    words=segment_words,
                    split_line=split_line,
                )
            )

    ordered_lines = order_segments(line_segments, gap_midpoints)
    text = "\n".join(segment.text for segment in ordered_lines)
    if raw_text:
        text = pytesseract.image_to_string(image, lang="eng", config=OCR_CONFIG).strip()

    return OCRPageResult(
        page_number=page_number,
        text=text,
        lines=ordered_lines,
        image_width=image.width,
        image_height=image.height,
    )


def ocr_pdf(
    input_pdf: Path,
    output_text: Path,
    scale: float,
    min_confidence: float,
    raw_text: bool,
    output_pdf: Path | None = None,
    pdf_text_mode: str = "ordered",
    pdf_base: str = "original",
) -> OCRResult:
    if not input_pdf.is_file():
        raise FileNotFoundError(f"Input PDF not found: {input_pdf}")
    if pdf_base not in {"original", "image"}:
        raise ValueError("pdf_base must be 'original' or 'image'")

    output_text.parent.mkdir(parents=True, exist_ok=True)
    if output_pdf is not None:
        output_pdf.parent.mkdir(parents=True, exist_ok=True)

    pdf = pdfium.PdfDocument(str(input_pdf))
    page_count = len(pdf)
    extracted_pages: list[str] = []
    page_spans: list[dict] = []
    page_results: list[OCRPageResult] = []
    cursor = 0

    print(f"Input: {input_pdf}")
    print(f"Pages: {page_count}")
    print(f"Output text: {output_text}")
    print(f"Minimum OCR confidence: {min_confidence:g}")
    if output_pdf is not None:
        print(f"Output searchable PDF: {output_pdf}")
        print(f"Searchable PDF text mode: {pdf_text_mode}")
        print(f"Searchable PDF base: {pdf_base}")

    try:
        for page_index in range(page_count):
            page_number = page_index + 1
            print(f"OCR page {page_number}/{page_count}", flush=True)

            page = pdf[page_index]
            bitmap = page.render(scale=scale)
            image = bitmap.to_pil()
            processed_image = preprocess_image(image)

            try:
                page_result = ocr_image_page(
                    processed_image,
                    page_number,
                    min_confidence,
                    raw_text,
                )
            finally:
                processed_image.close()
                image.close()
                bitmap.close()
                page.close()

            page_results.append(page_result)
            text = page_result.text
            page_text = f"===== PAGE {page_number} =====\n\n{text.strip()}\n"
            if extracted_pages:
                extracted_pages.append("\n")
                cursor += 1

            start = cursor
            extracted_pages.append(page_text)
            cursor += len(page_text)
            page_spans.append(
                {
                    "page": page_number,
                    "char_start": start,
                    "char_end": cursor,
                    "char_count": len(page_text),
                }
            )
    finally:
        pdf.close()

    full_text = "".join(extracted_pages).strip() + "\n"
    _write_text(output_text, full_text)

    if output_pdf is not None:
        if pdf_base == "image":
            write_image_based_searchable_pdf(
                input_pdf,
                output_pdf,
                page_results,
                pdf_text_mode,
                scale,
            )
        else:
            write_searchable_pdf(input_pdf, output_pdf, page_results, pdf_text_mode)

    print(f"Finished: wrote {len(full_text):,} characters")
    return OCRResult(
        full_text=full_text,
        page_spans=page_spans,
        page_count=page_count,
        page_results=page_results,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="OCR a PDF using pypdfium2 and Tesseract."
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Input PDF path",
    )
    parser.add_argument(
        "--output-text",
        required=True,
        type=Path,
        help="Output text-file path",
    )
    parser.add_argument(
        "--output-pdf",
        type=Path,
        default=None,
        help="Optional searchable OCR PDF output path. Requires pypdf.",
    )
    parser.add_argument(
        "--pdf-text-mode",
        choices=("ordered", "positioned"),
        default="ordered",
        help=(
            "Hidden text placement for --output-pdf. 'ordered' keeps the "
            "Tesseract reading order for copy/search/extraction; 'positioned' "
            "places lines near OCR boxes for closer visual highlights."
        ),
    )
    parser.add_argument(
        "--pdf-base",
        choices=("original", "image"),
        default="original",
        help=(
            "Use 'original' to preserve the source PDF and add OCR text. "
            "Use 'image' to render pages to an image-only PDF first, which "
            "removes corrupted embedded text layers such as CID artifacts."
        ),
    )
    parser.add_argument(
        "--parse-index",
        type=Path,
        default=None,
        help="Optional esg_parse_index.csv path to update after OCR.",
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=3.2,
        help="PDF rendering scale; default is 3.2",
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=DEFAULT_MIN_CONFIDENCE,
        help="Minimum Tesseract word confidence kept in text output; default is 45",
    )
    parser.add_argument(
        "--raw-text",
        action="store_true",
        help="Write raw Tesseract page text instead of cleaned confidence-filtered text.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        print(f"Tesseract version: {pytesseract.get_tesseract_version()}")
        result = ocr_pdf(
            args.input,
            args.output_text,
            args.scale,
            args.min_confidence,
            args.raw_text,
            args.output_pdf,
            args.pdf_text_mode,
            args.pdf_base,
        )
        page_map_file = args.output_text.with_suffix(".pages.csv")
        write_page_map(page_map_file, result.page_spans)
        print(f"Page map: {page_map_file}")
        if args.parse_index:
            update_parse_index(
                args.parse_index,
                args.input,
                args.output_text,
                page_map_file,
                result,
                args.output_pdf,
            )
            print(f"Updated parse index: {args.parse_index}")
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
