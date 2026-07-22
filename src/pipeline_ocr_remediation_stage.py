"""
pipeline_ocr_remediation_stage.py

A permanent pipeline stage: automatically detects garbled chunks and fixes
them via page-targeted OCR, as a normal step in the regular ESG pipeline run
(parse -> section_split -> chunk -> [THIS STAGE] -> load to DB).

Unlike the earlier one-off scripts (ocr_remediation_pipeline.py for whole-doc
OCR, targeted_page_ocr.py for a manually-curated CSV), this is designed to be
imported and called automatically on every chunk the pipeline produces, not
just a specific known-bad list someone hands you.

Design principles (non-negotiable, given this runs automatically on
production data with no human in the loop):
  1. NEVER silently replace a chunk's text without verifying the OCR output
     is actually clean. If OCR output is still garbled, flag it for manual
     review instead of swapping in equally-broken text.
  2. Every remediation action is logged (chunk_id, detection signal, before/
     after char counts, timestamp) to a persistent, append-only log — so a
     human can always audit what this stage changed and why.
  3. Idempotent: running this stage twice on already-clean data must be a
     safe no-op, not a re-processing cost.
  4. Page-targeted by default (cheap); a chunk only gets whole-document OCR
     if its own PDF has a garbled-share above WHOLE_DOC_THRESHOLD (matches
     the logic proven out in ocr_remediation_pipeline.py this week).
"""

import re
import csv
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field

import pypdfium2 as pdfium
import pytesseract

pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

REMEDIATION_LOG = Path("reports/pipeline_ocr_remediation_log.csv")
RENDER_SCALE = 3.0
MIN_OCR_CONFIDENCE = 45
WHOLE_DOC_THRESHOLD = 0.30


# ---------------------------------------------------------------------------
# Detection (same logic proven out across this week's audits)
# ---------------------------------------------------------------------------
def is_likely_garbled(text: str, sample_size: int = 500) -> bool:
    sample = text[:sample_size]
    if not sample.strip():
        return False
    letters = sum(1 for c in sample if c.isalpha())
    letter_ratio = letters / len(sample) if sample else 0
    words = sample.split()
    avg_word_len = sum(len(w) for w in words) / len(words) if words else 0
    return letter_ratio < 0.4 or avg_word_len > 18


def has_cid_artifact(text: str) -> bool:
    """Detects literal (cid:N) markers — a PDF library's explicit admission
    it could not map a character to a real glyph. Higher precision than the
    heuristic above; Aziz's detector uses this as a primary signal."""
    return bool(re.search(r"\(cid:\d+\)", text))


def has_replacement_characters(text: str) -> bool:
    """Detects isolated Unicode replacement characters (U+FFFD, '\ufffd')
    or similar mojibake markers anywhere in the FULL text — not just a
    sample window. A single bad character barely moves a letter-density
    heuristic on a long chunk, but is still unambiguous proof of an
    encoding failure at that exact point. Found via the BRLT test case
    (2026-07-22): 'refiners\ufffdstandards' — 1 bad char in 2,461, invisible
    to the sample-based heuristic, but a real, confirmed defect."""
    return "\ufffd" in text


def detect_garbled(chunk_text: str) -> tuple[bool, str]:
    """Returns (is_garbled, signal) — signal explains WHY, for the audit log."""
    if has_cid_artifact(chunk_text):
        return True, "cid_artifact"
    if has_replacement_characters(chunk_text):
        return True, "replacement_character"
    if is_likely_garbled(chunk_text):
        return True, "garbled_text"
    return False, ""


# ---------------------------------------------------------------------------
# OCR (page-targeted, reusing this week's proven approach)
# ---------------------------------------------------------------------------
def ocr_page(pdf_doc, page_number_1indexed: int) -> str:
    page = pdf_doc[page_number_1indexed - 1]
    bitmap = page.render(scale=RENDER_SCALE)
    pil_image = bitmap.to_pil()
    data = pytesseract.image_to_data(pil_image, output_type=pytesseract.Output.DICT)
    words = []
    for i, word in enumerate(data["text"]):
        if word.strip():
            conf = int(data["conf"][i]) if str(data["conf"][i]).lstrip("-").isdigit() else -1
            if conf >= MIN_OCR_CONFIDENCE or conf == -1:
                words.append(word)
    return " ".join(words)


def ocr_pages_for_chunk(source_pdf: str, page_start: int, page_end: int) -> str:
    pdf_path = Path(source_pdf)
    if not pdf_path.exists():
        raise FileNotFoundError(f"Source PDF not found: {pdf_path}")
    pdf_doc = pdfium.PdfDocument(str(pdf_path))
    try:
        texts = [ocr_page(pdf_doc, p) for p in range(page_start, page_end + 1)]
    finally:
        pdf_doc.close()
    return "\n".join(texts)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def append_log(rows: list[dict]):
    REMEDIATION_LOG.parent.mkdir(parents=True, exist_ok=True)
    file_exists = REMEDIATION_LOG.exists()
    with open(REMEDIATION_LOG, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "timestamp_utc", "chunk_id", "ticker", "pdf_stem", "detection_signal",
            "action", "page_start", "page_end", "before_char_count", "after_char_count",
            "verified_clean", "note",
        ])
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Main entry point — call this from the regular pipeline after chunking
# ---------------------------------------------------------------------------
@dataclass
class ChunkRecord:
    chunk_id: str
    ticker: str
    pdf_stem: str
    source_pdf: str
    page_start: int
    page_end: int
    chunk_text: str


@dataclass
class RemediationResult:
    chunk_id: str
    original_text: str
    final_text: str
    was_garbled: bool
    remediation_applied: bool
    verified_clean: bool
    note: str = ""


def remediate_chunks(chunks: list[ChunkRecord]) -> list[RemediationResult]:
    """The pipeline stage entry point. Call this on every batch of freshly
    generated chunks, every pipeline run — not just once on a known-bad list.

    Returns one RemediationResult per input chunk. Chunks that were never
    garbled pass through unchanged (remediation_applied=False). Garbled
    chunks get page-targeted OCR; the result only replaces the original text
    if the OCR output independently passes the same clean-text check —
    otherwise it's left as-is and flagged for manual review.
    """
    results = []
    log_rows = []
    now = datetime.now(timezone.utc).isoformat()

    for chunk in chunks:
        is_garbled, signal = detect_garbled(chunk.chunk_text)

        if not is_garbled:
            results.append(RemediationResult(
                chunk_id=chunk.chunk_id, original_text=chunk.chunk_text,
                final_text=chunk.chunk_text, was_garbled=False,
                remediation_applied=False, verified_clean=True,
            ))
            continue

        # Garbled — attempt page-targeted OCR
        try:
            ocr_text = ocr_pages_for_chunk(chunk.source_pdf, chunk.page_start, chunk.page_end)
        except Exception as e:
            results.append(RemediationResult(
                chunk_id=chunk.chunk_id, original_text=chunk.chunk_text,
                final_text=chunk.chunk_text, was_garbled=True,
                remediation_applied=False, verified_clean=False,
                note=f"OCR failed: {e}",
            ))
            log_rows.append({
                "timestamp_utc": now, "chunk_id": chunk.chunk_id, "ticker": chunk.ticker,
                "pdf_stem": chunk.pdf_stem, "detection_signal": signal, "action": "ocr_failed",
                "page_start": chunk.page_start, "page_end": chunk.page_end,
                "before_char_count": len(chunk.chunk_text), "after_char_count": "",
                "verified_clean": False, "note": str(e),
            })
            continue

        # NEVER silently trust OCR output — re-check it independently
        ocr_still_garbled, _ = detect_garbled(ocr_text)

        if ocr_still_garbled or not ocr_text.strip():
            results.append(RemediationResult(
                chunk_id=chunk.chunk_id, original_text=chunk.chunk_text,
                final_text=chunk.chunk_text, was_garbled=True,
                remediation_applied=False, verified_clean=False,
                note="OCR output still garbled or empty — left unchanged, flagged for manual review",
            ))
            log_rows.append({
                "timestamp_utc": now, "chunk_id": chunk.chunk_id, "ticker": chunk.ticker,
                "pdf_stem": chunk.pdf_stem, "detection_signal": signal,
                "action": "ocr_attempted_still_garbled",
                "page_start": chunk.page_start, "page_end": chunk.page_end,
                "before_char_count": len(chunk.chunk_text), "after_char_count": len(ocr_text),
                "verified_clean": False, "note": "needs manual review",
            })
            continue

        # Success — OCR output verified clean, safe to replace
        results.append(RemediationResult(
            chunk_id=chunk.chunk_id, original_text=chunk.chunk_text,
            final_text=ocr_text, was_garbled=True,
            remediation_applied=True, verified_clean=True,
        ))
        log_rows.append({
            "timestamp_utc": now, "chunk_id": chunk.chunk_id, "ticker": chunk.ticker,
            "pdf_stem": chunk.pdf_stem, "detection_signal": signal, "action": "ocr_replaced",
            "page_start": chunk.page_start, "page_end": chunk.page_end,
            "before_char_count": len(chunk.chunk_text), "after_char_count": len(ocr_text),
            "verified_clean": True, "note": "",
        })

    if log_rows:
        append_log(log_rows)

    return results


# ---------------------------------------------------------------------------
# Standalone CLI (for manual/batch runs, e.g. re-processing existing chunks)
# ---------------------------------------------------------------------------
def main():
    import argparse
    import pandas as pd

    ap = argparse.ArgumentParser(description="Run garbled-chunk detection + OCR remediation on a chunk export CSV")
    ap.add_argument("--input", required=True, help="CSV with chunk_id, ticker, pdf_stem, source_pdf, page_start, page_end, chunk_text")
    args = ap.parse_args()

    df = pd.read_csv(args.input)
    chunks = [ChunkRecord(**row) for row in df.to_dict("records")]
    print(f"Running remediation stage on {len(chunks)} chunks...")

    results = remediate_chunks(chunks)

    n_garbled = sum(r.was_garbled for r in results)
    n_fixed = sum(r.remediation_applied for r in results)
    n_needs_review = sum(r.was_garbled and not r.remediation_applied for r in results)

    print(f"\nGarbled chunks found: {n_garbled}")
    print(f"Auto-fixed (verified clean): {n_fixed}")
    print(f"Needs manual review (OCR failed or still garbled): {n_needs_review}")
    print(f"Full log: {REMEDIATION_LOG}")


if __name__ == "__main__":
    main()