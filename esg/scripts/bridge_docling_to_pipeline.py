"""Write fused docling+PyMuPDF pages in the layout the ESG pipeline consumes.

Produces ``<out>/<TICKER>/<STEM>.txt`` and ``<STEM>.pages.csv``, matching the
shape of ``data/02_interim/esg_text/``. This is the bridge between the parsing
spike and everything downstream: sectioning, chunking, QA.

It writes to its own directory and NEVER to the production tree.
``section_splitter_esg`` takes ``--input``, so it can be pointed here without
disturbing the parser output the rest of the pipeline depends on.

Usage::

    venv-docling\\Scripts\\python.exe esg\\scripts\\bridge_docling_to_pipeline.py \\
        --work-dir outputs\\docling_fullrun --out outputs\\docling_fullrun\\pipeline_input

Then run sectioning against it::

    venv\\Scripts\\python.exe esg\\src\\section_splitter_esg.py \\
        --input outputs\\docling_fullrun\\pipeline_input \\
        --out outputs\\docling_fullrun\\sections
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# The fuse stage prefixes each block with "[3:section_header]" so a human can
# match text to a numbered box in the overlay images. That is a debugging aid,
# not part of the document, and must not reach sectioning.
REGION_TAG_RE = re.compile(r"^\[\d+:[^\]]*\][ \t]*\n?", re.M)

# The fuse stage ends a page with "[unplaced words]" followed by any words that
# landed in no region -- roughly 3.4% of a page, mostly nav ribbons and page
# numbers that docling deliberately does not box. The marker itself is NOT
# matched by REGION_TAG_RE (no digits, no colon), so before this it survived
# into the document text: 64 occurrences in one Best Buy file, reaching 206
# section files.
UNPLACED_MARKER = "[unplaced words]"

# The fuse stage writes this for a region with no words under it -- a picture,
# or a cell whose text is vector art. It is a note to a human reading the fused
# file, not document content, and leaked 875 times into the v2 corpus and 176
# section files before being caught. Same class of bug as UNPLACED_MARKER.
EMPTY_REGION_NOTE = "(no text layer in this region)"

# A block header looks like "[6:text|band=footer]". The band suffix marks a
# region sitting in the top or bottom 12% of the page. It is a hint, not a
# verdict: on ORLY p12 the line "2,013 LEADERSHIP AWARDS EARNED IN 2023." sits
# in the band and is real content.
BLOCK_HEADER_RE = re.compile(r"^\[(\d+):([^\]|]*)(?:\|band=(header|footer))?\][ \t]*$", re.M)

# section_splitter_esg.read_page_map only reads page, char_start and char_end.
# The remaining columns exist because layout QA and the vector manifest consume
# them. They are filled with values describing what this actually is, rather
# than copied from the production parser, so no downstream stage can mistake
# fused output for pdfplumber output.
PAGES_CSV_COLUMNS = [
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
    "picture_region_count",
    "empty_region_count",
    "band_region_count",
    "band_dropped_count",
    "unplaced_char_count",
    "text_source",
    "table_candidate_count",
]


def split_page_blocks(raw: str) -> tuple[list[tuple[str, str]], str]:
    """Split a fused page into (kind, text) blocks plus its unplaced tail.

    ``kind`` is "body" or "band". The tail is whatever followed the
    ``[unplaced words]`` marker.
    """
    tail = ""
    if UNPLACED_MARKER in raw:
        raw, _, tail = raw.partition(UNPLACED_MARKER)

    blocks: list[tuple[str, str]] = []
    matches = list(BLOCK_HEADER_RE.finditer(raw))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        text = raw[start:end].strip()
        if text == EMPTY_REGION_NOTE:
            continue
        text = text.replace(EMPTY_REGION_NOTE, "").strip()
        if text:
            blocks.append(("band" if m.group(3) else "body", text))
    if not matches:
        stripped = raw.strip()
        if stripped:
            blocks.append(("body", stripped))
    return blocks, tail.strip()


def _furniture_key(text: str) -> str:
    """Normalise a block for repetition comparison.

    Page numbers change from page to page, so digits are dropped: a footer
    reading "Sustainability Report 14" and "Sustainability Report 15" is the
    same ribbon.
    """
    return re.sub(r"[^a-z]+", " ", text.lower()).strip()


def repeated_band_keys(
    pages: list[int], stem: str, fused_dir: Path, min_pages: int
) -> set[str]:
    """Band blocks whose text appears on at least ``min_pages`` pages.

    Position alone does not make a block furniture -- on ORLY p12 the line
    "2,013 LEADERSHIP AWARDS EARNED IN 2023." sits in the bottom band and is
    real content. Repetition alone does not either: a section heading can
    legitimately recur. Requiring BOTH is what separates a nav ribbon from a
    sentence that happens to sit low on the page.
    """
    seen: dict[str, set[int]] = {}
    for page_no in pages:
        fused = fused_dir / f"{stem}_p{page_no}.txt"
        if not fused.exists():
            continue
        blocks, _ = split_page_blocks(fused.read_text(encoding="utf-8"))
        for kind, text in blocks:
            if kind != "band":
                continue
            key = _furniture_key(text)
            if key:
                seen.setdefault(key, set()).add(page_no)
    return {k for k, v in seen.items() if len(v) >= min_pages}


def build_document(
    cached: dict[str, Any],
    fused_dir: Path,
    keep_band: bool = True,
    keep_unplaced: bool = True,
    drop_repeated_band: int = 0,
) -> tuple[str, list[dict[str, Any]], int]:
    """Concatenate a document's fused pages, tracking character offsets."""
    stem = cached["pdf_stem"]
    pages = sorted(int(k) for k in cached.get("pages", {}))
    chunks: list[str] = []
    rows: list[dict[str, Any]] = []
    offset = 0
    missing = 0

    # Needs the whole document: a single page cannot tell a running footer
    # from a line that happens to sit low.
    repeated = (
        repeated_band_keys(pages, stem, fused_dir, drop_repeated_band)
        if drop_repeated_band
        else set()
    )

    for page_no in pages:
        fused = fused_dir / f"{stem}_p{page_no}.txt"
        if fused.exists():
            raw = fused.read_text(encoding="utf-8")
        else:
            missing += 1
            raw = ""

        # Counted before filtering: a picture with no words under it leaves no
        # trace in the text, and a figure silently absent is worse than one
        # recorded as unreadable. Kept OUT of the text on purpose -- a marker
        # repeated on 15% of regions would be high-frequency noise in every
        # embedding it landed in.
        n_empty_regions = raw.count(EMPTY_REGION_NOTE)
        items = cached["pages"].get(str(page_no), [])
        blocks, unplaced = split_page_blocks(raw)
        n_dropped = 0
        if repeated:
            kept_blocks = []
            for kind, text in blocks:
                if kind == "band" and _furniture_key(text) in repeated:
                    n_dropped += 1
                    continue
                kept_blocks.append((kind, text))
            blocks = kept_blocks
        kept = [t for kind, t in blocks if kind == "body" or keep_band]
        n_band = sum(1 for kind, _ in blocks if kind == "band")

        # Unplaced words go LAST, which is where page furniture naturally sits
        # and where section_splitter's ribbon detector looks for it: that rule
        # drops a repeated heading seen on two pages when both copies are in
        # the bottom band. Dropping them here instead would also discard real
        # body text -- MHK-MOHAWK 2024 p15 has 57 unplaced words out of 413.
        if unplaced and keep_unplaced:
            kept.append(unplaced)

        body = "\n\n".join(kept).strip()
        # A blank line between pages so a heading at a page top is not glued to
        # the previous page's final sentence.
        block = body + "\n\n"

        rows.append(
            {
                "page": page_no,
                "char_start": offset,
                "char_end": offset + len(block),
                "char_count": len(block),
                "extracted_char_count": len(body),
                "emitted": "true" if body else "false",
                "page_type": "text" if body else "empty",
                "parse_status": "ok" if body else "no_text",
                "reading_order_status": "docling_regions",
                "layout_risk": "false",
                "visual_review_status": "not_required",
                "repair_method": "none",
                "picture_region_count": sum(1 for i in items if i.get("label") == "picture"),
                "empty_region_count": n_empty_regions,
                "band_region_count": n_band,
                "band_dropped_count": n_dropped,
                "unplaced_char_count": len(unplaced),
                "text_source": "docling_fusion",
                "table_candidate_count": sum(1 for i in items if i.get("grid")),
            }
        )
        chunks.append(block)
        offset += len(block)

    return "".join(chunks), rows, missing


def write_parse_index(
    source_index: Path, out_index: Path, built: dict[str, dict[str, Any]]
) -> int:
    """Derive a v2 parse index from the production one.

    Identity columns -- logical_source_id, source_version_id, file_alias_id,
    extraction_artifact_id, and the source hashes -- describe the SOURCE PDF,
    not the parser, so they are carried across unchanged. Regenerating them
    would mint new IDs for the same document and break lineage.

    Only the extraction columns are rewritten: where the text lives, which
    parser produced it, and the counts that follow from it.
    """
    if not source_index.exists():
        print(f"no production parse index at {source_index}", file=sys.stderr)
        return 0

    with source_index.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        rows = [r for r in reader if Path(r.get("pdf_file", "")).stem in built]

    for row in rows:
        info = built[Path(row["pdf_file"]).stem]
        row["parsed_text_file"] = info["txt"]
        row["page_map_file"] = info["csv"]
        row["parser_used"] = "docling_fusion"
        row["parser_policy"] = "docling_regions_pymupdf_words_v1"
        row["parser_reason"] = "docling layout regions, PyMuPDF word text"
        row["page_count"] = info["pages"]
        row["char_count"] = info["chars"]
        row["parsed_at"] = info["parsed_at"]
        # Deliberately cleared: these were measured on the pdfplumber output
        # and say nothing about this one. Leaving them would let downstream
        # gates act on stale evidence.
        for stale in (
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
            "quality_flags",
            "content_hash",
        ):
            if stale in row:
                row[stale] = ""

    out_index.parent.mkdir(parents=True, exist_ok=True)
    with out_index.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--work-dir", type=Path, default=Path("outputs/docling_fullrun"))
    parser.add_argument("--out", type=Path, default=None, help="default <work-dir>/pipeline_input")
    parser.add_argument("--drop-repeated-band", type=int, default=2, metavar="N",
                        help="drop header/footer-band blocks whose text repeats on N or more "
                             "pages (0 disables). Position alone is not enough -- band text "
                             "appearing once is usually real content.")
    parser.add_argument("--drop-band", action="store_true",
                        help="drop regions in the header/footer band instead of letting the "
                             "sectioner's ribbon detector judge them")
    parser.add_argument("--drop-unplaced", action="store_true",
                        help="drop words that landed in no region (~3%% of a page; mostly nav "
                             "ribbons, but sometimes real body text)")
    parser.add_argument("--parse-index-in", type=Path,
                        default=Path("data/00_reference/esg_parse_index.csv"))
    parser.add_argument("--parse-index-out", type=Path, default=None,
                        help="write a v2 parse index derived from the production one")
    args = parser.parse_args(argv)

    cache_dir = args.work_dir / "docling_json"
    fused_dir = args.work_dir / "fused"
    out_dir = args.out or (args.work_dir / "pipeline_input")

    caches = sorted(cache_dir.glob("*.pages.json"))
    if not caches:
        print(f"no docling cache under {cache_dir}; run convert and fuse first", file=sys.stderr)
        return 1
    if not fused_dir.exists():
        print(f"no fused text under {fused_dir}; run the fuse stage first", file=sys.stderr)
        return 1

    written = 0
    total_missing = 0
    built: dict[str, dict[str, Any]] = {}
    for cache_path in caches:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        stem = cached["pdf_stem"]
        ticker = stem.split("-", 1)[0].strip() or "UNKNOWN"

        text, rows, missing = build_document(
            cached,
            fused_dir,
            keep_band=not args.drop_band,
            keep_unplaced=not args.drop_unplaced,
            drop_repeated_band=args.drop_repeated_band,
        )
        if not rows:
            continue

        target = out_dir / ticker
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{stem}.txt").write_text(text, encoding="utf-8")
        with (target / f"{stem}.pages.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=PAGES_CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)

        built[stem] = {
            "txt": str((target / f"{stem}.txt").as_posix()),
            "csv": str((target / f"{stem}.pages.csv").as_posix()),
            "pages": len(rows),
            "chars": len(text),
            "parsed_at": datetime.now(timezone.utc).isoformat(),
        }
        written += 1
        total_missing += missing
        note = f"  ({missing} page(s) had no fused text)" if missing else ""
        print(f"{ticker}/{stem}: {len(rows)} pages, {len(text)} chars{note}")

    print()
    print(f"{written} document(s) -> {out_dir}")

    if args.parse_index_out:
        n = write_parse_index(args.parse_index_in, args.parse_index_out, built)
        print(f"parse index: {n} row(s) -> {args.parse_index_out}")
        if n < written:
            print(
                f"WARNING: {written - n} document(s) had no row in the production "
                f"parse index and are absent from the v2 index",
                file=sys.stderr,
            )
    if total_missing:
        print(f"WARNING: {total_missing} page(s) across all documents had no fused text", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
