"""Compare production's REAL final output against the candidate region reader.

Read-only. Nothing here is wired into production, no corpus data is written,
and ``src/pdf_parser.py``, ``src/esg_navigation.py``, ``src/esg_reading_order.py``,
and ``src/esg_reading_regions.py`` are only imported, never edited.

Why this script exists
-----------------------
``scripts/compare_reading_parsers.py`` (2026-07-30) compared the candidate
against ``esg_reading_order.reconstruct_column_order`` fed pdfplumber words
*after* navigation stripping, and called that "production". It is not
production. Real production (``src.pdf_parser.PDFParser._parse_with_pymupdf_layout``)
runs PyMuPDF words with NO navigation stripping through a two-stage pipeline:
the column reader first, then, when a gating condition fires, a second region
pass (``pdf_parser.reconstruct_region_order``). See
``reports/READING_ORDER_SESSION_HANDOFF_2026-07-30.md`` for the full account
of why the old comparison's results are void.

This script instead:

1. Reproduces production's real final per-page text by calling the actual
   functions from ``src/pdf_parser.py`` in the same order and with the same
   gating logic as ``_parse_with_pymupdf_layout`` (mirrored, not
   reimplemented from a description -- every helper is imported directly).
2. Feeds the candidate (``esg_reading_regions.reconstruct_by_regions``) the
   SAME PyMuPDF words, unstripped -- the identical-input comparison that has
   never been run before.
3. Runs a clearly separated secondary check: candidate fed navigation-stripped
   pdfplumber words, for reference only, to see whether stripping matters.
4. Draws two labelled samples (human_validation: the 13 pages already scored
   by a human in reports/llm_reader_review_2026-07-30/blind_key.json;
   diff_sample: ~40 fresh pages, >=15 documents, <=3 pages/document, seed
   below, where identical-input production and candidate text differ).
5. Writes results.json, summary.md, a blinded judging queue, and renders,
   mirroring the protocol in reports/llm_reader_review_2026-07-30/.

No paid API calls are made. This produces the queue and renders only.
"""

from __future__ import annotations

import json
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pdfplumber

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import fitz  # noqa: E402  (PyMuPDF -- imported the same way pdf_parser.py does)

from esg_navigation import build_navigation_profile, clean_navigation  # noqa: E402
from esg_reading_order import reconstruct_column_order  # noqa: E402
from esg_reading_regions import reconstruct_by_regions  # noqa: E402
from pdf_parser import (  # noqa: E402
    _has_mixed_width_header,
    _pymupdf_words,
    _valid_pymupdf_table_candidates,
    layout_grid_risk_from_metrics,
    normalize_extracted_page_text,
    pymupdf_page_layout_grid_metrics,
    reconstruct_region_order,
)

RAW_ROOT = REPO_ROOT / "data" / "01_raw" / "sustainability"
PRIOR_BLIND_KEY = REPO_ROOT / "reports" / "llm_reader_review_2026-07-30" / "blind_key.json"
DATE = "2026-07-30"
OUT_DIR = REPO_ROOT / "reports" / f"production_vs_candidate_{DATE}"
SEED = 20260730
RENDER_DPI = 110
DIFF_SAMPLE_TARGET = 40
DIFF_SAMPLE_MAX_PER_DOC = 3
DIFF_SAMPLE_MIN_DOCS = 15
DIFF_SAMPLE_PAGES_SCANNED_PER_DOC = 8
# Mirrors the near-empty guard style of compare_reading_parsers.py's
# run_document (which skipped pages with body_word_count == 0). This script
# has no navigation-stripped "body" concept for the primary comparison, so
# the guard is applied to the raw PyMuPDF word count instead.
MIN_WORDS_FOR_SAMPLE = 20


# ---------------------------------------------------------------------------
# Production: mirrors _parse_with_pymupdf_layout's PyMuPDF branch exactly.
# ---------------------------------------------------------------------------


def production_page_record(page) -> dict:
    """Reproduce production's real final text and branch for one PyMuPDF page.

    Mirrors src/pdf_parser.py `_parse_with_pymupdf_layout` (~lines 1552-1650)
    faithfully, calling the real functions rather than reimplementing them.
    """

    width = float(page.rect.width)
    height = float(page.rect.height)

    text = normalize_extracted_page_text(page.get_text("text", sort=False) or "")
    words = _pymupdf_words(page)
    native_layout_metrics = pymupdf_page_layout_grid_metrics(page, text, words)
    table_blocks = _valid_pymupdf_table_candidates(page)
    structural_grid_risk = layout_grid_risk_from_metrics(native_layout_metrics)
    reading_order = reconstruct_column_order(
        words, width, height, structural_grid_risk=structural_grid_risk
    )
    mixed_width_header = _has_mixed_width_header(words, width, height)
    use_region_pass = (
        bool(table_blocks)
        or (
            reading_order.status == "ambiguous"
            and reading_order.reason != "navigation_contents_layout"
            and not structural_grid_risk
        )
        or (reading_order.status == "reconstructed" and mixed_width_header)
    )
    region_order = (
        reconstruct_region_order(words, width, height, table_blocks)
        if use_region_pass
        else None
    )

    if region_order is not None and region_order.status == "reconstructed":
        final_text = normalize_extracted_page_text(region_order.text)
        branch = "region_pass"
    elif reading_order.status == "reconstructed":
        final_text = normalize_extracted_page_text(reading_order.text)
        branch = "column_order"
    else:
        final_text = text
        branch = "native"

    return {
        "text": final_text,
        "branch": branch,
        "use_region_pass": use_region_pass,
        "reading_order_status": reading_order.status,
        "region_order_status": (region_order.status if region_order is not None else None),
        "table_block_count": len(table_blocks),
        "structural_grid_risk": structural_grid_risk,
        "mixed_width_header": mixed_width_header,
        "words": words,
        "native_text": text,
        "width": width,
        "height": height,
    }


# ---------------------------------------------------------------------------
# Candidate: identical PyMuPDF words, unstripped.
# ---------------------------------------------------------------------------


def candidate_identical_input(words: list[dict], width: float, height: float) -> dict:
    """Feed the candidate the same raw PyMuPDF words used for production.

    If reconstruct_by_regions raises on the raw word dicts, that is recorded
    as an incompatibility finding rather than patched around.
    """

    try:
        result = reconstruct_by_regions(words, width, height)
        return {
            "ok": True,
            "text": result.text,
            "status": result.status,
            "reason": result.reason,
            "error": None,
        }
    except Exception as error:  # noqa: BLE001 -- deliberately broad; this IS the finding
        return {
            "ok": False,
            "text": None,
            "status": None,
            "reason": None,
            "error": f"{type(error).__name__}: {error}",
        }


# ---------------------------------------------------------------------------
# Sample assembly
# ---------------------------------------------------------------------------


def pdf_inventory() -> list[tuple[str, Path]]:
    return sorted((path.parent.name, path) for path in RAW_ROOT.glob("*/*.pdf"))


def load_human_validation_pages() -> list[dict]:
    """Read the 13 human_validation pages from the prior round's blind key."""

    rows = json.loads(PRIOR_BLIND_KEY.read_text(encoding="utf-8"))
    pages = [row for row in rows if row.get("set") == "human_validation"]
    if len(pages) != 13:
        raise RuntimeError(
            f"Expected 13 human_validation pages in {PRIOR_BLIND_KEY}, found {len(pages)}."
        )
    return [
        {"ticker": row["ticker"], "pdf_file": row["pdf_file"], "page": row["page"]}
        for row in pages
    ]


def build_page_record(
    ticker: str, pdf_file: str, path: Path, page_number: int, page, group: str
) -> dict | None:
    """Full per-page record (step 4/5). Returns None if below the word-count guard."""

    prod = production_page_record(page)
    if len(prod["words"]) < MIN_WORDS_FOR_SAMPLE:
        return None
    cand = candidate_identical_input(prod["words"], prod["width"], prod["height"])
    differs = bool(cand["ok"] and cand["text"] != prod["text"])
    return {
        "ticker": ticker,
        "pdf_file": pdf_file,
        "page": page_number,
        "group": group,
        "production_branch": prod["branch"],
        "use_region_pass": prod["use_region_pass"],
        "reading_order_status": prod["reading_order_status"],
        "region_order_status": prod["region_order_status"],
        "table_block_count": prod["table_block_count"],
        "structural_grid_risk": prod["structural_grid_risk"],
        "mixed_width_header": prod["mixed_width_header"],
        "source_word_count": len(prod["words"]),
        "production_text": prod["text"],
        "candidate_ok": cand["ok"],
        "candidate_error": cand["error"],
        "candidate_status": cand["status"],
        "candidate_reason": cand["reason"],
        "candidate_text": cand["text"] if cand["ok"] else "",
        "texts_differ": differs,
        "page_width": prod["width"],
        "page_height": prod["height"],
    }


def build_human_validation_sample() -> tuple[list[dict], list[dict]]:
    """Returns (records, crash_log)."""

    records: list[dict] = []
    crash_log: list[dict] = []
    by_file: dict[tuple[str, str], list[int]] = {}
    for item in load_human_validation_pages():
        by_file.setdefault((item["ticker"], item["pdf_file"]), []).append(item["page"])

    for (ticker, pdf_file), pages in by_file.items():
        path = RAW_ROOT / ticker / pdf_file
        document = fitz.open(str(path))
        try:
            for page_number in pages:
                page = document.load_page(page_number - 1)
                record = build_page_record(
                    ticker, pdf_file, path, page_number, page, "human_validation"
                )
                if record is None:
                    # The fixed 13-page set is required regardless of the
                    # near-empty guard; keep the page but flag it.
                    prod = production_page_record(page)
                    cand = candidate_identical_input(
                        prod["words"], prod["width"], prod["height"]
                    )
                    record = {
                        "ticker": ticker,
                        "pdf_file": pdf_file,
                        "page": page_number,
                        "group": "human_validation",
                        "production_branch": prod["branch"],
                        "use_region_pass": prod["use_region_pass"],
                        "reading_order_status": prod["reading_order_status"],
                        "region_order_status": prod["region_order_status"],
                        "table_block_count": prod["table_block_count"],
                        "structural_grid_risk": prod["structural_grid_risk"],
                        "mixed_width_header": prod["mixed_width_header"],
                        "source_word_count": len(prod["words"]),
                        "production_text": prod["text"],
                        "candidate_ok": cand["ok"],
                        "candidate_error": cand["error"],
                        "candidate_status": cand["status"],
                        "candidate_reason": cand["reason"],
                        "candidate_text": cand["text"] if cand["ok"] else "",
                        "texts_differ": bool(cand["ok"] and cand["text"] != prod["text"]),
                        "page_width": prod["width"],
                        "page_height": prod["height"],
                        "below_word_count_guard": True,
                    }
                if not record.get("candidate_ok", True):
                    crash_log.append(
                        {
                            "ticker": ticker,
                            "pdf_file": pdf_file,
                            "page": page_number,
                            "set": "human_validation",
                            "error": record["candidate_error"],
                        }
                    )
                records.append(record)
        finally:
            document.close()
    return records, crash_log


def build_diff_sample() -> tuple[list[dict], list[dict], list[dict]]:
    """Returns (records, crash_log, sampled_pages_meta).

    Scans documents in a fixed random order, tries a bounded number of pages
    per document, and keeps pages where the identical-input production and
    candidate texts differ, until the target size and document-diversity
    floor are both met.
    """

    rng = random.Random(SEED)
    inventory = pdf_inventory()
    shuffled = inventory[:]
    rng.shuffle(shuffled)

    records: list[dict] = []
    crash_log: list[dict] = []
    per_doc: Counter[tuple[str, str]] = Counter()
    docs_used: set[tuple[str, str]] = set()

    for ticker, path in shuffled:
        if len(records) >= DIFF_SAMPLE_TARGET and len(docs_used) >= DIFF_SAMPLE_MIN_DOCS:
            break
        try:
            document = fitz.open(str(path))
        except Exception:
            continue
        try:
            page_count = int(document.page_count)
            if page_count == 0:
                continue
            page_numbers = list(range(1, page_count + 1))
            rng.shuffle(page_numbers)
            candidates_this_doc = page_numbers[:DIFF_SAMPLE_PAGES_SCANNED_PER_DOC]
            taken_this_doc = 0
            for page_number in candidates_this_doc:
                if taken_this_doc >= DIFF_SAMPLE_MAX_PER_DOC:
                    break
                try:
                    page = document.load_page(page_number - 1)
                except Exception:
                    continue
                record = build_page_record(
                    ticker, path.name, path, page_number, page, "diff_sample"
                )
                if record is None:
                    continue
                if not record["candidate_ok"]:
                    crash_log.append(
                        {
                            "ticker": ticker,
                            "pdf_file": path.name,
                            "page": page_number,
                            "set": "diff_sample",
                            "error": record["candidate_error"],
                        }
                    )
                    continue
                if not record["texts_differ"]:
                    continue
                records.append(record)
                per_doc[(ticker, path.name)] += 1
                docs_used.add((ticker, path.name))
                taken_this_doc += 1
        finally:
            document.close()

    # Deliberately not truncated to exactly DIFF_SAMPLE_TARGET: a blind
    # records[:TARGET] slice can silently drop an entire document's pages if
    # that document's contribution happened to land past the cut index,
    # undercounting document diversity below the floor even though it was
    # met at collection time. "~40" allows a small overshoot instead.
    docs_used = {(row["ticker"], row["pdf_file"]) for row in records}
    if len(docs_used) < DIFF_SAMPLE_MIN_DOCS:
        raise RuntimeError(
            f"diff_sample only reached {len(docs_used)} distinct documents "
            f"(need >= {DIFF_SAMPLE_MIN_DOCS}); widen the scan."
        )
    if max(per_doc.values(), default=0) > DIFF_SAMPLE_MAX_PER_DOC:
        raise RuntimeError("diff_sample violated the max-pages-per-document cap.")

    sampled_meta = [
        {
            "ticker": row["ticker"],
            "pdf_file": row["pdf_file"],
            "page": row["page"],
            "group": row["group"],
        }
        for row in records
    ]
    return records, crash_log, sampled_meta


# ---------------------------------------------------------------------------
# Secondary, clearly separated run: candidate with navigation stripping.
# Not identical-input; kept out of the primary summary numbers.
# ---------------------------------------------------------------------------


def extract_pdfplumber_words(page) -> list[dict]:
    try:
        return page.extract_words(
            use_text_flow=False, keep_blank_chars=False, extra_attrs=["size", "upright"]
        ) or []
    except TypeError:
        return page.extract_words(use_text_flow=False, keep_blank_chars=False) or []


def build_nav_stripped_secondary(records: list[dict]) -> list[dict]:
    """Candidate fed navigation-stripped pdfplumber words, for reference only.

    This deliberately uses pdfplumber (not PyMuPDF) words, because
    clean_navigation's rotated/edge-character detection is built on
    pdfplumber's char stream. Grouped by document so build_navigation_profile
    runs once per file.
    """

    by_doc: dict[tuple[str, str], list[dict]] = {}
    for row in records:
        by_doc.setdefault((row["ticker"], row["pdf_file"]), []).append(row)

    secondary: list[dict] = []
    for (ticker, pdf_file), rows in by_doc.items():
        path = RAW_ROOT / ticker / pdf_file
        try:
            with pdfplumber.open(path) as pdf:
                profile = build_navigation_profile(
                    [(p.chars, float(p.width), float(p.height)) for p in pdf.pages]
                )
                for row in rows:
                    page = pdf.pages[row["page"] - 1]
                    width, height = float(page.width), float(page.height)
                    words = extract_pdfplumber_words(page)
                    cleaned = clean_navigation(words, page.chars, width, height, profile)
                    body = cleaned.body_words
                    cand = candidate_identical_input(body, width, height)
                    secondary.append(
                        {
                            "ticker": ticker,
                            "pdf_file": pdf_file,
                            "page": row["page"],
                            "group": row["group"],
                            "body_word_count": len(body),
                            "navigation_item_count": len(cleaned.navigation_items),
                            "candidate_nav_stripped_ok": cand["ok"],
                            "candidate_nav_stripped_error": cand["error"],
                            "candidate_nav_stripped_status": cand["status"],
                            "candidate_nav_stripped_text": cand["text"] if cand["ok"] else "",
                            "matches_identical_input_candidate": (
                                cand["ok"]
                                and row["candidate_ok"]
                                and cand["text"] == row["candidate_text"]
                            ),
                        }
                    )
        except Exception as error:  # noqa: BLE001
            for row in rows:
                secondary.append(
                    {
                        "ticker": ticker,
                        "pdf_file": pdf_file,
                        "page": row["page"],
                        "group": row["group"],
                        "error": f"{type(error).__name__}: {error}",
                    }
                )
    return secondary


# ---------------------------------------------------------------------------
# Rendering and blinding
# ---------------------------------------------------------------------------


def slug(value: str) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")


def render_page_png(path: Path, page_number: int, out_path: Path, dpi: int = RENDER_DPI) -> dict:
    document = fitz.open(str(path))
    try:
        page = document.load_page(page_number - 1)
        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        pixmap = page.get_pixmap(matrix=matrix)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        pixmap.save(str(out_path))
        return {"dpi": dpi, "width_px": pixmap.width, "height_px": pixmap.height, "bytes": out_path.stat().st_size}
    finally:
        document.close()


def make_blinded_files(records: list[dict], images_dir: Path) -> tuple[list[dict], list[dict], list[dict]]:
    """Returns (queue, key, render_drops)."""

    rng = random.Random(SEED + 1)
    queue: list[dict] = []
    key: list[dict] = []
    drops: list[dict] = []

    for index, row in enumerate(records, 1):
        item_id = f"{row['group']}_{index:03d}"
        image_name = f"{row['group']}_{row['ticker']}_{slug(Path(row['pdf_file']).stem)}_p{row['page']}.png"
        image_path = images_dir / image_name
        path = RAW_ROOT / row["ticker"] / row["pdf_file"]
        try:
            render_info = render_page_png(path, row["page"], image_path)
        except Exception as error:  # noqa: BLE001
            drops.append(
                {
                    "ticker": row["ticker"],
                    "pdf_file": row["pdf_file"],
                    "page": row["page"],
                    "group": row["group"],
                    "reason": f"{type(error).__name__}: {error}",
                }
            )
            continue

        candidate_text = row["candidate_text"] if row["candidate_ok"] else (
            f"[reconstruct_by_regions raised: {row['candidate_error']}]"
        )
        mapping = rng.choice(["production", "candidate"])
        texts = {"production": row["production_text"], "candidate": candidate_text}
        other = "candidate" if mapping == "production" else "production"
        image_rel = str(image_path.relative_to(REPO_ROOT)).replace("\\", "/")

        queue.append(
            {
                "item_id": item_id,
                "set": row["group"],
                "image_path": image_rel,
                "parser_a": texts[mapping],
                "parser_b": texts[other],
            }
        )
        key.append(
            {
                "item_id": item_id,
                "ticker": row["ticker"],
                "pdf_file": row["pdf_file"],
                "page": row["page"],
                "set": row["group"],
                "parser_a": mapping,
                "parser_b": other,
                "production_branch": row["production_branch"],
                "use_region_pass": row["use_region_pass"],
                "reading_order_status": row["reading_order_status"],
                "region_order_status": row["region_order_status"],
                "table_block_count": row["table_block_count"],
                "structural_grid_risk": row["structural_grid_risk"],
                "mixed_width_header": row["mixed_width_header"],
                "production_text": row["production_text"],
                "candidate_ok": row["candidate_ok"],
                "candidate_status": row["candidate_status"],
                "candidate_text": candidate_text,
                "texts_differ": row["texts_differ"],
                "image_path": image_rel,
                "render": render_info,
            }
        )
    return queue, key, drops


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def branch_shares(records: list[dict]) -> dict[str, Any]:
    counts = Counter(row["production_branch"] for row in records)
    total = len(records)
    return {
        "total": total,
        "region_pass": counts.get("region_pass", 0),
        "column_order": counts.get("column_order", 0),
        "native": counts.get("native", 0),
        "region_pass_share": (counts.get("region_pass", 0) / total) if total else 0.0,
        "column_order_share": (counts.get("column_order", 0) / total) if total else 0.0,
        "native_share": (counts.get("native", 0) / total) if total else 0.0,
    }


def write_summary(
    all_records: list[dict],
    human_records: list[dict],
    diff_records: list[dict],
    secondary_records: list[dict],
    crash_log: list[dict],
    old_vs_new_note: str,
) -> str:
    overall = branch_shares(all_records)
    human_shares = branch_shares(human_records)
    diff_shares = branch_shares(diff_records)

    differing = [row for row in all_records if row["texts_differ"]]
    differing_human = [row for row in human_records if row["texts_differ"]]
    differing_diff = [row for row in diff_records if row["texts_differ"]]

    candidate_crashes = [row for row in all_records if not row["candidate_ok"]]

    secondary_ok = [row for row in secondary_records if "error" not in row and row.get("candidate_nav_stripped_ok")]
    secondary_matches = [row for row in secondary_ok if row["matches_identical_input_candidate"]]
    secondary_diffs = [row for row in secondary_ok if not row["matches_identical_input_candidate"]]

    lines: list[str] = []
    lines.append("# Production vs candidate reading-order comparison")
    lines.append("")
    lines.append(f"Date: {DATE} - Seed: {SEED}")
    lines.append("")
    lines.append(
        "**This supersedes `reports/parser_comparison_2026-07-30/` and "
        "`reports/old_vs_new_review_2026-07-30/`.** Both compared the candidate "
        "against `esg_reading_order.reconstruct_column_order` fed "
        "navigation-stripped pdfplumber words and called that 'production'. "
        "It is not: real production (`src/pdf_parser.py`) uses PyMuPDF words, "
        "unstripped, and runs a two-stage pipeline (column reader, then a "
        "region pass under `use_region_pass`). "
        + old_vs_new_note
    )
    lines.append("")

    lines.append("## 1. How often does production's region pass actually fire?")
    lines.append("")
    lines.append(
        "Nobody previously measured this. Across the full sample "
        f"({overall['total']} pages, both groups combined):"
    )
    lines.append("")
    lines.append("| Branch | Pages | Share |")
    lines.append("|---|---:|---:|")
    lines.append(f"| region_pass | {overall['region_pass']} | {overall['region_pass_share']:.1%} |")
    lines.append(f"| column_order | {overall['column_order']} | {overall['column_order_share']:.1%} |")
    lines.append(f"| native | {overall['native']} | {overall['native_share']:.1%} |")
    lines.append("")
    lines.append("Broken out per group:")
    lines.append("")
    lines.append("| Group | Pages | region_pass | column_order | native |")
    lines.append("|---|---:|---:|---:|---:|")
    lines.append(
        f"| human_validation | {human_shares['total']} | "
        f"{human_shares['region_pass']} ({human_shares['region_pass_share']:.1%}) | "
        f"{human_shares['column_order']} ({human_shares['column_order_share']:.1%}) | "
        f"{human_shares['native']} ({human_shares['native_share']:.1%}) |"
    )
    lines.append(
        f"| diff_sample | {diff_shares['total']} | "
        f"{diff_shares['region_pass']} ({diff_shares['region_pass_share']:.1%}) | "
        f"{diff_shares['column_order']} ({diff_shares['column_order_share']:.1%}) | "
        f"{diff_shares['native']} ({diff_shares['native_share']:.1%}) |"
    )
    lines.append("")

    lines.append("## 2. How many sampled pages does production actually differ from the candidate on?")
    lines.append("")
    lines.append(
        f"**{len(differing)} / {overall['total']}** sampled pages differ (identical-input "
        "comparison). By construction, diff_sample pages were selected because they "
        "differ, so that group's share is not informative on its own; the "
        "human_validation number is the useful one because it is a fixed, "
        "independently-chosen set."
    )
    lines.append("")
    lines.append(
        f"- human_validation: {len(differing_human)} / {len(human_records)} differ.\n"
        f"- diff_sample: {len(differing_diff)} / {len(diff_records)} differ "
        "(selected to differ, by design)."
    )
    lines.append("")

    lines.append("## 3. Was the candidate compatible with raw PyMuPDF words out of the box?")
    lines.append("")
    if not candidate_crashes:
        lines.append(
            f"**Yes.** `reconstruct_by_regions` was called on raw, unstripped PyMuPDF word "
            f"dicts (from `_pymupdf_words`) for all {overall['total']} sampled pages with zero "
            "exceptions. No shim was needed: the fields `reconstruct_by_regions` reads "
            "(`text`, `top`, `bottom`, `x0`, `x1`, `upright`) are all present in "
            "`_pymupdf_words`'s output schema, which happens to already match what "
            "`esg_reading_order`/`esg_reading_regions` expect."
        )
    else:
        lines.append(
            f"**No.** `reconstruct_by_regions` raised on {len(candidate_crashes)} of "
            f"{overall['total']} sampled pages when fed raw PyMuPDF words:"
        )
        lines.append("")
        for row in candidate_crashes:
            lines.append(
                f"- {row['ticker']} p{row['page']} ({row['pdf_file']}): {row['candidate_error']}"
            )
    if crash_log:
        lines.append("")
        lines.append(
            f"Additionally, {len(crash_log)} page(s) crashed the candidate during pool "
            "scanning (outside the final sample; recorded here for completeness):"
        )
        for row in crash_log:
            lines.append(
                f"- [{row['set']}] {row['ticker']} p{row['page']} ({row['pdf_file']}): {row['error']}"
            )
    lines.append("")

    lines.append("## 4. Does navigation-stripping change the picture materially?")
    lines.append("")
    lines.append(
        "Secondary, reference-only run: candidate fed navigation-stripped pdfplumber "
        "words (via `esg_navigation.clean_navigation`), NOT identical input to "
        "production. Kept out of the headline numbers above."
    )
    lines.append("")
    if secondary_ok:
        lines.append(
            f"- {len(secondary_matches)} / {len(secondary_ok)} sampled pages produced the "
            "SAME candidate text with and without navigation stripping.\n"
            f"- {len(secondary_diffs)} / {len(secondary_ok)} differed."
        )
        if len(secondary_ok):
            share = len(secondary_diffs) / len(secondary_ok)
            verdict = (
                "Navigation stripping materially changes the candidate's output on a "
                "meaningful share of pages; it is not a safe simplification to skip."
                if share >= 0.15
                else "Navigation stripping changes the candidate's output on a small "
                "share of pages; most of the difference in this comparison comes from "
                "the reader, not from stripping."
            )
            lines.append(f"- {verdict} ({share:.1%} differ.)")
    else:
        lines.append("No secondary-run pages produced comparable output (see results.json).")
    lines.append("")

    lines.append("## Sample composition")
    lines.append("")
    lines.append(
        f"- human_validation: {len(human_records)} pages (the 13 pages scored by a human in "
        "`reports/llm_reader_review_2026-07-30/`)."
    )
    docs = {(row["ticker"], row["pdf_file"]) for row in diff_records}
    lines.append(
        f"- diff_sample: {len(diff_records)} pages across {len(docs)} documents, seed {SEED}, "
        "reproducible page list in `sampled_pages.json`."
    )
    lines.append("")

    lines.append("## Files")
    lines.append("")
    lines.append("- `results.json` - per-page record for every sampled page, both groups.")
    lines.append("- `secondary_navigation_stripped.json` - the reference-only run from section 4.")
    lines.append("- `sampled_pages.json` - reproducible diff_sample page list.")
    lines.append("- `judging_queue.json` - blinded parser_a/parser_b texts + image paths only.")
    lines.append("- `blind_key.json` - full unmasked mapping and per-page branch/status fields.")
    lines.append("- `images/` - 110dpi PNG renders of every sampled page.")
    lines.append(
        "- `render_drops.json` - any pages excluded because their render failed."
    )
    lines.append("")
    lines.append(
        "No paid API calls were made. Judging queue and renders only; scoring happens "
        "separately (human or a separate chat-based model)."
    )
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    images_dir = OUT_DIR / "images"

    print("Building human_validation sample (13 fixed pages)...")
    human_records, human_crash_log = build_human_validation_sample()
    print(f"  {len(human_records)} pages, {len(human_crash_log)} candidate crashes")

    print("Building diff_sample (~40 pages where production and candidate differ)...")
    diff_records, diff_crash_log, sampled_pages_meta = build_diff_sample()
    print(f"  {len(diff_records)} pages across "
          f"{len({(r['ticker'], r['pdf_file']) for r in diff_records})} documents")

    all_records = human_records + diff_records
    crash_log = human_crash_log + diff_crash_log

    print("Running secondary navigation-stripped reference comparison...")
    secondary_records = build_nav_stripped_secondary(all_records)

    print("Rendering images and building blinded files...")
    queue, key, render_drops = make_blinded_files(all_records, images_dir)

    old_vs_new_note = (
        "`reports/old_vs_new_review_2026-07-30/` (the r01-r03/g01-g19 renders and "
        "`recovered_scores_unblinded.json`) is the same voided baseline: its `r*` pages "
        "are exactly the 'production refuses' void group and its `g*` pages compare the "
        "candidate against the column reader alone, not real production. "
        "`reports/parser_comparison_2026-07-30/` is `column_order`/`regions`/table-variant "
        "output from navigation-stripped pdfplumber words, the same wrong baseline. "
        "This report is the first to call production's real two-stage path "
        "(`reconstruct_column_order` then, conditionally, `pdf_parser.reconstruct_region_order`) "
        "against raw, unstripped PyMuPDF words."
    )

    summary_text = write_summary(
        all_records, human_records, diff_records, secondary_records, crash_log, old_vs_new_note
    )
    (OUT_DIR / "summary.md").write_text(summary_text, encoding="utf-8")

    (OUT_DIR / "results.json").write_text(
        json.dumps(
            [{k: v for k, v in row.items()} for row in all_records],
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (OUT_DIR / "secondary_navigation_stripped.json").write_text(
        json.dumps(secondary_records, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (OUT_DIR / "sampled_pages.json").write_text(
        json.dumps(sampled_pages_meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (OUT_DIR / "judging_queue.json").write_text(
        json.dumps(queue, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (OUT_DIR / "blind_key.json").write_text(
        json.dumps(key, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (OUT_DIR / "render_drops.json").write_text(
        json.dumps(render_drops, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (OUT_DIR / "candidate_crash_log.json").write_text(
        json.dumps(crash_log, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print(f"\nWrote report to {OUT_DIR}")
    print(f"Total sampled pages: {len(all_records)} "
          f"(human_validation={len(human_records)}, diff_sample={len(diff_records)})")


if __name__ == "__main__":
    main()
