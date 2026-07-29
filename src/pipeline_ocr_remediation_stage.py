"""Page-level ESG OCR remediation with downstream regeneration.

This stage never edits completed chunks. It detects unsafe page text, OCRs each
affected page once, verifies an improvement, rewrites the parsed document and
page map, then rebuilds the selected document's sections and chunks.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Sequence

import pypdfium2 as pdfium
import pytesseract


# One row per inspected page, and the stage's only audit record. It carries the
# page-level hashes for the override itself plus the parsed-document hashes the
# override produced, so a page can be traced to the document version it landed
# in. drive_to_db.py reads these as artifact_role=page_ocr_override.
OVERRIDE_FIELDS = [
    "logical_source_id", "source_version_id", "extraction_artifact_id",
    "ticker", "pdf_stem", "page", "source_page_sha256", "before_text_sha256",
    "after_text_sha256", "parsed_doc_before_sha256", "parsed_doc_after_sha256",
    "detection_signal", "action", "verification_result", "note",
    "recovery_method", "attempted_methods", "ocr_engine", "created_at", "active",
]
# Recovery candidates, tried cheapest-first. Most flagged pages are not scanned
# at all: the embedded text layer is intact and only the extractor that produced
# the parsed document mishandled it, so simply re-reading the page costs a
# fraction of a render-plus-Tesseract pass and preserves the original line
# structure. OCR is the last resort, for pages that really are images.
RECOVERY_METHOD_PDFIUM = "pdfium_text"
RECOVERY_METHOD_OCR = "ocr"
RENDER_SCALE = 3.0
MIN_OCR_CONFIDENCE = 45
REPLACEMENT_MARKERS = ("\ufffd", "ï¿½", "Ã¯Â¿Â½")


# ESG data pages -- emissions inventories, SASB indexes, multi-year appendices --
# are legitimately mostly digits. Rating readability by letter share alone scores
# a clean metrics table like garbage, which both sends it to OCR needlessly and
# then refuses the OCR result at verification, so the pages carrying the actual
# metrics could never be repaired. Readability is measured over recognizable
# tokens instead, where a well-formed number counts exactly as much as a word.
TOKEN_EDGE_PUNCT = "([{<\"'`.,;:!?)]}>%"
MIN_CONTENT_RATIO = 0.40
SINGLE_CHAR_WORDS = frozenset({"a", "A", "I"})


def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def has_cid_artifact(text: str) -> bool:
    return bool(re.search(r"\(cid:\d+\)", text, re.I))


def has_replacement_characters(text: str) -> bool:
    return any(marker in text for marker in REPLACEMENT_MARKERS)


def is_content_token(token: str) -> bool:
    """True when a token reads as a word, a number, or an identifier code."""
    core = token.strip(TOKEN_EDGE_PUNCT)
    if not core:
        return False
    alnum = sum(char.isalnum() for char in core)
    if not alnum:
        return False
    # A lone character is as likely to be OCR speckle as real content, so it
    # counts only as a digit or one of the two English single-letter words.
    if len(core) == 1:
        return core.isdigit() or core in SINGLE_CHAR_WORDS
    return alnum / len(core) >= 0.5


def content_ratio(text: str) -> float:
    """Share of non-space characters sitting inside recognizable tokens."""
    tokens = re.findall(r"\S+", text)
    if not tokens:
        return 0.0
    total = sum(len(token) for token in tokens)
    content = sum(len(token) for token in tokens if is_content_token(token))
    return content / max(total, 1)


def is_likely_garbled(text: str) -> bool:
    if not text.strip():
        return True
    words = re.findall(r"\S+", text)
    long_words = sum(len(word) > 24 for word in words)
    return content_ratio(text) < MIN_CONTENT_RATIO or long_words > max(2, len(words) // 20)


def detect_page_quality(text: str) -> list[str]:
    signals: list[str] = []
    if not text.strip():
        signals.append("empty_text")
    if has_cid_artifact(text):
        signals.append("cid_artifact")
    if has_replacement_characters(text):
        signals.append("replacement_character")
    if is_likely_garbled(text) and "empty_text" not in signals:
        signals.append("garbled_or_low_readable_text")
    return signals


def detect_garbled(text: str) -> tuple[bool, str]:
    signals = detect_page_quality(text)
    return bool(signals), ";".join(signals)


def quality_score(text: str) -> float:
    if not text.strip():
        return -1000.0
    tokens = [token for token in re.findall(r"\S+", text) if is_content_token(token)]
    score = 100.0 * content_ratio(text) + min(len(tokens), 300) / 10
    score -= 80 * len(re.findall(r"\(cid:\d+\)", text, re.I))
    score -= 80 * sum(text.count(marker) for marker in REPLACEMENT_MARKERS)
    return score


def discover_tesseract() -> str:
    configured = os.environ.get("ESG_TESSERACT_CMD", "").strip()
    candidates = [configured, shutil.which("tesseract") or ""]
    if os.name == "nt":
        candidates.extend([
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        ])
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate))
    raise RuntimeError("Tesseract was not found; set ESG_TESSERACT_CMD or add tesseract to PATH")


def ocr_page(pdf_doc, page_number_1indexed: int) -> str:
    """OCR one page, preserving its line and column reading order.

    Delegates to the searchable-PDF helper so a remediated page is segmented the
    same way ocr_pdf.py segments one: words grouped into lines, lines split at
    column gaps, then ordered. Joining the raw word list with spaces instead
    would collapse a two-column ESG page into a single run-on line, which then
    flows into sections and chunks with no structure left to split on.
    """
    from ocr_pdf import ocr_image_page, preprocess_image

    pytesseract.pytesseract.tesseract_cmd = discover_tesseract()
    page = pdf_doc[page_number_1indexed - 1]
    bitmap = page.render(scale=RENDER_SCALE)
    image = bitmap.to_pil()
    processed = preprocess_image(image)
    try:
        return ocr_image_page(processed, page_number_1indexed, MIN_OCR_CONFIDENCE, False).text
    finally:
        for handle in (processed, image, bitmap, page):
            if handle is not None and hasattr(handle, "close"):
                handle.close()


def extract_pdfium_text(pdf_doc, page_number_1indexed: int) -> str:
    """Re-read a page's embedded text layer, without rendering or OCR."""
    # Normalized exactly as the parser normalized the rest of the document, so
    # recovered text splices into it without introducing a formatting seam.
    from pdf_parser import normalize_extracted_page_text

    page = None
    text_page = None
    try:
        page = pdf_doc[page_number_1indexed - 1]
        text_page = page.get_textpage()
        return normalize_extracted_page_text(text_page.get_text_range() or "")
    finally:
        for handle in (text_page, page):
            if handle is not None and hasattr(handle, "close"):
                handle.close()


def page_texts(text: str, page_map: list[dict]) -> dict[int, str]:
    pages: dict[int, str] = {}
    for row in page_map:
        try:
            page = int(row["page"])
            start, end = int(row["char_start"]), int(row["char_end"])
        except (KeyError, TypeError, ValueError):
            continue
        if start < 0 or end < start or end > len(text):
            raise ValueError(f"invalid page map bounds for page {row.get('page')}")
        pages[page] = text[start:end]
    return pages


def regenerate_document(pages: dict[int, str]) -> tuple[str, list[dict]]:
    parts: list[str] = []
    rows: list[dict] = []
    offset = 0
    for page in sorted(pages):
        if parts:
            separator = "\n\n"
            parts.append(separator)
            offset += len(separator)
        page_text = pages[page].strip()
        start = offset
        parts.append(page_text)
        offset += len(page_text)
        rows.append({"page": page, "char_start": start, "char_end": offset, "char_count": len(page_text)})
    return "".join(parts), rows


def _recover_page(
    page: int,
    before: str,
    sources: Sequence[tuple[str, Callable[[int], str]]],
) -> tuple[str | None, str, list[str], list[str], bool]:
    """Try each source in order, returning the first text that verifies.

    Returns ``(accepted_text, winning_method, attempted, reasons, any_ran)``.
    ``accepted_text`` is None when no source produced verified text. ``any_ran``
    distinguishes engines that ran and did not help from engines that never ran.
    """
    attempted: list[str] = []
    reasons: list[str] = []
    any_ran = False
    for method, provide in sources:
        attempted.append(method)
        try:
            candidate = provide(page)
        except Exception as exc:
            reasons.append(str(exc))
            continue
        any_ran = True
        candidate_signals = detect_page_quality(candidate)
        improved = quality_score(candidate) > quality_score(before) + 1.0
        if candidate.strip() and not candidate_signals and improved:
            return candidate, method, attempted, reasons, any_ran
        reasons.append(";".join(candidate_signals) or "not_better")
    return None, "", attempted, reasons, any_ran


def remediate_page_texts(
    existing_pages: dict[int, str],
    ocr: Callable[[int], str] | None = None,
    *,
    sources: Sequence[tuple[str, Callable[[int], str]]] | None = None,
) -> tuple[dict[int, str], list[dict]]:
    if sources is None:
        if ocr is None:
            raise ValueError("remediate_page_texts requires an ocr callable or explicit sources")
        sources = ((RECOVERY_METHOD_OCR, ocr),)
    final_pages = dict(existing_pages)
    outcomes: list[dict] = []
    for page, before in sorted(existing_pages.items()):
        signals = detect_page_quality(before)
        if not signals:
            continue
        before_hash = _sha_text(before)
        accepted, method, attempted, reasons, any_ran = _recover_page(page, before, sources)
        if accepted is not None:
            final_pages[page] = accepted
            action, verification = "approved_page_override", "verified_quality_improvement"
        else:
            action = "manual_review_hold"
            # No engine running at all is a different operational problem from
            # an engine that ran and did not improve the page.
            verification = "ocr_not_verified_or_not_better" if any_ran else "ocr_failed"
        outcomes.append({"page": page, "signal": ";".join(signals), "action": action, "before_hash": before_hash, "after_hash": _sha_text(final_pages[page]), "verification": verification, "note": "; ".join(reasons), "method": method, "attempted": ";".join(attempted)})
    return final_pages, outcomes


def _read_csv(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8-sig") as source:
        return list(csv.DictReader(source))


def _write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fields, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)
    temp.replace(path)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def _selected(row: dict, ticker: str | None, pdf_stem: str | None, pdf_file: str | None) -> bool:
    if ticker and str(row.get("ticker") or "").upper() != ticker.upper():
        return False
    stem = Path(str(row.get("pdf_file") or "")).stem
    return not (pdf_stem or pdf_file) or stem == (pdf_stem or Path(str(pdf_file)).stem)


def run(
    *,
    parse_index: str | Path,
    sections_index: str | Path,
    chunks_index: str | Path,
    ticker: str | None = None,
    pdf_stem: str | None = None,
    pdf_file: str | None = None,
    parsed_root: str | Path = "data/02_interim/esg_text",
    sections_root: str | Path = "data/03_sections/esg",
    chunks_root: str | Path = "data/04_chunks/esg",
    source_registry: str | Path = "data/00_reference/esg_source_registry.csv",
    override_index: str | Path = "data/00_reference/esg_page_ocr_overrides.csv",
    chunk_history: str | Path = "data/00_reference/esg_chunk_history.csv",
    ocr_function: Callable[[object, int], str] = ocr_page,
) -> list[dict]:
    if not (ticker or pdf_stem or pdf_file):
        raise ValueError("page remediation must be scoped by --ticker, --pdf-stem, or --pdf-file")
    parse_path = Path(parse_index)
    parse_rows = _read_csv(parse_path)
    targets = [row for row in parse_rows if _selected(row, ticker, pdf_stem, pdf_file)]
    all_outcomes: list[dict] = []
    for row in targets:
        parsed_path = Path(str(row.get("parsed_text_file") or ""))
        map_path = Path(str(row.get("page_map_file") or ""))
        source_pdf = Path(str(row.get("source_pdf") or ""))
        if not (parsed_path.is_file() and map_path.is_file() and source_pdf.is_file()):
            continue
        text = parsed_path.read_text(encoding="utf-8", errors="replace")
        existing_pages = page_texts(text, _read_csv(map_path))
        pdf_doc = pdfium.PdfDocument(str(source_pdf))
        try:
            final_pages, outcomes = remediate_page_texts(
                existing_pages,
                sources=(
                    (RECOVERY_METHOD_PDFIUM, lambda page: extract_pdfium_text(pdf_doc, page)),
                    (RECOVERY_METHOD_OCR, lambda page: ocr_function(pdf_doc, page)),
                ),
            )
        finally:
            pdf_doc.close()
        if not outcomes:
            continue
        now = datetime.now(UTC).isoformat()
        changed = [result for result in outcomes if result["action"] == "approved_page_override"]
        old_hash = _sha_text(text)
        final_text, final_map = regenerate_document(final_pages)
        new_hash = _sha_text(final_text)
        if changed:
            old_chunks = [chunk for chunk in _read_csv(Path(chunks_index)) if str(chunk.get("ticker") or "").upper() == str(row.get("ticker") or "").upper() and str(chunk.get("pdf_stem") or "") == Path(str(row.get("pdf_file") or "")).stem]
            history_path = Path(chunk_history)
            history = _read_csv(history_path)
            history_fields = list(dict.fromkeys((["lifecycle_state", "superseded_at", "superseded_by_parsed_hash"] + list(old_chunks[0].keys())) if old_chunks else ["lifecycle_state", "superseded_at", "superseded_by_parsed_hash"]))
            history.extend({**chunk, "lifecycle_state": "superseded", "superseded_at": now, "superseded_by_parsed_hash": new_hash} for chunk in old_chunks)
            _write_csv(history_path, history, history_fields)
            _write_text(parsed_path, final_text)
            _write_csv(map_path, final_map, ["page", "char_start", "char_end", "char_count"])
            row["content_hash"] = new_hash; row["char_count"] = str(len(final_text)); row["parsed_at"] = now
            row["quality_flags"] = ";".join(flag for flag in str(row.get("quality_flags") or "").split(";") if flag and flag != "held_for_ocr")
            _write_csv(parse_path, parse_rows, list(parse_rows[0].keys()))
            import section_splitter_esg
            import esg_chunker
            stem = Path(str(row.get("pdf_file") or "")).stem
            doc_ticker = str(row.get("ticker") or "").upper()
            section_splitter_esg.run(parsed_root, sections_root, sections_index, ticker=doc_ticker, pdf_stem=stem, force=True)
            esg_chunker.run(sections_root, chunks_root, chunks_index, sections_index=sections_index, parse_index=parse_index, source_registry=source_registry, ticker=doc_ticker, pdf_stem=stem, force=True)
        else:
            flags = {flag for flag in str(row.get("quality_flags") or "").split(";") if flag}
            flags.add("held_for_ocr")
            row["quality_flags"] = ";".join(sorted(flags))
            _write_csv(parse_path, parse_rows, list(parse_rows[0].keys()))
        override_rows = _read_csv(Path(override_index))
        for result in outcomes:
            ocr_engine = ""
            if result["method"] == RECOVERY_METHOD_OCR and ocr_function is ocr_page:
                ocr_engine = Path(discover_tesseract()).name
            override_rows.append({
                "logical_source_id": row.get("logical_source_id", ""), "source_version_id": row.get("source_version_id", ""), "extraction_artifact_id": row.get("extraction_artifact_id", ""),
                "ticker": row.get("ticker", ""), "pdf_stem": Path(str(row.get("pdf_file") or "")).stem, "page": result["page"], "source_page_sha256": result["before_hash"], "before_text_sha256": result["before_hash"], "after_text_sha256": result["after_hash"], "parsed_doc_before_sha256": old_hash, "parsed_doc_after_sha256": new_hash if changed else old_hash, "detection_signal": result["signal"], "action": result["action"], "verification_result": result["verification"], "note": result["note"], "recovery_method": result["method"], "attempted_methods": result["attempted"], "ocr_engine": ocr_engine, "created_at": now, "active": "true" if result["action"] == "approved_page_override" else "false",
            })
        unique_override_rows: list[dict] = []
        seen_override: set[tuple[str, ...]] = set()
        for override_row in override_rows:
            override_key = tuple(
                str(override_row.get(field) or "")
                for field in (
                    "logical_source_id", "source_version_id", "pdf_stem", "page",
                    "before_text_sha256", "after_text_sha256", "action",
                )
            )
            if override_key in seen_override:
                continue
            seen_override.add(override_key)
            unique_override_rows.append(override_row)
        _write_csv(Path(override_index), unique_override_rows, OVERRIDE_FIELDS)
        all_outcomes.extend(outcomes)
    return all_outcomes


def main() -> None:
    parser = argparse.ArgumentParser(description="Run scoped page-level ESG OCR remediation and rebuild downstream objects.")
    parser.add_argument("--parse-index", default="data/00_reference/esg_parse_index.csv")
    parser.add_argument("--sections-index", default="data/00_reference/esg_sections_index.csv")
    parser.add_argument("--chunks-index", default="data/00_reference/esg_chunks_index.csv")
    parser.add_argument("--ticker"); parser.add_argument("--pdf-stem"); parser.add_argument("--pdf-file")
    parser.add_argument("--force", action="store_true", help="Accepted for fast-run symmetry; remediation is always scoped and stale-aware.")
    args = parser.parse_args()
    try:
        results = run(parse_index=args.parse_index, sections_index=args.sections_index, chunks_index=args.chunks_index, ticker=args.ticker, pdf_stem=args.pdf_stem, pdf_file=args.pdf_file)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"Page remediation outcomes: {len(results)}")


if __name__ == "__main__":
    main()
