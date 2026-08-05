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
import hashlib
import json
import re
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
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
# The trailing (.*) matters: the fuse stage writes an empty region as
# "[19:picture] (no text layer in this region)" -- tag and note on ONE line.
# Anchoring the tag to end-of-line missed those, so the note and every later
# tag were swallowed into the preceding block's text, producing heading text
# like 'Whistleblower Policy\n\n[6:picture]'.
BLOCK_HEADER_RE = re.compile(
    r"^\[(\d+):([^\]|]*)(?:\|band=(header|footer))?\][ \t]*(.*)$", re.M
)

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
    # What the page IS, as opposed to whether it parsed: content, toc, divider,
    # cover or blank. The chunker drops chunks whose pages are all furniture.
    "page_role",
    "parse_status",
    "reading_order_status",
    "layout_risk",
    "visual_review_status",
    "repair_method",
    "picture_region_count",
    "empty_region_count",
    "band_region_count",
    "band_dropped_count",
    # Characters removed from this page's unplaced tail as repeated furniture.
    # Recorded per page so an unexpectedly large removal can be traced to the
    # document and line responsible rather than inferred from a total.
    "unplaced_dropped_count",
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
        # m.end() now sits after any same-line trailing text, so start from
        # the captured remainder instead of dropping it.
        trailing = (m.group(4) or "").strip()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        rest = raw[m.end():end].strip()
        text = (trailing + ("\n" + rest if rest else "")).strip()
        if text == EMPTY_REGION_NOTE:
            continue
        text = text.replace(EMPTY_REGION_NOTE, "").strip()
        if text:
            kind = "band" if m.group(3) else "body"
            blocks.append((kind, text, (m.group(2) or "").strip()))
    if not matches:
        stripped = raw.strip()
        if stripped:
            blocks.append(("body", stripped, ""))
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
        for kind, text, _label in blocks:
            if kind != "band":
                continue
            key = _furniture_key(text)
            if key:
                seen.setdefault(key, set()).add(page_no)
    return {k for k, v in seen.items() if len(v) >= min_pages}


def repeated_unplaced_keys(
    pages: list[int], stem: str, fused_dir: Path, min_pages: int
) -> set[str]:
    """Unplaced lines whose text appears on at least ``min_pages`` pages."""
    seen: dict[str, set[int]] = {}
    for page_no in pages:
        fused = fused_dir / f"{stem}_p{page_no}.txt"
        if not fused.exists():
            continue
        _, unplaced = split_page_blocks(fused.read_text(encoding="utf-8"))
        for line in unplaced.splitlines():
            key = _furniture_key(line)
            if key:
                seen.setdefault(key, set()).add(page_no)
    return {k for k, v in seen.items() if len(v) >= min_pages}


HEADINGS_CSV_COLUMNS = ["char_start", "char_end", "page", "label", "level", "text"]

# The fuse stage prefixes a section_header block with "## " so it reads as
# markdown. That prefix ends up inside section titles ('## ABOUT THIS REPORT')
# and inside 80% of chunk texts, where it is a meaningless repeated token.
MD_HEADING_PREFIX = re.compile(r"^#{1,6}\s+")


# A contents entry: '04 Message from our CEO' or 'Introduction 04'.
TOC_LINE_RE = re.compile(r"^\s*(?:\d{1,3}\s+\S.*|\S.*\s+\d{1,3})\s*$")
WORD_RE_ROLE = re.compile(r"[A-Za-z]{2,}")
NUM_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)?%?")


def _is_toc_line(line: str) -> bool:
    """One title beside one page number, and nothing else on the line.

    The single-number requirement is what separates a contents entry from a
    table row. 'Bangladesh 115 122 74 74 234 234' also ends in a number.
    """
    if not TOC_LINE_RE.match(line) or not WORD_RE_ROLE.search(line):
        return False
    numbers = NUM_TOKEN_RE.findall(line)
    return len(numbers) == 1 and numbers[0].isdigit() and int(numbers[0]) <= 400


def classify_page_role(text: str, page_no: int, picture_count: int) -> str:
    """content / toc / divider / cover / blank for one page of fused text."""
    stripped = text.strip()
    if not stripped:
        return "blank"
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    words = WORD_RE_ROLE.findall(stripped)

    # Several complete sentences mean there is prose worth keeping, whatever
    # else shares the page.
    prose_lines = sum(
        1 for line in lines if len(WORD_RE_ROLE.findall(line)) >= 15 and line.rstrip().endswith(".")
    )
    if prose_lines >= 5:
        return "content"

    # Rows carrying several numbers each are a data table, not a contents list.
    is_data_table = sum(1 for line in lines if len(NUM_TOKEN_RE.findall(line)) >= 3) >= 3

    if not is_data_table:
        toc_lines = sum(1 for line in lines if _is_toc_line(line))
        if len(lines) >= 6 and toc_lines >= 6 and toc_lines / len(lines) >= 0.55:
            return "toc"
        if (
            re.search(r"\b(table of contents|contents)\b", stripped[:400], re.IGNORECASE)
            and toc_lines >= 4
            and toc_lines / len(lines) >= 0.40
        ):
            return "toc"

        # A contents page need not print page numbers. URBN lists only section
        # names, Valvoline sets each twice; both read as prose-free lists of
        # titles. The explicit 'contents' heading is required here, because
        # without the page numbers there is nothing else to distinguish such a
        # page from a legitimate page of short headings.
        if (
            prose_lines == 0
            and len(lines) >= 6
            and re.search(r"\bcontents\b", stripped[:600], re.IGNORECASE)
            and sum(1 for line in lines if len(WORD_RE_ROLE.findall(line)) <= 10)
            / len(lines)
            >= 0.80
        ):
            return "toc"

    # A part-title page carries a handful of words, and the design frequently
    # sets them twice, so an immediately repeated word is a strong hint.
    if len(words) <= 60:
        lowered = [w.lower() for w in words]
        repeats = sum(1 for a, b in zip(lowered, lowered[1:]) if a == b)
        if page_no == 1 and len(words) <= 40:
            return "cover"
        if repeats >= 3 or (picture_count >= 1 and len(words) <= 25):
            return "divider"

    return "content"


def build_document(
    cached: dict[str, Any],
    fused_dir: Path,
    keep_band: bool = True,
    keep_unplaced: bool = True,
    drop_repeated_band: int = 0,
    drop_repeated_unplaced: int = 0,
    strip_md_prefix: bool = True,
) -> tuple[str, list[dict[str, Any]], int, list[dict[str, Any]]]:
    """Concatenate a document's fused pages, tracking character offsets."""
    stem = cached["pdf_stem"]
    pages = sorted(int(k) for k in cached.get("pages", {}))
    chunks: list[str] = []
    rows: list[dict[str, Any]] = []
    headings: list[dict[str, Any]] = []
    offset = 0
    missing = 0

    # Needs the whole document: a single page cannot tell a running footer
    # from a line that happens to sit low.
    repeated = (
        repeated_band_keys(pages, stem, fused_dir, drop_repeated_band)
        if drop_repeated_band
        else set()
    )
    repeated_unplaced = (
        repeated_unplaced_keys(pages, stem, fused_dir, drop_repeated_unplaced)
        if drop_repeated_unplaced
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
            for kind, text, label in blocks:
                if kind == "band" and _furniture_key(text) in repeated:
                    n_dropped += 1
                    continue
                kept_blocks.append((kind, text, label))
            blocks = kept_blocks

        kept: list[str] = []
        page_headings: list[tuple[int, str, str]] = []
        for kind, text, label in blocks:
            if kind == "band" and not keep_band:
                continue
            if strip_md_prefix and label in ("section_header", "title"):
                text = MD_HEADING_PREFIX.sub("", text)
            if label in ("section_header", "title"):
                # Offset within the page block, resolved to document offset
                # once the page's own start is known.
                page_headings.append((sum(len(x) + 2 for x in kept), label, text))
            kept.append(text)
        n_band = sum(1 for kind, _, _ in blocks if kind == "band")

        # Unplaced words go last. Drop only lines repeated across enough pages;
        # keep all other lines because this tail can contain real body text.
        unplaced_dropped = 0
        if unplaced and repeated_unplaced:
            before = len(unplaced)
            unplaced = "\n".join(
                line
                for line in unplaced.splitlines()
                if _furniture_key(line) not in repeated_unplaced
            ).strip()
            unplaced_dropped = before - len(unplaced)
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
                "page_role": classify_page_role(
                    body,
                    page_no,
                    sum(1 for i in items if i.get("label") == "picture"),
                ),
                "parse_status": "ok" if body else "no_text",
                "reading_order_status": "docling_regions",
                "layout_risk": "false",
                "visual_review_status": "not_required",
                "repair_method": "none",
                "picture_region_count": sum(1 for i in items if i.get("label") == "picture"),
                "empty_region_count": n_empty_regions,
                "band_region_count": n_band,
                "band_dropped_count": n_dropped,
                "unplaced_dropped_count": unplaced_dropped,
                "unplaced_char_count": len(unplaced),
                "text_source": "docling_fusion",
                "table_candidate_count": sum(1 for i in items if i.get("grid")),
            }
        )
        for rel, label, htext in page_headings:
            headings.append(
                {
                    "char_start": offset + rel,
                    "char_end": offset + rel + len(htext),
                    "page": page_no,
                    "label": label,
                    "level": "",
                    "text": htext,
                }
            )

        chunks.append(block)
        offset += len(block)

    return "".join(chunks), rows, missing, headings


# Thresholds read off the production parse index rather than invented: the two
# rows it flags low_readable_word_ratio sit at 0.41 and 0.42, its p10 is 0.71,
# and its lowest chars_per_page is 459.
# A document whose words mostly landed in no region has no usable reading
# order, whatever its text looks like. Seen at 98% on a cache whose convert
# produced picture regions and nothing else, so every word fell through to the
# unplaced path while the run still reported success. The same PDF converts
# cleanly at 8% through the normal path -- which is the point: the failure is
# not predictable from the document, only observable in the output.
MAX_UNPLACED_SHARE = 0.25
MIN_READABLE_WORD_RATIO = 0.50
MIN_CHARS_PER_PAGE = 300

WORD_RE = re.compile(r"[^\s]+")
READABLE_RE = re.compile(r"^[A-Za-z][A-Za-z'\u2019-]{1,}$")
CID_RE = re.compile(r"\(cid:\d+\)")


def measure_text_quality(
    text: str, page_count: int, unplaced_chars: int = 0
) -> dict[str, Any]:
    """Quality signals for a synthesised parse-index row.

    Measured, not assumed. A document that parses badly should reach the
    chunker as needs_review rather than being waved through because it had no
    production row to inherit a verdict from.
    """
    # Markdown table structure is not vocabulary. --table-mode grid emits a '|'
    # between every pair of cells and a '---' rule under each header, and those
    # tokens land in the denominator: AEO-2024 is 10 table-heavy pages where 408
    # of 1,828 tokens are pipes, which dragged its ratio to 0.4584 and flagged a
    # document whose prose is fine. Excluding structure puts it at 0.6033.
    # This check exists to catch garbled or mis-decoded text, so it must not
    # punish formatting the pipeline itself chose to add.
    words = [
        w
        for w in WORD_RE.findall(text)
        if w != "|" and set(w) != {"-"}
    ]
    readable = sum(1 for w in words if READABLE_RE.match(w))
    ratio = readable / len(words) if words else 0.0
    per_page = len(text) / page_count if page_count else 0.0
    garbled = len(CID_RE.findall(text))

    # The unplaced words are already inside `text` -- the bridge keeps them
    # rather than dropping real content -- so the denominator is the document
    # itself, not document plus unplaced.
    unplaced_share = unplaced_chars / len(text) if text else 0.0

    flags = []
    if unplaced_share >= MAX_UNPLACED_SHARE:
        flags.append("high_unplaced_text")
    if ratio < MIN_READABLE_WORD_RATIO:
        flags.append("low_readable_word_ratio")
    if per_page < MIN_CHARS_PER_PAGE:
        flags.append("low_text_per_page")
    if garbled:
        flags.append("garbled_text")

    return {
        "readable_word_count": readable,
        "readable_word_ratio": round(ratio, 4),
        "chars_per_page": round(per_page, 1),
        "garbled_char_count": garbled,
        "unplaced_share": round(unplaced_share, 4),
        "quality_flags": "|".join(flags),
    }


def synthesise_parse_row(
    fieldnames: list[str], stem: str, ticker: str, info: dict[str, Any], raw_dir: Path
) -> dict[str, Any]:
    """A parse-index row for a document production never parsed."""
    row = {name: "" for name in fieldnames}
    matches = list(raw_dir.rglob(f"{stem}.pdf"))
    pdf = matches[0] if matches else None

    row.update(
        {
            "ticker": ticker,
            "pdf_file": f"{stem}.pdf",
            "source_pdf": str(pdf.as_posix()) if pdf else "",
            "parse_source_kind": "raw",
            "parse_source_pdf": str(pdf.as_posix()) if pdf else "",
            "parsed_text_file": info["txt"],
            "page_map_file": info["csv"],
            "status": "parsed",
            "parser_used": "docling_fusion",
            "parser_policy": "docling_regions_pymupdf_words_v1",
            "parser_reason": "synthesised: no production parse-index row",
            "page_count": info["pages"],
            "char_count": info["chars"],
            "parsed_at": info["parsed_at"],
            "possible_wrong_doc_type": "false",
            "ocr_approval_status": "not_applicable",
        }
    )
    if pdf and pdf.exists():
        data = pdf.read_bytes()
        stat = pdf.stat()
        row["source_sha256"] = hashlib.sha256(data).hexdigest()
        row["parse_source_sha256"] = row["source_sha256"]
        row["source_size_bytes"] = stat.st_size
        row["parse_source_size_bytes"] = stat.st_size
    row.update({k: v for k, v in info.get("quality", {}).items() if k in row})
    # Same fused-input fingerprint the production-derived rows carry. Without
    # it every synthesised document rebuilds on every run, and most of this
    # corpus is synthesised.
    if "content_hash" in row:
        row["content_hash"] = info.get("content_hash", "")
    return row


def ticker_map(source_index: Path) -> dict[str, str]:
    """stem -> ticker, straight from the production parse index.

    The file stem is not a reliable source for this. Deriving the ticker from
    the text before the first hyphen assumes a TICKER-COMPANY-YEAR layout that
    real filenames do not always follow: a company name can contain a hyphen,
    and some files put the ticker last.
    """
    mapping: dict[str, str] = {}
    if not source_index.exists():
        return mapping
    with source_index.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            stem = Path(row.get("pdf_file") or "").stem
            ticker = (row.get("ticker") or "").strip()
            if stem and ticker:
                mapping[stem] = ticker
    return mapping


def write_parse_index(
    source_index: Path,
    out_index: Path,
    built: dict[str, dict[str, Any]],
    raw_dir: Path | None = None,
) -> tuple[int, int]:
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
        # Quality is measured on THIS text, not inherited. The production row
        # describes a pdfplumber parse of the same PDF; these columns must
        # describe the fusion output, or a document that fused badly would be
        # waved through on the strength of an unrelated parse.
        for key, value in info.get("quality", {}).items():
            if key in row:
                row[key] = value

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
        ):
            if stale in row:
                row[stale] = ""

        # Repurposed, deliberately. The production pipeline stored a hash of
        # its pdfplumber text here, which says nothing about this parse; the
        # column was previously cleared for that reason. It now carries the
        # fingerprint of the fused pages this row was built from, which is
        # what a later resume needs to tell rebuilt text from stale text.
        if "content_hash" in row:
            row["content_hash"] = info.get("content_hash", "")

    # Documents production has never parsed get a synthesised row instead of
    # being dropped. Without this the v2 corpus silently loses every document
    # outside the production index -- 484 of 686 on disk.
    reused = {Path(r["pdf_file"]).stem for r in rows}
    synthesised = 0
    if raw_dir is not None:
        for stem, info in built.items():
            if stem in reused:
                continue
            ticker = info.get("ticker") or stem.split("-", 1)[0].strip() or "UNKNOWN"
            rows.append(synthesise_parse_row(fieldnames, stem, ticker, info, raw_dir))
            synthesised += 1

    out_index.parent.mkdir(parents=True, exist_ok=True)
    with out_index.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows), synthesised


def _nonempty_file(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def fused_fingerprint(fused_dir: Path, stem: str, pages: list[int]) -> str:
    """Hash the fused pages a document is built from.

    Resume has to answer "is this output still derived from these inputs?",
    and existence cannot: re-fusing rewrites pages in place, leaving the
    bridge output complete, non-empty and stale. Page numbers are hashed
    alongside the bytes so a page appearing or vanishing also shows up.
    """
    digest = hashlib.sha256()
    for page_no in pages:
        fused = fused_dir / f"{stem}_p{page_no}.txt"
        digest.update(f"{page_no}:".encode())
        if fused.is_file():
            digest.update(fused.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def load_completed_bridge_records(index_path: Path | None) -> dict[str, dict[str, Any]]:
    """Recover reusable bridge metadata only when all output sidecars exist."""
    if index_path is None or not index_path.is_file():
        return {}
    completed: dict[str, dict[str, Any]] = {}
    with index_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            stem = Path(row.get("pdf_file") or "").stem
            txt = Path(row.get("parsed_text_file") or "")
            pages = Path(row.get("page_map_file") or "")
            headings = txt.with_name(f"{stem}.headings.csv") if stem and txt.name else Path()
            if not stem or not (_nonempty_file(txt) and _nonempty_file(pages) and _nonempty_file(headings)):
                continue
            try:
                page_count = int(row.get("page_count") or 0)
                char_count = int(row.get("char_count") or 0)
            except ValueError:
                continue
            completed[stem] = {
                "txt": str(txt.as_posix()),
                "csv": str(pages.as_posix()),
                "pages": page_count,
                "chars": char_count,
                "ticker": (row.get("ticker") or "").strip(),
                "parsed_at": row.get("parsed_at") or "",
                # Freshness of the fused input, checked in the worker. Rows
                # written before this column was populated hold "", which no
                # fingerprint matches, so they rebuild once and then resume.
                "content_hash": (row.get("content_hash") or "").strip(),
                "quality": row,
            }
    return completed


@dataclass(frozen=True)
class BridgeTask:
    """One cache document that can be bridged without sharing output files."""

    cache_path: Path
    fused_dir: Path
    out_dir: Path
    ticker: str
    keep_band: bool
    keep_unplaced: bool
    drop_repeated_band: int
    drop_repeated_unplaced: int
    strip_md_prefix: bool
    resume_info: dict[str, Any] | None = None


def _run_bridge_task(task: BridgeTask) -> dict:
    """Build one document in a worker; the parent owns the parse-index write."""
    cached = json.loads(task.cache_path.read_text(encoding="utf-8"))
    stem = cached["pdf_stem"]
    fingerprint = fused_fingerprint(
        task.fused_dir, stem, sorted(int(k) for k in cached.get("pages", {}))
    )
    # Reuse needs the output to exist (checked by the parent) AND to still
    # derive from these fused pages. A mismatch falls through and rebuilds
    # rather than failing: stale text should cost time, not a run.
    if task.resume_info is not None and task.resume_info.get("content_hash") == fingerprint:
        return {
            "status": "reused",
            "stem": stem,
            "ticker": task.ticker,
            "pages": task.resume_info["pages"],
            "chars": task.resume_info["chars"],
            "missing": 0,
            "dropped": 0,
            "before": task.resume_info["chars"],
            "built": task.resume_info,
        }
    text, rows, missing, headings = build_document(
        cached,
        task.fused_dir,
        keep_band=task.keep_band,
        keep_unplaced=task.keep_unplaced,
        drop_repeated_band=task.drop_repeated_band,
        drop_repeated_unplaced=task.drop_repeated_unplaced,
        strip_md_prefix=task.strip_md_prefix,
    )
    if not rows:
        return {"status": "skipped", "stem": stem, "ticker": task.ticker}

    target = task.out_dir / task.ticker
    target.mkdir(parents=True, exist_ok=True)
    txt_path = target / f"{stem}.txt"
    pages_path = target / f"{stem}.pages.csv"
    headings_path = target / f"{stem}.headings.csv"
    txt_path.write_text(text, encoding="utf-8")
    with headings_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADINGS_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(headings)
    with pages_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=PAGES_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    dropped = sum(int(row.get("unplaced_dropped_count") or 0) for row in rows)
    return {
        "status": "written",
        "stem": stem,
        "ticker": task.ticker,
        "pages": len(rows),
        "chars": len(text),
        "missing": missing,
        "dropped": dropped,
        "before": len(text) + dropped,
        "built": {
            "txt": str(txt_path.as_posix()),
            "csv": str(pages_path.as_posix()),
            "pages": len(rows),
            "chars": len(text),
            "ticker": task.ticker,
            "parsed_at": datetime.now(timezone.utc).isoformat(),
            "content_hash": fingerprint,
            "quality": measure_text_quality(
                text, len(rows), sum(row["unplaced_char_count"] for row in rows)
            ),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--work-dir", type=Path, default=Path("outputs/docling_fullrun"))
    parser.add_argument("--layout-dir", type=Path,
                        help="Docling JSON cache (default: <work-dir>/docling_json)")
    parser.add_argument("--fused-dir", type=Path,
                        help="fused page text (default: <work-dir>/fused)")
    parser.add_argument("--workers", type=int, default=1, metavar="N",
                        help="bridge independent documents in N processes; the parent writes the parse index")
    resume_group = parser.add_mutually_exclusive_group()
    resume_group.add_argument("--resume", dest="resume", action="store_true",
                              help="reuse complete bridge documents (default)")
    resume_group.add_argument("--no-resume", dest="resume", action="store_false",
                              help="rebuild every cached document")
    parser.set_defaults(resume=True)
    parser.add_argument("--force", action="store_true",
                        help="rebuild every cached document, including complete outputs")
    parser.add_argument("--out", type=Path, default=None, help="default <work-dir>/pipeline_input")
    parser.add_argument("--drop-repeated-band", type=int, default=2, metavar="N",
                        help="drop header/footer-band blocks whose text repeats on N or more "
                             "pages (0 disables). Position alone is not enough -- band text "
                             "appearing once is usually real content.")
    parser.add_argument("--furniture-review-share", type=float, default=0.10,
                        metavar="SHARE",
                        help="list documents losing at least this share to repeated "
                             "furniture. Reported for review; never fails the run")
    parser.add_argument("--drop-repeated-unplaced", type=int, default=3, metavar="N",
                        help="drop unplaced lines whose text repeats on N or more pages "
                             "(0 disables); non-repeated unplaced lines are kept")
    parser.add_argument("--keep-md-prefix", action="store_true",
                        help="keep the '## ' markdown prefix on heading text")
    parser.add_argument("--drop-band", action="store_true",
                        help="drop regions in the header/footer band instead of letting the "
                             "sectioner's ribbon detector judge them")
    parser.add_argument("--drop-unplaced", action="store_true",
                        help="drop words that landed in no region (~3%% of a page; mostly nav "
                             "ribbons, but sometimes real body text)")
    parser.add_argument("--raw-dir", type=Path,
                        default=Path("data/01_raw/sustainability"),
                        help="source PDFs, for synthesising rows")
    parser.add_argument("--parse-index-in", type=Path,
                        default=Path("data/00_reference/esg_parse_index.csv"))
    parser.add_argument("--parse-index-out", type=Path, default=None,
                        help="write a v2 parse index derived from the production one")
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers must be at least 1")

    cache_dir = args.layout_dir or args.work_dir / "docling_json"
    fused_dir = args.fused_dir or args.work_dir / "fused"
    out_dir = args.out or (args.work_dir / "pipeline_input")

    caches = sorted(cache_dir.glob("*.pages.json"))
    if not caches:
        print(f"no docling cache under {cache_dir}; run convert and fuse first", file=sys.stderr)
        return 1
    if not fused_dir.exists():
        print(f"no fused text under {fused_dir}; run the fuse stage first", file=sys.stderr)
        return 1

    stems_to_ticker = ticker_map(args.parse_index_in)
    completed = (
        load_completed_bridge_records(args.parse_index_out)
        if args.resume and not args.force
        else {}
    )

    written = 0
    total_missing = 0
    furniture_removed: dict[str, tuple[int, int]] = {}
    built: dict[str, dict[str, Any]] = {}
    tasks = []
    for cache_path in caches:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        stem = cached["pdf_stem"]
        # The chunker joins on (ticker, pdf_stem), so a guessed ticker silently
        # drops the document rather than failing. Prefer the index; the prefix
        # is only a fallback for documents it has never seen.
        ticker = stems_to_ticker.get(stem) or stem.split("-", 1)[0].strip() or "UNKNOWN"

        tasks.append(
            BridgeTask(
                cache_path=cache_path,
                fused_dir=fused_dir,
                out_dir=out_dir,
                ticker=ticker,
                keep_band=not args.drop_band,
                keep_unplaced=not args.drop_unplaced,
                drop_repeated_band=args.drop_repeated_band,
                drop_repeated_unplaced=args.drop_repeated_unplaced,
                strip_md_prefix=not args.keep_md_prefix,
                resume_info=completed.get(stem),
            )
        )

    print(f"Bridge workers: {args.workers}")
    if args.workers == 1:
        results = map(_run_bridge_task, tasks)
        executor = None
    else:
        executor = ProcessPoolExecutor(max_workers=args.workers)
        results = executor.map(_run_bridge_task, tasks)
    try:
        for result in results:
            if result["status"] == "skipped":
                continue
            stem = result["stem"]
            built[stem] = result["built"]
            written += 1
            total_missing += result["missing"]
            if result["dropped"]:
                furniture_removed[stem] = (result["dropped"], result["before"])
            note = f"  ({result['missing']} page(s) had no fused text)" if result["missing"] else ""
            label = "reused" if result["status"] == "reused" else "bridged"
            print(f"{result['ticker']}/{stem}: {label}, {result['pages']} pages, {result['chars']} chars{note}")
    finally:
        if executor is not None:
            executor.shutdown(wait=True)

    print()
    print(f"{written} document(s) -> {out_dir}")

    # Report, do not block. A high share is expected rather than suspicious:
    # a short report carrying a long ribbon on most of its pages loses a large
    # fraction legitimately -- The Children's Place 2023 is 29k characters with
    # a 235-character doubled header repeated on 21 pages, which is 17% of the
    # document and entirely furniture. Every document above this line was
    # checked by hand on the first run and all 20 were nav ribbons or running
    # report titles, none of them content. So the number is a prompt to look,
    # not evidence of damage, and failing the run on it only stops work that
    # should continue.
    if furniture_removed:
        total_dropped = sum(d for d, _ in furniture_removed.values())
        total_before = sum(t for _, t in furniture_removed.values())
        print(
            f"repeated furniture removed: {total_dropped} chars "
            f"({total_dropped / max(total_before, 1):.2%} of affected documents)"
        )
        outliers = sorted(
            (
                (dropped / max(before, 1), stem, dropped)
                for stem, (dropped, before) in furniture_removed.items()
            ),
            reverse=True,
        )
        flagged = [o for o in outliers if o[0] >= args.furniture_review_share]
        if flagged:
            print(
                f"  {len(flagged)} document(s) above "
                f"{args.furniture_review_share:.0%} -- worth a look, not a failure:"
            )
            for share, stem, dropped in flagged[:10]:
                print(f"    {share:6.1%}  -{dropped:6d} chars  {stem}")
            if len(flagged) > 10:
                print(f"    ... and {len(flagged) - 10} more")

    # Documents in the tree that this run did not produce. Usually harmless
    # leftovers from an earlier run; sometimes the same document under a stale
    # ticker, which downstream stages will happily process twice.
    stale = sorted(
        path
        for path in out_dir.rglob("*.txt")
        if not path.name.endswith(".pages.csv") and path.stem not in built
    )
    if stale:
        print(
            f"\nWARNING: {len(stale)} document(s) in {out_dir} were not written "
            f"by this run:"
        )
        for path in stale[:10]:
            print(f"  {path.relative_to(out_dir)}")
        if len(stale) > 10:
            print(f"  ... and {len(stale) - 10} more")
        print(
            "  If a ticker changed, the same document may now exist twice and "
            "will be chunked twice. Remove the stale copy before continuing."
        )

    if args.parse_index_out:
        n, synth = write_parse_index(
            args.parse_index_in, args.parse_index_out, built, args.raw_dir
        )
        print(f"parse index: {n} row(s) -> {args.parse_index_out} ({synth} synthesised)")
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
