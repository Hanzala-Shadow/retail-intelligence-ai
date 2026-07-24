"""
check_chunking_coverage.py

For a given PDF, determines which pages actually made it into the final
chunks and which pages got silently dropped somewhere in the pipeline
(parsing, section-splitting, or chunking).

Method (self-sufficient — doesn't require database access or page_start/
page_end metadata, since we don't have that for every chunk):
  1. Extract real text from the source PDF, page by page.
  2. Load every chunk (or section) file that exists locally for that report.
  3. For each PDF page, take a distinctive text fingerprint and check whether
     it appears anywhere in the combined chunk text.
  4. A page whose fingerprint is NOT found anywhere = likely dropped.

A page with genuinely little/no extractable text (e.g. a full-page photo or
a blank divider page) will correctly show as "thin, likely no real content"
rather than being falsely flagged as a gap.
"""

import argparse
import re
from pathlib import Path
import pypdfium2 as pdfium
import pandas as pd

PDF_ROOT = Path("data/01_raw/sustainability")
CHUNKS_ROOT = Path("data/Teamwork/04_chunks/esg")  # adjust if your local path differs


def normalize_words(text, min_word_len=4):
    """Order-independent word set, ignoring short/common words. Robust to
    reading-order differences between our raw pdfium extraction and the
    pipeline's dedicated reading-order-aware parser (confirmed necessary
    2026-07-23: an exact-phrase-match approach produced false gaps on
    complex multi-column ESG layouts, even at 100% real word coverage)."""
    text = re.sub(r"[^\w\s]", "", text.lower())
    return set(w for w in text.split() if len(w) >= min_word_len)


def load_all_chunk_words(ticker, pdf_stem):
    ticker_dir = CHUNKS_ROOT / ticker
    if not ticker_dir.exists():
        return set()
    matching_files = [f for f in ticker_dir.glob("*.txt") if pdf_stem in f.name]
    all_words = set()
    for f in matching_files:
        all_words |= normalize_words(f.read_text(encoding="utf-8", errors="replace"))
    return all_words


def check_coverage(pdf_path, ticker, pdf_stem, min_overlap_pct=70):
    doc = pdfium.PdfDocument(str(pdf_path))
    n_pages = len(doc)

    chunk_words = load_all_chunk_words(ticker, pdf_stem)
    if not chunk_words:
        print(f"  WARNING: no local chunk files found for {ticker}/{pdf_stem} — "
              f"cannot check coverage without them.")
        doc.close()
        return None

    results = []
    for page_num in range(n_pages):
        page = doc[page_num]
        text_page = page.get_textpage()
        page_text = text_page.get_text_range()
        page_words = normalize_words(page_text)

        if len(page_words) < 15:
            status = "THIN (likely no real text content, not a gap)"
            overlap_pct = None
        else:
            overlap = page_words & chunk_words
            overlap_pct = len(overlap) / len(page_words) * 100
            status = "COVERED" if overlap_pct >= min_overlap_pct else "GAP — LOW WORD OVERLAP"

        results.append({
            "page": page_num + 1,
            "distinctive_words": len(page_words),
            "overlap_pct": round(overlap_pct, 1) if overlap_pct is not None else None,
            "status": status,
        })

    doc.close()
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--company-list", required=True,
                     help="CSV with columns: ticker, pdf_stem, pdf_filename")
    args = ap.parse_args()

    companies = pd.read_csv(args.company_list)
    all_results = []

    for _, row in companies.iterrows():
        ticker, pdf_stem, pdf_filename = row["ticker"], row["pdf_stem"], row["pdf_filename"]
        pdf_path = PDF_ROOT / ticker / pdf_filename

        print(f"\n{'=' * 60}")
        print(f"{ticker} — {pdf_filename}")
        print("=" * 60)

        if not pdf_path.exists():
            print(f"  SKIPPED: PDF not found at {pdf_path}")
            continue

        results = check_coverage(pdf_path, ticker, pdf_stem)
        if results is None:
            continue

        n_total = len(results)
        n_covered = sum(1 for r in results if r["status"] == "COVERED")
        n_gap = sum(1 for r in results if "GAP" in r["status"])
        n_thin = sum(1 for r in results if "THIN" in r["status"])

        print(f"Total pages: {n_total}")
        print(f"Covered: {n_covered}")
        print(f"Gaps (real content, not found in chunks): {n_gap}")
        print(f"Thin (no real content expected): {n_thin}")

        if n_gap > 0:
            print(f"\nPages with gaps:")
            for r in results:
                if "GAP" in r["status"]:
                    print(f"  page {r['page']} ({r['distinctive_words']} words, {r['overlap_pct']}% overlap)")

        for r in results:
            all_results.append({"ticker": ticker, "pdf_filename": pdf_filename, **r})

    out_df = pd.DataFrame(all_results)
    out_path = Path("reports/chunking_coverage_check.csv")
    out_df.to_csv(out_path, index=False)
    print(f"\n{'=' * 60}")
    print(f"Full per-page results saved to {out_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()