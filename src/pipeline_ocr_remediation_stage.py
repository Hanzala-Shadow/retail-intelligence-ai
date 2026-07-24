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
from typing import Callable

import pypdfium2 as pdfium
import pytesseract


OVERRIDE_FIELDS = [
    "logical_source_id", "source_version_id", "extraction_artifact_id",
    "ticker", "pdf_stem", "page", "source_page_sha256", "before_text_sha256",
    "after_text_sha256", "detection_signal", "action", "verification_result",
    "ocr_engine", "created_at", "active",
]
LOG_FIELDS = [
    "timestamp_utc", "logical_source_id", "source_version_id", "ticker", "pdf_stem",
    "pages", "reason", "action", "before_hash", "after_hash", "verification_result", "note",
]
RENDER_SCALE = 3.0
MIN_OCR_CONFIDENCE = 45
REPLACEMENT_MARKERS = ("\ufffd", "ï¿½", "Ã¯Â¿Â½")


def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def has_cid_artifact(text: str) -> bool:
    return bool(re.search(r"\(cid:\d+\)", text, re.I))


def has_replacement_characters(text: str) -> bool:
    return any(marker in text for marker in REPLACEMENT_MARKERS)


def is_likely_garbled(text: str) -> bool:
    if not text.strip():
        return True
    chars = [char for char in text if not char.isspace()]
    letters = sum(char.isalpha() for char in chars)
    words = re.findall(r"\S+", text)
    long_words = sum(len(word) > 24 for word in words)
    readable_ratio = letters / max(len(chars), 1)
    return readable_ratio < 0.40 or long_words > max(2, len(words) // 20)


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
    words = re.findall(r"[A-Za-z][A-Za-z'-]*", text)
    chars = [char for char in text if not char.isspace()]
    letters = sum(char.isalpha() for char in chars)
    score = 100.0 * letters / max(len(chars), 1) + min(len(words), 300) / 10
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
    pytesseract.pytesseract.tesseract_cmd = discover_tesseract()
    page = pdf_doc[page_number_1indexed - 1]
    image = page.render(scale=RENDER_SCALE).to_pil()
    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    words: list[str] = []
    for word, raw_conf in zip(data["text"], data["conf"]):
        if not str(word).strip():
            continue
        try:
            confidence = float(raw_conf)
        except (TypeError, ValueError):
            confidence = -1
        if confidence >= MIN_OCR_CONFIDENCE or confidence == -1:
            words.append(str(word))
    return " ".join(words)


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


def remediate_page_texts(
    existing_pages: dict[int, str],
    ocr: Callable[[int], str],
) -> tuple[dict[int, str], list[dict]]:
    final_pages = dict(existing_pages)
    outcomes: list[dict] = []
    for page, before in sorted(existing_pages.items()):
        signals = detect_page_quality(before)
        if not signals:
            continue
        before_hash = _sha_text(before)
        try:
            candidate = ocr(page)
        except Exception as exc:
            outcomes.append({"page": page, "signal": ";".join(signals), "action": "manual_review_hold", "before_hash": before_hash, "after_hash": before_hash, "verification": "ocr_failed", "note": str(exc)})
            continue
        candidate_signals = detect_page_quality(candidate)
        improved = quality_score(candidate) > quality_score(before) + 1.0
        verified = bool(candidate.strip()) and not candidate_signals and improved
        if verified:
            final_pages[page] = candidate
            action, verification = "approved_page_override", "verified_quality_improvement"
        else:
            action, verification = "manual_review_hold", "ocr_not_verified_or_not_better"
        outcomes.append({"page": page, "signal": ";".join(signals), "action": action, "before_hash": before_hash, "after_hash": _sha_text(final_pages[page]), "verification": verification, "note": ";".join(candidate_signals)})
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


def _append_log(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    existing = _read_csv(path)
    key_fields = (
        "logical_source_id",
        "source_version_id",
        "pdf_stem",
        "pages",
        "action",
        "before_hash",
        "after_hash",
        "verification_result",
    )
    seen: set[tuple[str, ...]] = set()
    merged: list[dict] = []
    for row in existing + rows:
        key = tuple(str(row.get(field) or "") for field in key_fields)
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
    _write_csv(path, merged, LOG_FIELDS)


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
    log_path: str | Path = "reports/pipeline_ocr_remediation_log.csv",
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
            final_pages, outcomes = remediate_page_texts(existing_pages, lambda page: ocr_function(pdf_doc, page))
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
            if result["action"] == "approved_page_override" and ocr_function is ocr_page:
                ocr_engine = Path(discover_tesseract()).name
            override_rows.append({
                "logical_source_id": row.get("logical_source_id", ""), "source_version_id": row.get("source_version_id", ""), "extraction_artifact_id": row.get("extraction_artifact_id", ""),
                "ticker": row.get("ticker", ""), "pdf_stem": Path(str(row.get("pdf_file") or "")).stem, "page": result["page"], "source_page_sha256": result["before_hash"], "before_text_sha256": result["before_hash"], "after_text_sha256": result["after_hash"], "detection_signal": result["signal"], "action": result["action"], "verification_result": result["verification"], "ocr_engine": ocr_engine, "created_at": now, "active": "true" if result["action"] == "approved_page_override" else "false",
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
        log_rows = [{"timestamp_utc": now, "logical_source_id": row.get("logical_source_id", ""), "source_version_id": row.get("source_version_id", ""), "ticker": row.get("ticker", ""), "pdf_stem": Path(str(row.get("pdf_file") or "")).stem, "pages": result["page"], "reason": result["signal"], "action": result["action"], "before_hash": old_hash, "after_hash": new_hash if changed else old_hash, "verification_result": result["verification"], "note": result["note"]} for result in outcomes]
        _append_log(Path(log_path), log_rows)
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
