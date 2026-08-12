"""Docling spike: score docling page text against the ESG AI gold set.

Read-only against the corpus. Writes only under ``--work-dir``. Nothing here
touches production parser output, the vector manifest, or the gold file.

This script is deliberately standalone -- no ``_bootstrap``/``config`` import --
because docling pulls torch and transformers and should live in its OWN venv,
separate from the production pipeline venv. All paths come in as arguments.

Three stages, run in order:

  convert    PDFs -> per-document docling JSON cache (slow; models run here)
  emit       cache -> selected_page_text.jsonl in the evaluator's schema
  fusecheck  bbox containment diagnostic, docling boxes vs PyMuPDF words

``fusecheck`` is the one that matters before trusting anything else. It answers
the question the fusion design stands or falls on: do docling's boxes and
PyMuPDF's word coordinates actually land in the same coordinate space? If the
"words in zero boxes" share is not near zero, the two are misaligned and every
downstream number is meaningless -- however plausible it looks.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

# Mirrors esg_navigation.HEADER_FOOTER_BAND_SHARE. Duplicated (not imported)
# because this script runs in the docling venv, outside the pipeline package.
HEADER_FOOTER_BAND_SHARE = 0.12

# Same idea, rotated 90 degrees: a persistent vertical nav rail (e.g. TDUP's
# pink sidebar with section links) sits in the left or right margin on every
# page, not the top/bottom band HEADER_FOOTER_BAND_SHARE covers. Measured
# against TDUP-2022: the rail's right edge sits at x=145 of 1224 (0.118) and
# real body text never starts before x=237 (0.194), so 0.15 separates them
# with room on both sides.
SIDE_BAND_SHARE = 0.15

# Where a column boundary sits across the gutter. 0.9 keeps trailing text
# with its own column and mirrors the row rule (cut just before the next row
# starts). Not corpus-validated -- it is a knob, exposed as --col-cut-share.
COL_CUT_SHARE = 0.9


# --------------------------------------------------------------------------
# docling accessors
#
# The DoclingDocument API has moved between releases, so every field access
# below is defensive. A spike that dies on an attribute rename teaches nothing.
# --------------------------------------------------------------------------


def _docling_version() -> str:
    try:
        import docling

        return getattr(docling, "__version__", "unknown")
    except Exception as exc:  # pragma: no cover - import-time environment probe
        return f"<not importable: {exc}>"


def _page_size(doc: Any, page_no: int) -> tuple[float, float] | None:
    """Return (width, height) of a docling page, or None if unavailable."""
    pages = getattr(doc, "pages", None) or {}
    page = pages.get(page_no) if hasattr(pages, "get") else None
    size = getattr(page, "size", None)
    if size is None:
        return None
    width = getattr(size, "width", None)
    height = getattr(size, "height", None)
    if width is None or height is None:
        return None
    return float(width), float(height)


def _bbox_top_left(bbox: Any, page_height: float | None) -> tuple[float, float, float, float] | None:
    """Normalise a docling bbox to top-left origin, y increasing downward.

    Docling carries an explicit ``coord_origin``; PDF-native boxes are commonly
    BOTTOMLEFT while PyMuPDF words are always TOPLEFT. Converting here, once,
    is what keeps the fusion honest.
    """
    if bbox is None:
        return None
    try:
        left = float(bbox.l)
        top = float(bbox.t)
        right = float(bbox.r)
        bottom = float(bbox.b)
    except Exception:
        return None

    origin = getattr(bbox, "coord_origin", None)
    origin_name = getattr(origin, "name", str(origin) if origin is not None else "")
    if origin_name.upper().startswith("BOTTOM") and page_height is not None:
        top, bottom = page_height - top, page_height - bottom

    if top > bottom:
        top, bottom = bottom, top
    if left > right:
        left, right = right, left
    return left, top, right, bottom


def _item_placements(item: Any, doc: Any) -> list[tuple[int, tuple[float, float, float, float] | None, tuple[int, int] | None]]:
    """Every place on the page(s) this item occupies.

    Docling merges an element with the next one when the first ends without
    terminal punctuation -- a lowercase letter, comma or hyphen. The merged
    item then covers TWO regions of the page and carries two provenance
    entries. Keeping only ``prov[0]``, as this used to, left the second region
    with no box at all: the paragraph looked undetected even though docling
    had found it. Four-column pages hit this most, because a narrow column
    rarely ends a paragraph on a full stop.

    Each provenance carries a ``charspan`` into the item's own text, so the
    text can be split back across its locations instead of guessed at.
    """
    out = []
    for prov in getattr(item, "prov", None) or []:
        page_no = getattr(prov, "page_no", None)
        if page_no is None:
            continue
        size = _page_size(doc, page_no)
        bbox = _bbox_top_left(getattr(prov, "bbox", None), size[1] if size else None)
        span = getattr(prov, "charspan", None)
        if span is not None:
            try:
                span = (int(span[0]), int(span[1]))
            except Exception:
                span = None
        out.append((page_no, bbox, span))
    return out


def _item_text(item: Any, doc: Any) -> str:
    """Render one docling item to text, tables as markdown."""
    export = getattr(item, "export_to_markdown", None)
    if callable(export):
        try:
            return export(doc).strip()
        except TypeError:
            try:
                return export().strip()
            except Exception:
                pass
        except Exception:
            pass
    return str(getattr(item, "text", "") or "").strip()


def _label(item: Any) -> str:
    label = getattr(item, "label", None)
    return getattr(label, "value", None) or str(label or "unknown")


def _iter_items(doc: Any) -> Iterable[Any]:
    iterate = getattr(doc, "iterate_items", None)
    if callable(iterate):
        for entry in iterate():
            # iterate_items yields (item, level). The level IS the heading
            # hierarchy -- discarding it, as this used to, throws away the
            # only structural signal docling produces.
            if isinstance(entry, tuple):
                yield entry[0], (entry[1] if len(entry) > 1 else None)
            else:
                yield entry, None
        return
    for attr in ("texts", "tables", "pictures"):
        for item in getattr(doc, attr, None) or []:
            yield item, None


# --------------------------------------------------------------------------
# stage: convert
# --------------------------------------------------------------------------


def _build_converter(args: argparse.Namespace) -> Any:
    """Construct a DocumentConverter, optionally with OCR switched off.

    Docling OCRs picture regions by default. On born-digital ESG reports that
    mostly means running an OCR engine across logos and photographs to learn
    they contain no text -- the "RapidOCR returned empty result" chatter -- at
    real cost per page. Turning it off is safe ONLY for PDFs with a real text
    layer; a scanned report parsed with --no-ocr comes back empty, which is why
    this is opt-in rather than the default.
    """
    from docling.document_converter import DocumentConverter

    flags = (
        args.no_ocr
        or getattr(args, "heading_hierarchy", False)
        or getattr(args, "backend_text", False)
        or getattr(args, "full_page_ocr", False)
        or getattr(args, "skip_cells", False)
    )
    if not flags:
        return DocumentConverter()

    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import PdfFormatOption

        options = PdfPipelineOptions()
        enabled = []
        if args.no_ocr:
            options.do_ocr = False
            enabled.append("do_ocr=False")
        if getattr(args, "full_page_ocr", False):
            options.do_ocr = True
            options.ocr_options.force_full_page_ocr = True
            enabled.append("force_full_page_ocr=True")
        if getattr(args, "heading_hierarchy", False):
            options.heading_hierarchy_options.enabled = True
            enabled.append("heading_hierarchy=True")
        if getattr(args, "backend_text", False):
            options.force_backend_text = True
            enabled.append("force_backend_text=True")
        if getattr(args, "skip_cells", False):
            options.layout_options.skip_cell_assignment = True
            enabled.append("skip_cell_assignment=True")
        print("docling options: " + ", ".join(enabled))
        return DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
        )
    except Exception as exc:
        print(
            f"could not disable OCR on this docling build ({exc}); continuing with defaults",
            file=sys.stderr,
        )
        return DocumentConverter()


def _table_cells(item: Any, doc: Any, page_no: int) -> dict[str, Any] | None:
    """Capture a table's cell grid: spans, header flags, and per-cell boxes.

    Note the coordinate trap: docling reports table CELL boxes as TOPLEFT while
    the table item's own provenance box is BOTTOMLEFT. Both go through
    ``_bbox_top_left``, which reads ``coord_origin`` rather than assuming.
    """
    data = getattr(item, "data", None)
    cells = getattr(data, "table_cells", None)
    if not cells:
        return None
    size = _page_size(doc, page_no)
    height = size[1] if size else None
    out = []
    for cell in cells:
        out.append(
            {
                "r0": int(cell.start_row_offset_idx),
                "r1": int(cell.end_row_offset_idx),
                "c0": int(cell.start_col_offset_idx),
                "c1": int(cell.end_col_offset_idx),
                "header": bool(getattr(cell, "column_header", False)),
                "text": str(getattr(cell, "text", "") or ""),
                "bbox": _bbox_top_left(getattr(cell, "bbox", None), height),
            }
        )
    return {
        "num_rows": int(getattr(data, "num_rows", 0)),
        "num_cols": int(getattr(data, "num_cols", 0)),
        "cells": out,
    }


def _has_text_layer(pdf_path: Path, sample: int = 12, min_words: int = 20) -> bool:
    """True when the PDF already carries extractable text on most pages.

    Docling OCRs bitmap regions by default. On a scanned page that is correct.
    On a scanned page that has ALREADY been through OCR -- which is what the
    pipeline's OCR remediation stage produces -- it re-reads the image and
    emits a second, worse copy of the same sentences, with no spaces between
    words. WMT-WALMART-2023 p26 came out with 56 regions instead of ~30, about
    20 of them empty, and a picture region that stole fragments of real text.

    Sampling evenly rather than reading every page keeps this cheap on a
    500-document corpus.
    """
    import fitz

    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return False
    try:
        total = doc.page_count
        if total == 0:
            return False
        step = max(1, total // sample)
        checked = hits = 0
        for i in range(0, total, step):
            checked += 1
            if len(doc[i].get_text("words")) >= min_words:
                hits += 1
        return checked > 0 and hits / checked >= 0.7
    finally:
        doc.close()



def _is_type3(pdf_path: Path, sample: int = 8, threshold: float = 0.5) -> bool:
    """True when this document's glyphs are Type3 drawing programs.

    Docling's PDF backend reads no text cells at all from a Type3 font.
    Measured on CROX-CROCS INC-2025 page 11: 0 cells against PyMuPDF's 8,324
    characters on the same page. Every text cluster the layout model predicts
    therefore comes back empty and is dropped, leaving only picture and table
    regions -- so no section_header is emitted, sectioning has nothing to split
    at, and the document contributes zero chunks after converting, fusing and
    sectioning without a single error.

    Over the 681-document corpus, 8 documents are majority-Type3 and those 8
    are exactly the 8 that produced no chunks. No false positives, no false
    negatives, which is what makes routing on this signal safe.

    Font descriptors only, never page content, and a shallow page sample: the
    real cases sit at 100% Type3 rather than marginal, so this stays cheap.
    """
    import fitz

    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return False
    try:
        type3 = total = 0
        for pno in range(min(doc.page_count, sample)):
            for font in doc[pno].get_fonts(full=True):
                total += 1
                if font[2] == "Type3":
                    type3 += 1
        return total > 0 and type3 / total > threshold
    finally:
        doc.close()


def _should_skip_cells(pdf_path: Path, args: argparse.Namespace) -> bool:
    """Whether this document is converted with cell assignment skipped.

    Shared by the converter choice and the cache payload so the two cannot
    disagree: _report_sparse_cache reads the recorded flag to tell a Type3
    document (no docling text by design, words supplied later by fusion) from
    one that genuinely has no text layer and does need OCR.
    """
    if getattr(args, "skip_cells", False):
        return True
    if getattr(args, "no_auto_skip_cells", False):
        return False
    return _is_type3(pdf_path)


def _converter_for(
    pdf_path: Path, args: argparse.Namespace, cache: dict[tuple[bool, bool], Any]
) -> Any:
    """Pick the converter for this document: OCR on/off, cell assignment on/off.

    With --auto-ocr, a PDF that already has a text layer is converted with OCR
    disabled so docling does not re-read the page image and duplicate it.

    Type3 documents are converted with cell assignment skipped, which keeps the
    layout model's regions instead of discarding the ones docling could not put
    text into. This costs nothing in accuracy here because fusion never used
    docling's text in the first place -- it takes its words from PyMuPDF, which
    reads Type3 correctly. Docling only has to say where the regions are.

    Measured on GPRO-GOPRO INC-2023: 0 section_header regions before, 23 after,
    and the fused pages carry proper heading text ("## Employee Engagement",
    "## Diversity, Equity, Inclusion, and Belonging (DEIB)") because fusion
    fills the empty boxes. Full-page OCR reaches the same fused output at 2.0
    min against 1.6, by re-deriving text that fusion then overwrites -- and its
    own reading of these headings was badly scrambled. Skipping cells attacks
    the broken link instead of routing around it.

    Routing rather than a global switch: only Type3 documents change path, and
    they currently produce nothing at all, so the 673 documents that already
    work convert exactly as before. --no-auto-skip-cells restores the old
    behaviour for reproducing an earlier run.
    """
    want_ocr = True
    if getattr(args, "auto_ocr", False) and _has_text_layer(pdf_path):
        want_ocr = False
    skip_cells = _should_skip_cells(pdf_path, args)
    if skip_cells and not getattr(args, "skip_cells", False):
        print("  (Type3 fonts -> skipping cell assignment for this document)")
    key = (want_ocr, skip_cells)
    if key not in cache:
        import copy as _copy

        sub = _copy.copy(args)
        sub.no_ocr = args.no_ocr or (not want_ocr)
        sub.skip_cells = skip_cells
        cache[key] = _build_converter(sub)
        if not want_ocr:
            print("  (text layer present -> OCR disabled for this document)")
    return cache[key]


def _collect_items(doc: Any, force_page: int | None = None) -> dict[int, list[dict[str, Any]]]:
    """Group a converted document's items by page number.

    ``force_page`` exists because a page-range conversion may renumber the
    extracted page to 1 rather than keeping its position in the source PDF.
    Which of the two docling does varies by release, so when we asked for a
    single known page we assert the answer rather than trusting the label.
    """
    by_page: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item, level in _iter_items(doc):
        placements = _item_placements(item, doc)
        if not placements:
            continue
        full_text = _item_text(item, doc)
        raw_text = str(getattr(item, "text", "") or "")
        for page_no, bbox, span in placements:
            if force_page is not None:
                page_no = force_page
            # Split the text across its locations when we can; a table's
            # markdown has no meaningful charspan, so it stays whole.
            if len(placements) > 1 and span and raw_text and full_text == raw_text.strip():
                piece = raw_text[span[0]:span[1]].strip()
            else:
                piece = full_text
            entry = {
                "label": _item_label_safe(item),
                "text": piece,
                "bbox": bbox,
                "level": level,
                "split_of": len(placements) if len(placements) > 1 else None,
            }
            if entry["label"] == "table":
                grid = _table_cells(item, doc, page_no)
                if grid:
                    entry["grid"] = grid
            by_page[page_no].append(entry)
    return by_page


def stage_convert_gold_pages(args: argparse.Namespace) -> int:
    """Convert ONLY the pages the gold set scores, one page range at a time.

    This is the decision-grade local test: it produces the full development
    split score for ~40 pages of model time instead of the ~3,500 pages a
    whole-document run costs, because nothing outside the scored pages
    affects the benchmark.

    The tradeoff is real and worth stating: a single-page conversion has no
    cross-page context, so an item continuing from the previous page is seen
    cold. The gold set was itself built from single-page images, so the
    benchmark is blind to that either way -- but a full-document run remains
    the honest configuration for a production decision, which is what the
    Colab GPU pass is for.
    """
    cache_dir = args.layout_dir or args.work_dir / "docling_json"
    cache_dir.mkdir(parents=True, exist_ok=True)

    wanted: dict[str, set[int]] = defaultdict(set)

    if args.pages or args.random_pages:
        # Explicit page selection: "file.pdf:31,32,40;other.pdf:5"
        for spec in filter(None, (x.strip() for x in args.pages.split(";"))):
            name, _, nums = spec.rpartition(":")
            for n in filter(None, (x.strip() for x in nums.split(","))):
                wanted[name].add(int(n))
        if args.random_pages:
            import random

            rng = random.Random(args.seed)
            pool: list[tuple[str, int]] = []
            for pdf in sorted(args.pdf_dir.rglob("*.pdf")):
                try:
                    import fitz

                    d = fitz.open(str(pdf))
                    pool += [(pdf.name, i + 1) for i in range(d.page_count)]
                    d.close()
                except Exception:
                    continue
            already = {(n, p) for n, ps in wanted.items() for p in ps}
            pool = [x for x in pool if x not in already]
            for name, page in rng.sample(pool, min(args.random_pages, len(pool))):
                wanted[name].add(page)
    else:
        gold = [json.loads(l) for l in args.gold.read_text(encoding="utf-8").splitlines() if l.strip()]
        if args.split:
            gold = [r for r in gold if r.get("split") == args.split]
        for record in gold:
            wanted[record["pdf_file"]].add(int(record["page"]))
    if not wanted:
        print("no gold records after filtering", file=sys.stderr)
        return 1

    items = sorted(wanted.items())
    if args.limit:
        items = items[: args.limit]
    total_pages = sum(len(pages) for _, pages in items)
    print(
        f"docling {_docling_version()}  |  gold-page mode: "
        f"{total_pages} page(s) across {len(items)} PDF(s)"
    )

    converter = _build_converter(args)
    _conv_cache: dict[tuple[bool, bool], Any] = {}
    page_range_supported = True
    done = 0
    timings: list[float] = []
    run_started = time.time()
    budget_seconds = args.time_budget_min * 60 if args.time_budget_min else 0.0

    for pdf_name, pages in items:
        matches = list(args.pdf_dir.rglob(pdf_name))
        if not matches:
            print(f"skip {pdf_name}: not found under {args.pdf_dir}", file=sys.stderr)
            continue
        pdf = matches[0]
        out_path = cache_dir / f"{pdf.stem}.pages.json"
        merged: dict[str, list[dict[str, Any]]] = {}
        if out_path.exists() and not args.force:
            merged = json.loads(out_path.read_text(encoding="utf-8")).get("pages", {})

        for page_no in sorted(pages):
            done += 1
            if str(page_no) in merged and not args.force:
                print(f"[{done}/{total_pages}] skip (cached) {pdf_name} p{page_no}")
                continue
            started = time.time()
            try:
                if page_range_supported:
                    result = _converter_for(pdf, args, _conv_cache).convert(str(pdf), page_range=(page_no, page_no))
                else:
                    result = converter.convert(str(pdf))
            except TypeError:
                # Older docling without page_range: fall back once, loudly,
                # because the cost model changes completely.
                print(
                    "  note: this docling build has no page_range; falling back "
                    "to whole-document conversion (much slower)",
                    file=sys.stderr,
                )
                page_range_supported = False
                result = converter.convert(str(pdf))
            except Exception as exc:
                print(f"[{done}/{total_pages}] FAILED {pdf_name} p{page_no}: {exc}", file=sys.stderr)
                continue

            elapsed = time.time() - started
            timings.append(elapsed)
            force = page_no if page_range_supported else None
            grouped = _collect_items(result.document, force_page=force)
            if page_range_supported:
                merged[str(page_no)] = grouped.get(page_no, [])
            else:
                merged.update({str(k): v for k, v in grouped.items()})
            n_items = len(merged.get(str(page_no), []))

            # Flush after EVERY page, not once per document. A time-boxed run
            # that gets interrupted must lose at most the page in flight.
            out_path.write_text(
                json.dumps(
                    {"pdf_file": pdf.name, "pdf_stem": pdf.stem, "mode": "gold_pages", "pages": merged},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            spent = time.time() - run_started
            remaining = total_pages - done
            rate = statistics.mean(timings)
            print(
                f"[{done}/{total_pages}] {pdf_name} p{page_no}: {n_items} items, "
                f"{elapsed:.1f}s | elapsed {spent / 60:.1f}m, "
                f"~{remaining * rate / 60:.1f}m left at {rate:.1f}s/page"
            )

            if budget_seconds and spent >= budget_seconds:
                print(
                    f"\nstopping: {args.time_budget_min:.0f} min budget reached after "
                    f"{done}/{total_pages} page(s). Cache is complete for those pages -- "
                    f"re-run the same command to resume where this left off.",
                )
                return 0

    if timings:
        print(
            f"\nconverted {len(timings)} page(s), {sum(timings):.1f}s total, "
            f"{statistics.mean(timings):.2f}s/page mean"
        )
    return 0


# Text density separates "this report is image-heavy by design" from "this PDF
# has no text layer and every word is locked in a picture". Measured over the
# 131-document cache: median 330 words/page, 10th percentile 212, and the
# sparsest genuine document (BIRD-ALLBIRDS-2024, a 15-page report) sits at 75.5.
# GPRO-2023 converted at under 45. So below 60 is a document PyMuPDF cannot read
# either, and 60-100 is worth a look without being an error.
SPARSE_OCR_WORDS_PER_PAGE = 60.0
SPARSE_REVIEW_WORDS_PER_PAGE = 100.0


def _text_density(by_page: dict[int, list[dict[str, Any]]], n_pages: int) -> float:
    """Words per page across docling's text-bearing regions."""
    if not n_pages:
        return 0.0
    words = sum(
        len((item.get("text") or "").split())
        for items in by_page.values()
        for item in items
    )
    return words / n_pages


# A document is cached only when BOTH halves are on disk. They are written as
# two separate files, so a stop between them used to leave <stem>.json alone on
# disk -- which the old existence check accepted, skipping that document on
# every future run. It never errored and never appeared in any count; the
# document simply vanished from the corpus.
def _cache_paths(cache_dir: Path, stem: str) -> tuple[Path, Path]:
    return cache_dir / f"{stem}.json", cache_dir / f"{stem}.pages.json"


def _cache_is_complete(cache_dir: Path, stem: str) -> bool:
    return all(p.exists() and p.stat().st_size > 0 for p in _cache_paths(cache_dir, stem))


def _write_json_atomic(path: Path, payload: Any) -> None:
    """Write via a temp file and rename, so a kill never leaves a partial file.

    Existence alone is not enough to trust a cache entry: a process killed
    mid-write leaves a truncated file that exists and parses as garbage.
    Path.replace is atomic within a filesystem, so readers see either the old
    file or the complete new one, never a prefix of it.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def stage_convert(args: argparse.Namespace) -> int:
    if args.gold_pages:
        return stage_convert_gold_pages(args)

    cache_dir = args.layout_dir or args.work_dir / "docling_json"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Leftovers from a kill during a previous run's rename window.
    for stale in cache_dir.glob("*.json.tmp"):
        print(f"removing stale temp file {stale.name}", file=sys.stderr)
        stale.unlink()

    pdfs = sorted(p for p in args.pdf_dir.rglob("*.pdf"))
    if args.limit:
        pdfs = pdfs[: args.limit]
    if not pdfs:
        print(f"no PDFs found under {args.pdf_dir}", file=sys.stderr)
        return 1

    if args.shards > 1:
        # Shard across processes. There is no GPU here, so running several
        # converters at once is the only way to use the machine; each writes
        # <stem>.json, so distinct documents never contend for a file.
        #
        # Largest first, dealt round-robin. Convert time is roughly linear in
        # page count, and under a wall-clock budget an unlucky shard holding
        # several 78-page reports would finish far fewer documents than its
        # peers. File size stands in for page count so planning stays cheap --
        # opening every PDF to plan the split would cost more than it saves.
        pdfs.sort(key=lambda p: -p.stat().st_size)
        pdfs = [p for i, p in enumerate(pdfs) if i % args.shards == args.shard]
        print(f"shard {args.shard + 1}/{args.shards}: {len(pdfs)} document(s)")

    print(f"docling {_docling_version()}  |  {len(pdfs)} PDF(s)")
    converter = _build_converter(args)
    _conv_cache: dict[tuple[bool, bool], Any] = {}
    timings: list[tuple[str, float, int]] = []

    # Previously only the --gold-pages path read this, so a whole-document run
    # ignored the budget and ran to completion however long it took. Checked at
    # the document boundary: stopping mid-convert would write no cache entry
    # and throw away the work already spent on that document.
    budget_seconds = args.time_budget_min * 60 if args.time_budget_min else 0.0
    run_started = time.time()

    for index, pdf in enumerate(pdfs, start=1):
        out_path, pages_path = _cache_paths(cache_dir, pdf.stem)
        if _cache_is_complete(cache_dir, pdf.stem) and not args.force:
            print(f"[{index}/{len(pdfs)}] skip (cached) {pdf.name}")
            continue
        # Half a cache entry is worse than none: drop it so this run rebuilds
        # both halves rather than trusting whichever one survived.
        for half in (out_path, pages_path):
            if half.exists():
                print(f"[{index}/{len(pdfs)}] incomplete cache, reconverting {pdf.name}")
                half.unlink()
        if budget_seconds and (time.time() - run_started) >= budget_seconds:
            spent = (time.time() - run_started) / 60
            remaining = len(pdfs) - index + 1
            print(
                "\n"
                + f"stopping: {args.time_budget_min:.0f} min budget reached after "
                f"{spent:.1f} min, {remaining} document(s) not started. "
                f"Re-run to resume; converted documents are cached."
            )
            break
        started = time.time()
        try:
            result = _converter_for(pdf, args, _conv_cache).convert(str(pdf))
        except Exception as exc:
            print(f"[{index}/{len(pdfs)}] FAILED {pdf.name}: {exc}", file=sys.stderr)
            continue
        doc = result.document
        elapsed = time.time() - started
        n_pages = len(getattr(doc, "pages", {}) or {})

        payload = doc.export_to_dict()
        _write_json_atomic(out_path, payload)

        # Page-grouped text is what the emit stage needs, and building it here
        # means emit never has to re-run the models. This MUST go through
        # _collect_items rather than repeating its logic: an earlier inline
        # copy here silently missed the multi-provenance fix and broke this
        # whole-document path while the gold-page path kept working.
        by_page = _collect_items(doc)
        _write_json_atomic(
            pages_path,
            {
                "pdf_file": pdf.name,
                "pdf_stem": pdf.stem,
                "n_pages": n_pages,
                "seconds": round(elapsed, 2),
                # Recorded so the density report can tell "no text by design"
                # from "no text layer, needs OCR". Absent on older entries,
                # which is read as False.
                "skip_cells": _should_skip_cells(pdf, args),
                "pages": {str(k): v for k, v in sorted(by_page.items())},
            },
        )
        timings.append((pdf.name, elapsed, n_pages))
        per_page = elapsed / n_pages if n_pages else float("nan")
        print(
            f"[{index}/{len(pdfs)}] {pdf.name}: {n_pages} pages, "
            f"{elapsed:.1f}s ({per_page:.2f}s/page)"
        )

        # A picture-heavy report with no text layer converts quickly and looks
        # successful -- GPRO-2023 produced 19 pages and almost no words. Say so
        # at the point of conversion instead of leaving it to be caught by eye.
        # A Type3 document is deliberately converted without cell assignment,
        # so its docling text is empty by construction and this measure says
        # nothing about the page. Fusion fills the regions from PyMuPDF.
        density = _text_density(by_page, n_pages)
        if density < SPARSE_REVIEW_WORDS_PER_PAGE and not _should_skip_cells(pdf, args):
            label = "NO TEXT LAYER" if density < SPARSE_OCR_WORDS_PER_PAGE else "sparse"
            print(
                f"    ^ {label}: {density:.0f} words/page "
                f"(corpus median ~330) -- likely needs -WithOcr",
                file=sys.stderr,
            )

    if timings:
        total_pages = sum(n for _, _, n in timings)
        total_time = sum(t for _, t, _ in timings)
        print(
            f"\nconverted {len(timings)} doc(s), {total_pages} pages, "
            f"{total_time:.1f}s total, {total_time / max(total_pages, 1):.2f}s/page mean"
        )

    _report_sparse_cache(cache_dir)
    return 0


def _report_sparse_cache(cache_dir: Path) -> None:
    """List every cached document whose text density suggests it needs OCR.

    Scans the whole cache rather than only this run's conversions: a document
    converted three runs ago is just as empty, and until now the only way to
    notice was to read the overlays.
    """
    found: list[tuple[float, str, int]] = []
    # Converted with cell assignment skipped, so docling holds no text for them
    # by construction. Reporting these as NEEDS OCR would be a false alarm, and
    # worse, the advice attached to it ("re-convert with -WithOcr") is the
    # wrong remedy for a Type3 document.
    skipped_cells: set[str] = set()
    for path in sorted(cache_dir.glob("*.pages.json")):
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"unreadable cache entry {path.name}: {exc}", file=sys.stderr)
            continue
        pages = cached.get("pages", {})
        n_pages = cached.get("n_pages") or len(pages)
        by_page = {int(k): v for k, v in pages.items()}
        density = _text_density(by_page, n_pages)
        stem = cached.get("pdf_stem", path.stem)
        if cached.get("skip_cells"):
            skipped_cells.add(stem)
        if density < SPARSE_REVIEW_WORDS_PER_PAGE:
            found.append((density, stem, n_pages))

    if not found:
        return

    needs_ocr = [f for f in found if f[0] < SPARSE_OCR_WORDS_PER_PAGE and f[1] not in skipped_cells]
    print(f"\n{'=' * 64}")
    print(f"  text density: {len(found)} document(s) below "
          f"{SPARSE_REVIEW_WORDS_PER_PAGE:.0f} words/page")
    print("=" * 64)
    for density, stem, n_pages in sorted(found):
        if stem in skipped_cells:
            tag = "type3 ok "
        elif density < SPARSE_OCR_WORDS_PER_PAGE:
            tag = "NEEDS OCR"
        else:
            tag = "review   "
        print(f"  {tag}  {density:6.1f} w/pg  {n_pages:4d}p  {stem}")
    if skipped_cells & {f[1] for f in found}:
        print(
            "\n  type3 ok: cell assignment skipped, so docling carries no text for these."
            "\n  Fusion supplies the words from PyMuPDF; they are not missing text."
        )
    if needs_ocr:
        print(
            f"\n  {len(needs_ocr)} document(s) have effectively no text layer. "
            "Re-convert just those with -WithOcr;\n"
            "  leaving them as-is puts empty pages into the retrieval index."
        )


def _item_label_safe(item: Any) -> str:
    try:
        return _label(item)
    except Exception:
        return "unknown"


# --------------------------------------------------------------------------
# stage: emit
# --------------------------------------------------------------------------


def stage_emit(args: argparse.Namespace) -> int:
    cache_dir = args.layout_dir or args.work_dir / "docling_json"
    gold = [json.loads(line) for line in args.gold.read_text(encoding="utf-8").splitlines() if line.strip()]

    if args.split:
        gold = [r for r in gold if r.get("split") == args.split]
    if not gold:
        print("no gold records after filtering", file=sys.stderr)
        return 1

    # Furniture docling already labels as page chrome. Dropping it here mirrors
    # what the production page-role gate does, so the comparison is like-for-like
    # rather than crediting docling for a filter the gold set does not reward.
    drop_labels = {"page_header", "page_footer"} if args.drop_furniture else set()

    out_records: list[dict[str, Any]] = []
    missing: list[str] = []

    for record in gold:
        stem = Path(record["pdf_file"]).stem
        pages_path = cache_dir / f"{stem}.pages.json"
        if not pages_path.exists():
            missing.append(record["item_id"])
            continue
        cached = json.loads(pages_path.read_text(encoding="utf-8"))
        items = cached.get("pages", {}).get(str(record["page"]), [])
        blocks = []
        dropped: list[str] = []
        for it in items:
            text = (it.get("text") or "").strip()
            if text and it.get("label") in drop_labels:
                dropped.append(text)
            if not text or it.get("label") in drop_labels:
                continue
            # Picture items carry no page text -- docling substitutes an HTML
            # comment telling you to enable image generation. Scoring that
            # string as parser output would tank token precision for no reason.
            if text.startswith("<!--"):
                continue
            blocks.append(text)
        out_records.append(
            {
                "item_id": record["item_id"],
                "ticker": record.get("ticker"),
                "pdf_file": record["pdf_file"],
                "pdf_stem": stem,
                "page": record["page"],
                "split": record.get("split"),
                "sample_category": record.get("sample_category"),
                "source_sha256": record.get("source_sha256"),
                "image_sha256": record.get("image_sha256"),
                "parser_text": "\n\n".join(blocks),
                "dropped_furniture": dropped,
                "parser_used": "docling",
                "parser_policy": f"docling_{_docling_version()}"
                + ("_nofurniture" if args.drop_furniture else "_raw"),
            }
        )

    out_path = args.work_dir / "selected_page_text.jsonl"
    with out_path.open("w", encoding="utf-8") as handle:
        for record in out_records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    # The evaluator iterates every row of --gold and hard-fails on a missing
    # parser row, so scoring a subset means handing it a matching gold subset.
    # Writing it here (rather than editing the evaluator) keeps the holdout
    # sealed by construction: pages we did not emit are not in the file at all.
    emitted_ids = {r["item_id"] for r in out_records}
    subset_path = args.work_dir / "gold_subset.jsonl"
    with subset_path.open("w", encoding="utf-8") as handle:
        for record in gold:
            if record["item_id"] in emitted_ids:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"wrote {len(out_records)} record(s) -> {out_path}")
    print(f"wrote matching gold subset  -> {subset_path}  (pass this as --gold)")
    if missing:
        print(
            f"WARNING: {len(missing)} gold page(s) had no docling cache "
            f"(convert their PDFs first): {', '.join(missing[:8])}"
            + (" ..." if len(missing) > 8 else ""),
            file=sys.stderr,
        )
    empty = [r["item_id"] for r in out_records if not r["parser_text"].strip()]
    if empty:
        print(f"WARNING: {len(empty)} page(s) produced empty text: {empty[:8]}", file=sys.stderr)

    # Strip accounting. The gold set is a TEXT-DETECTION reference, not a
    # furniture-removal reference: it neither asks for strips to go nor credits
    # dropping them. So every strip token removed here is a token the score can
    # only read as missing. Report the exposure rather than let it look like a
    # detection failure downstream.
    if args.drop_furniture:
        pages_hit = [r for r in out_records if r["dropped_furniture"]]
        strip_tokens = sum(
            len(" ".join(r["dropped_furniture"]).split()) for r in out_records
        )
        kept_tokens = sum(len(r["parser_text"].split()) for r in out_records)
        print(
            f"\nstrip accounting: dropped {strip_tokens} token(s) as furniture "
            f"across {len(pages_hit)}/{len(out_records)} page(s) "
            f"({strip_tokens / max(strip_tokens + kept_tokens, 1):.1%} of docling's text)"
        )
        for record in pages_hit[:5]:
            preview = " / ".join(record["dropped_furniture"])[:110]
            print(f"  {record['item_id']}: {preview}")
        print(
            "  -> any of this the gold set happens to include will read as a "
            "recall miss. Re-run emit without --drop-furniture to separate "
            "furniture cost from reading-order quality."
        )
    return 0


# --------------------------------------------------------------------------
# stage: fusecheck
# --------------------------------------------------------------------------


def stage_fusecheck(args: argparse.Namespace) -> int:
    import fitz  # PyMuPDF

    cache_dir = args.work_dir / "docling_json"
    caches = sorted(cache_dir.glob("*.pages.json"))
    if args.limit:
        caches = caches[: args.limit]
    if not caches:
        print(f"no docling cache under {cache_dir}; run `convert` first", file=sys.stderr)
        return 1

    pad = args.pad
    rows: list[dict[str, Any]] = []

    for cache_path in caches:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        pdf_name = cached["pdf_file"]
        matches = list(args.pdf_dir.rglob(pdf_name))
        if not matches:
            print(f"skip {pdf_name}: source PDF not found under {args.pdf_dir}", file=sys.stderr)
            continue

        doc = fitz.open(str(matches[0]))
        for page_key, items in cached.get("pages", {}).items():
            page_no = int(page_key)
            if page_no < 1 or page_no > doc.page_count:
                continue
            page = doc[page_no - 1]
            words = page.get_text("words")
            if not words:
                continue
            boxes = [it["bbox"] for it in items if it.get("bbox")]
            if not boxes:
                rows.append(
                    {
                        "pdf": pdf_name,
                        "page": page_no,
                        "words": len(words),
                        "zero": len(words),
                        "one": 0,
                        "multi": 0,
                        "boxes": 0,
                    }
                )
                continue

            # Docling reports boxes in its own page space; PyMuPDF in PDF points.
            # Scale before comparing or every containment test is nonsense.
            # Docling page space IS PDF points; no rescaling needed.
            scale_x = scale_y = 1.0

            scaled = [
                (b[0] * scale_x - pad, b[1] * scale_y - pad, b[2] * scale_x + pad, b[3] * scale_y + pad)
                for b in boxes
            ]

            # Two assignment rules, scored side by side rather than picking one
            # on principle. Centroid is what this script started with; overlap
            # (intersection over the WORD's own area, the convention docling and
            # layoutparser use) is the field default. IoU is deliberately absent:
            # a word is tiny next to a region, so IoU is ~0 even for a perfect
            # containment and would reject everything.
            counts = Counter()
            disagreements: list[dict[str, Any]] = []
            for x0, y0, x1, y1, word, *_ in words:
                cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
                word_area = max((x1 - x0) * (y1 - y0), 1e-9)
                centroid_hits = []
                overlap_hits = []
                for idx, (bx0, by0, bx1, by1) in enumerate(scaled):
                    if bx0 <= cx <= bx1 and by0 <= cy <= by1:
                        centroid_hits.append(idx)
                    ix = max(0.0, min(x1, bx1) - max(x0, bx0))
                    iy = max(0.0, min(y1, by1) - max(y0, by0))
                    if (ix * iy) / word_area >= args.overlap_threshold:
                        overlap_hits.append(idx)

                n = len(centroid_hits)
                counts["zero" if n == 0 else "one" if n == 1 else "multi"] += 1
                m = len(overlap_hits)
                counts["ov_zero" if m == 0 else "ov_one" if m == 1 else "ov_multi"] += 1

                if set(centroid_hits) != set(overlap_hits):
                    counts["disagree"] += 1
                    if len(disagreements) < args.max_disagreements:
                        disagreements.append(
                            {
                                "word": word,
                                "bbox": [round(v, 1) for v in (x0, y0, x1, y1)],
                                "centroid_boxes": centroid_hits,
                                "overlap_boxes": overlap_hits,
                            }
                        )

            rows.append(
                {
                    "pdf": pdf_name,
                    "page": page_no,
                    "words": len(words),
                    "zero": counts["zero"],
                    "one": counts["one"],
                    "multi": counts["multi"],
                    "overlap_zero": counts["ov_zero"],
                    "overlap_one": counts["ov_one"],
                    "overlap_multi": counts["ov_multi"],
                    "disagree": counts["disagree"],
                    "disagreement_examples": disagreements,
                    "boxes": len(boxes),
                    "scale_applied": round(scale_x, 4),
                }
            )
        doc.close()

    if not rows:
        print("no pages compared", file=sys.stderr)
        return 1

    total_words = sum(r["words"] for r in rows)
    total_zero = sum(r["zero"] for r in rows)
    total_multi = sum(r["multi"] for r in rows)
    total_disagree = sum(r["disagree"] for r in rows)
    ov_zero = sum(r["overlap_zero"] for r in rows)
    ov_multi = sum(r["overlap_multi"] for r in rows)
    zero_shares = [r["zero"] / r["words"] for r in rows if r["words"]]

    out_path = args.work_dir / "fusecheck.json"
    out_path.write_text(
        json.dumps(
            {
                "pages": rows,
                "total_words": total_words,
                "unassigned_share": total_zero / max(total_words, 1),
                "multi_assigned_share": total_multi / max(total_words, 1),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"pages compared      : {len(rows)}")
    print(f"total PyMuPDF words : {total_words}")
    print("\n-- centroid-in-box rule --")
    print(f"words in NO box     : {total_zero} ({total_zero / max(total_words, 1):.1%})")
    print(f"words in >1 box     : {total_multi} ({total_multi / max(total_words, 1):.1%})")
    print(f"\n-- overlap rule (word area inside box >= {args.overlap_threshold}) --")
    print(f"words in NO box     : {ov_zero} ({ov_zero / max(total_words, 1):.1%})")
    print(f"words in >1 box     : {ov_multi} ({ov_multi / max(total_words, 1):.1%})")
    print(
        f"\nrules DISAGREE on   : {total_disagree} word(s) "
        f"({total_disagree / max(total_words, 1):.2%})  <- inspect these before choosing"
    )
    print(f"median page unassigned share (centroid): {statistics.median(zero_shares):.1%}")
    worst = sorted(rows, key=lambda r: -(r["zero"] / max(r["words"], 1)))[:5]
    print("\nworst pages by unassigned share:")
    for r in worst:
        print(f"  {r['pdf']} p{r['page']}: {r['zero']}/{r['words']} unassigned, {r['boxes']} boxes")
    print(f"\nwrote {out_path}")
    print(
        "\nread this as: unassigned should be a few percent (decorative/rotated "
        "runs). Tens of percent means the coordinate spaces are misaligned and "
        "the emit-stage scores below are not trustworthy."
    )
    return 0


# --------------------------------------------------------------------------
# stage: overlay
# --------------------------------------------------------------------------


LABEL_COLORS = {
    "text": (0.13, 0.45, 0.85),
    "section_header": (0.85, 0.25, 0.10),
    "title": (0.85, 0.25, 0.10),
    "table": (0.10, 0.60, 0.30),
    "picture": (0.60, 0.30, 0.75),
    "page_header": (0.55, 0.55, 0.55),
    "page_footer": (0.55, 0.55, 0.55),
    "list_item": (0.90, 0.60, 0.10),
    "caption": (0.10, 0.65, 0.70),
    "footnote": (0.45, 0.45, 0.20),
}


def stage_overlay(args: argparse.Namespace) -> int:
    """Render each cached page with docling's boxes drawn over it.

    Every box is stroked in a label-specific colour and tagged with its index
    in docling's reading order, so a wrong ORDER and a wrong BOX are visually
    distinguishable -- they are different failures with different fixes.
    """
    import fitz

    cache_dir = args.work_dir / "docling_json"
    out_dir = args.work_dir / "overlays"
    out_dir.mkdir(parents=True, exist_ok=True)

    caches = sorted(cache_dir.glob("*.pages.json"))
    if args.limit:
        caches = caches[: args.limit]
    if not caches:
        print(f"no docling cache under {cache_dir}; run `convert` first", file=sys.stderr)
        return 1

    written = 0
    for cache_path in caches:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        matches = list(args.pdf_dir.rglob(cached["pdf_file"]))
        if not matches:
            continue
        doc = fitz.open(str(matches[0]))
        for page_key, items in cached.get("pages", {}).items():
            page_no = int(page_key)
            if not 1 <= page_no <= doc.page_count:
                continue
            page = doc[page_no - 1]
            boxes = [(it.get("bbox"), it.get("label", "unknown")) for it in items if it.get("bbox")]
            if not boxes:
                continue

            scale = 1.0

            for order, (bbox, label) in enumerate(boxes, start=1):
                color = LABEL_COLORS.get(label, (0.2, 0.2, 0.2))
                r = fitz.Rect(
                    bbox[0] * scale, bbox[1] * scale, bbox[2] * scale, bbox[3] * scale
                )
                if page.rotation:
                    # draw_rect/insert_text on a Page place coordinates in raw
                    # mediabox space, but docling's bbox is in the
                    # rotated/displayed space page.rect reports. Map it back
                    # before drawing, or the box lands as a thin, misplaced
                    # sliver on any rotated page (seen on MOV-2021, /Rotate 90
                    # throughout).
                    r = r * page.derotation_matrix
                    r.normalize()
                page.draw_rect(r, color=color, width=1.2)
                page.insert_text(
                    (r.x0 + 2, max(r.y0 - 3, 8)),
                    f"{order}:{label}",
                    fontsize=7,
                    color=color,
                )

            # Table grid overlay: draw the row/column cuts the fuse stage
            # actually uses, so a cell landing in the wrong row is visible as
            # a line in the wrong place rather than inferred from text.
            if getattr(args, "show_grid", False):
                for item in items:
                    grid = item.get("grid")
                    tb = item.get("bbox")
                    if not grid or not tb:
                        continue
                    if not _grid_is_coherent(grid):
                        page.draw_rect(fitz.Rect(*tb), color=(0.6, 0.6, 0.6), width=1.6)
                        page.insert_text((tb[0] + 2, tb[1] - 4), "GRID DECLINED (incoherent)",
                                         fontsize=8, color=(0.6, 0.6, 0.6))
                        continue

                    # Draw the CELL boxes, because those are what the fuse
                    # stage assigns words into. Drawing the cut grid here would
                    # show a structure the code no longer uses.
                    for cell in grid["cells"]:
                        cb_ = cell.get("bbox")
                        if not cb_:
                            continue
                        span = (cell["r1"] - cell["r0"] > 1) or (cell["c1"] - cell["c0"] > 1)
                        colr = (0.55, 0.15, 0.75) if span else (0.85, 0.1, 0.1)
                        page.draw_rect(fitz.Rect(*cb_), color=colr, width=0.7)
                        tag = f"{cell['r0']},{cell['c0']}"
                        if span:
                            tag += f" span{cell['r1']-cell['r0']}x{cell['c1']-cell['c0']}"
                        page.insert_text((cb_[0] + 1.5, cb_[1] + 7), tag, fontsize=5, color=colr)

                    # Faint fallback grid: where words landing outside every
                    # cell get placed.
                    rb, cb = _grid_bands(grid)
                    for y in _monotonic([rb[i + 1][0] - 0.5 for i in range(len(rb) - 1)]):
                        page.draw_line(fitz.Point(tb[0], y), fitz.Point(tb[2], y),
                                       color=(0.75, 0.8, 0.85), width=0.4)
                    for x in _col_cuts(cb):
                        page.draw_line(fitz.Point(x, tb[1]), fitz.Point(x, tb[3]),
                                       color=(0.75, 0.8, 0.85), width=0.4)
                    page.draw_rect(fitz.Rect(*tb), color=(0.0, 0.35, 0.9), width=1.6)

            suffix = "_grid" if getattr(args, "show_grid", False) else ""
            out_path = out_dir / f"{cached['pdf_stem']}_p{page_no}{suffix}.png"
            page.get_pixmap(dpi=args.dpi).save(str(out_path))
            written += 1
            print(f"wrote {out_path}")
        doc.close()

    print(f"\n{written} overlay image(s) in {out_dir}")
    print(
        "reading guide: numbers are docling's reading order. Colours are its "
        "labels (grey = page_header/page_footer, i.e. what --drop-furniture "
        "removes). A box in the right place with the wrong number is an ORDER "
        "problem; a box in the wrong place is a DETECTION problem."
    )
    return 0


# --------------------------------------------------------------------------
# stage: fuse
# --------------------------------------------------------------------------


def _scale_for_page(boxes: list[list[float]], rect: Any) -> float:
    """Scale from docling page space to PDF points.

    Docling reports page size in PDF points, identical to PyMuPDF's page rect
    (verified: 864x540 both ways). So this is 1.0.

    It previously derived a scale from max(bbox.right)/max(bbox.bottom) as a
    stand-in for page size. Content never reaches the page edge, so that ratio
    was always >1 and pushed every box right and down -- visible as boxes
    sitting low and right of their text.
    """
    return 1.0


# Labels whose visual line breaks carry meaning and must survive reflow.
#
# "picture" is here because a chart's contents are laid out in 2D: on KSS p16
# the title "ANNUAL GREENHOUSE GAS EMISSIONS" has its legend ("Scope 1",
# "Scope 2") sitting to the right, so reading line by line interleaves them.
# Reflowing then welds the pieces into one line that reads like a sentence and
# means nothing:
#
#   'ANNUAL GREENHOUSE Scope 1 GAS EMISSIONS (Calendar Year) Scope 2 % Change'
#
# Keeping the breaks leaves fragments instead, which is worse-looking and less
# harmful -- a fragment is obviously incomplete, a fake sentence is not.
KEEP_LINE_BREAKS = {"list_item", "table", "code", "formula", "picture"}


def _join_lines(rows: list[list[tuple]], gap_factor: float = 1.6) -> str:
    """Reflow visual lines into paragraphs.

    A PDF wraps a paragraph into short lines; preserving those breaks left 86%
    of corpus lines ending mid-sentence, which makes every wrapped line look
    like a standalone heading and puts hard breaks inside chunk text.

    A region is one block, so its lines belong to one paragraph unless the
    vertical gap between them is markedly larger than the usual line spacing --
    that gap is a real paragraph break. A line ending in a hyphen is joined
    without a space, since the word was split by the wrap.
    """
    if not rows:
        return ""

    def mid(row: list[tuple]) -> float:
        return (row[0][1] + row[0][3]) / 2

    def text_of(row: list[tuple]) -> str:
        return " ".join(w[4] for w in sorted(row, key=lambda w: w[0]))

    gaps = [mid(rows[i + 1]) - mid(rows[i]) for i in range(len(rows) - 1)]
    typical = statistics.median(gaps) if gaps else 0.0

    out = text_of(rows[0])
    for i in range(1, len(rows)):
        line = text_of(rows[i])
        if typical and (mid(rows[i]) - mid(rows[i - 1])) > typical * gap_factor:
            out += "\n" + line
        elif out.endswith("-"):
            out = out[:-1] + line
        else:
            out += " " + line
    return out


def _words_to_lines(words: list[tuple], tol: float = 3.0, label: str = "") -> str:
    """Order words within one region: top-to-bottom, then left-to-right.

    Deliberately NOT using PyMuPDF's own block/line numbering -- that numbering
    is the thing that scrambles multi-column pages, and discarding it is the
    entire point of letting docling define the region.

    Lines are reflowed into paragraphs unless the label says the breaks matter
    (a list, a table, code). ``label`` may carry a band suffix, so only the
    part before "|" is compared.
    """
    if not words:
        return ""
    rows: list[list[tuple]] = []
    for word in sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0])):
        mid = (word[1] + word[3]) / 2
        if rows and abs(((rows[-1][0][1] + rows[-1][0][3]) / 2) - mid) <= tol:
            rows[-1].append(word)
        else:
            rows.append([word])

    if label.split("|")[0] in KEEP_LINE_BREAKS:
        return chr(10).join(
            " ".join(w[4] for w in sorted(row, key=lambda w: w[0])) for row in rows
        )
    return _join_lines(rows)


def _grid_bands(grid: dict[str, Any]) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Derive row and column bands from the cells that span exactly one slot.

    Spanning cells are excluded on purpose. A cell docling believes covers two
    rows carries one tall box; using it would reproduce the span. Bands taken
    only from single-slot cells describe where the rows and columns actually
    sit, so a row that is genuinely empty gets an empty band rather than
    inheriting its neighbour's text.
    """
    rows: dict[int, list[float]] = defaultdict(list)
    cols: dict[int, list[float]] = defaultdict(list)
    for cell in grid["cells"]:
        b = cell.get("bbox")
        if not b:
            continue
        if cell["r1"] - cell["r0"] == 1:
            rows[cell["r0"]].append((b[1], b[3]))
        if cell["c1"] - cell["c0"] == 1:
            cols[cell["c0"]].append((b[0], b[2]))

    def bands(known: dict[int, list[float]], n: int) -> list[tuple[float, float]]:
        out: list[tuple[float, float]] = []
        for i in range(n):
            vals = known.get(i)
            if vals:
                # Median, not min/max. One outlier cell whose box is wider than
                # its column drags a min/max band across its neighbour -- on
                # LOVE p46 that put a column cut in the middle of column 4.
                out.append(
                    (
                        statistics.median([v[0] for v in vals]),
                        statistics.median([v[1] for v in vals]),
                    )
                )
            else:
                out.append((float("nan"), float("nan")))
        # Fill any band with no single-slot cell from its neighbours.
        for i in range(n):
            if out[i][0] == out[i][0]:
                continue
            prev = next((out[j] for j in range(i - 1, -1, -1) if out[j][0] == out[j][0]), None)
            nxt = next((out[j] for j in range(i + 1, n) if out[j][0] == out[j][0]), None)
            if prev and nxt:
                out[i] = (prev[1], nxt[0])
            elif prev:
                out[i] = (prev[1], prev[1])
            elif nxt:
                out[i] = (nxt[0], nxt[0])
            else:
                out[i] = (0.0, 0.0)
        return out

    return bands(rows, grid["num_rows"]), bands(cols, grid["num_cols"])


def _monotonic(cuts: list[float], eps: float = 0.01) -> list[float]:
    """Force cut points to strictly increase.

    Bands are derived per row/column independently, so nothing guarantees they
    come out in order. On DECK p169 two adjacent columns shared a left edge and
    produced identical cuts, giving one column a zero-width slot that could
    never receive a word.
    """
    out: list[float] = []
    for c in cuts:
        if out and c <= out[-1]:
            c = out[-1] + eps
        out.append(c)
    return out


def _col_cuts(bands: list[tuple[float, float]]) -> list[float]:
    """Column boundaries.

    Columns do NOT need the row rule. A row must own its overflow because a
    tall cell's text hangs below its own band; text inside a column wraps
    instead of spilling sideways, so the natural boundary is the gutter.

    Bands frequently overlap here -- a cell whose box is wider than its column
    drags the band into its neighbour -- so when the gutter is negative, split
    the two LEFT edges instead, which stay ordered even when the widths do not.
    """
    cuts: list[float] = []
    for i in range(len(bands) - 1):
        right, nxt_left = bands[i][1], bands[i + 1][0]
        if nxt_left > right:
            # 90% of the way across the gutter, mirroring the row rule: a
            # column keeps text that trails past its own band, and only text
            # essentially touching the next column goes to the next column.
            cuts.append(right + COL_CUT_SHARE * (nxt_left - right))
        else:
            # Bands overlap; the gutter is meaningless. Split the two LEFT
            # edges instead -- those stay ordered even when widths do not.
            cuts.append(
                bands[i][0] + COL_CUT_SHARE * (bands[i + 1][0] - bands[i][0])
            )
    return _monotonic(cuts)


def _grid_is_coherent(grid: dict[str, Any]) -> bool:
    """Does this grid describe a real column layout?

    TableFormer sometimes returns a cell-to-column assignment that does not
    correspond to any consistent x position -- on PVH p55 cells assigned to
    column 2 start at both x=84 and x=784, left of column 0. No cut rule can
    recover a grid from that, so the honest move is to decline and let the
    region render as ordered words instead of inventing a structure.

    Test: the median left edge per column must increase left to right. That
    tolerates ragged cells while catching a genuinely scrambled assignment.
    """
    import statistics as _st

    lefts: dict[int, list[float]] = defaultdict(list)
    for cell in grid["cells"]:
        b = cell.get("bbox")
        if b and cell["c1"] - cell["c0"] == 1:
            lefts[cell["c0"]].append(b[0])
    med = [_st.median(lefts[i]) for i in sorted(lefts) if lefts[i]]
    if len(med) < 2:
        return True
    return all(b > a for a, b in zip(med, med[1:]))


def _table_from_grid(grid: dict[str, Any], words: list[tuple], assign: str = "cell") -> str:
    """Rebuild a table. ``assign`` selects how words are placed in cells.

    Four strategies, kept switchable because each fails differently and the
    failures are only visible on different pages:

    ``cell``   docling's own cell boxes, smallest containing cell wins, grid
               cuts as fallback for words outside every cell. Handles rows
               whose height differs per column, which a single grid cannot.
    ``strict`` grid slots only; a word matching no row band AND col band is
               DROPPED. Gave the cleanest empty rows but silently lost 13-14%
               of the words on LOVE p46 and DECK p169.
    ``snap``   grid slots, unmatched words go to the nearest band. Stops the
               loss but pushes a tall cell's overflow into the following row.
    ``cuts``   grid slots from tiling cut points, so every point belongs to
               exactly one slot. No loss, no snapping.
    """
    if not _grid_is_coherent(grid):
        return ""

    row_bands, col_bands = _grid_bands(grid)
    n_rows, n_cols = grid["num_rows"], grid["num_cols"]
    if n_rows < 1 or n_cols < 1:
        return ""

    row_cuts = _monotonic([row_bands[i + 1][0] - 0.5 for i in range(len(row_bands) - 1)])
    col_cuts = _col_cuts(col_bands)

    def by_cuts(cut_points: list[float], v: float) -> int:
        i = 0
        while i < len(cut_points) and v > cut_points[i]:
            i += 1
        return i

    def in_band(bands: list[tuple[float, float]], v: float) -> int | None:
        for i, (a, b) in enumerate(bands):
            if a - 1 <= v <= b + 1:
                return i
        return None

    def nearest_band(bands: list[tuple[float, float]], v: float) -> int:
        hit = in_band(bands, v)
        if hit is not None:
            return hit
        return min(range(len(bands)), key=lambda i: min(abs(v - bands[i][0]), abs(v - bands[i][1])))

    boxed = [c for c in grid["cells"] if c.get("bbox")]
    buckets: dict[tuple[int, int], list[tuple]] = defaultdict(list)
    dropped = 0

    for word in words:
        cx, cy = (word[0] + word[2]) / 2, (word[1] + word[3]) / 2

        if assign == "cell":
            best, best_area = None, float("inf")
            for cell in boxed:
                b = cell["bbox"]
                if b[0] <= cx <= b[2] and b[1] <= cy <= b[3]:
                    area = (b[2] - b[0]) * (b[3] - b[1])
                    if area < best_area:
                        best, best_area = cell, area
            if best is not None:
                buckets[(best["r0"], best["c0"])].append(word)
                continue
            r, c = by_cuts(row_cuts, cy), by_cuts(col_cuts, cx)
        elif assign == "strict":
            ri, ci = in_band(row_bands, cy), in_band(col_bands, cx)
            if ri is None or ci is None:
                dropped += 1
                continue
            r, c = ri, ci
        elif assign == "snap":
            r, c = nearest_band(row_bands, cy), nearest_band(col_bands, cx)
        else:  # cuts
            r, c = by_cuts(row_cuts, cy), by_cuts(col_cuts, cx)

        buckets[(min(r, n_rows - 1), min(c, n_cols - 1))].append(word)

    def cell_text(bucket: list[tuple]) -> str:
        if not bucket:
            return ""
        return " ".join(
            w[4] for w in sorted(bucket, key=lambda w: ((w[1] + w[3]) / 2, w[0]))
        ).replace("|", r"\|")

    header_rows = {c["r0"] for c in grid["cells"] if c.get("header")}
    lines = []
    for r in range(n_rows):
        lines.append(
            "| " + " | ".join(cell_text(buckets.get((r, c), [])) for c in range(n_cols)) + " |"
        )
        if r in header_rows and r + 1 not in header_rows:
            lines.append("|" + "|".join([" --- "] * n_cols) + "|")
    return chr(10).join(lines)


def fuse_page(pdf_path: Path, page_no: int, regions: list[dict[str, Any]], snap_limit: float = 12.0, table_mode: str = "words", table_assign: str = "cell") -> dict[str, Any]:
    """Docling decides the regions and their order; PyMuPDF supplies the text."""
    import fitz

    doc = fitz.open(str(pdf_path))
    page = doc[page_no - 1]
    words = list(page.get_text("words"))
    if page.rotation:
        # get_text("words") reports coordinates in raw, un-rotated mediabox
        # space, while docling's region boxes are in the rotated/displayed
        # page space page.rect already reflects. Left unreconciled, a rotated
        # page's words and regions live in two different coordinate frames
        # and almost nothing lands inside a box -- verified on MOV-2021 (every
        # page /Rotate 90), which held 60-83% of its words unplaced for
        # exactly this reason, not because docling missed the text.
        matrix = page.rotation_matrix
        rotated = []
        for word in words:
            r = fitz.Rect(word[0], word[1], word[2], word[3]) * matrix
            r.normalize()
            rotated.append((r.x0, r.y0, r.x1, r.y1, *word[4:]))
        words = rotated
    page_height = page.rect.height
    page_width = page.rect.width
    boxed = [r for r in regions if r.get("bbox")]
    scale = _scale_for_page([r["bbox"] for r in boxed], page.rect)

    scaled = [
        (r["bbox"][0] * scale, r["bbox"][1] * scale, r["bbox"][2] * scale, r["bbox"][3] * scale)
        for r in boxed
    ]

    buckets: list[list[tuple]] = [[] for _ in boxed]
    leftover: list[tuple] = []
    for word in words:
        cx, cy = (word[0] + word[2]) / 2, (word[1] + word[3]) / 2
        hit = None
        for idx, (bx0, by0, bx1, by1) in enumerate(scaled):
            if bx0 <= cx <= bx1 and by0 <= cy <= by1:
                hit = idx
                break

        # Docling draws its boxes tight, so the first or last word on a line
        # often has its centroid a point or two outside. Dropping those to the
        # tail block preserves them for recall but destroys sentence order --
        # which is the whole reason to fuse. Snap a near-miss to the closest
        # box instead, and only give up beyond SNAP_LIMIT.
        if hit is None:
            best, best_d = None, snap_limit
            for idx, (bx0, by0, bx1, by1) in enumerate(scaled):
                dx = max(bx0 - cx, 0.0, cx - bx1)
                dy = max(by0 - cy, 0.0, cy - by1)
                dist = (dx * dx + dy * dy) ** 0.5
                if dist < best_d:
                    best, best_d = idx, dist
            hit = best

        (buckets[hit] if hit is not None else leftover).append(word)
    doc.close()

    # Tag, do not drop. Whether a band region is furniture is decided by the
    # already-validated repetition rule in esg_navigation, which needs the whole
    # document; a single page cannot tell a running footer from a real line that
    # happens to sit low (region 9 on ORLY p12 is content, and is in the band).
    # Docling's own labels do not help here -- it calls nav ribbons "text".
    band = page_height * HEADER_FOOTER_BAND_SHARE
    side_band = page_width * SIDE_BAND_SHARE

    blocks: list[str] = []
    for idx, region in enumerate(boxed):
        label = region.get("label", "text")
        b = region["bbox"]
        if b[3] * scale <= band:
            label += "|band=header"
        elif b[1] * scale >= page_height - band:
            label += "|band=footer"
        elif b[2] * scale <= side_band:
            label += "|band=left"
        elif b[0] * scale >= page_width - side_band:
            label += "|band=right"
        text = ""
        if table_mode == "grid" and label.startswith("table") and region.get("grid"):
            # Empty means the grid was declined as incoherent. Fall back to
            # ordered words -- NOT to the empty-region placeholder, which would
            # drop the whole table.
            text = _table_from_grid(region["grid"], buckets[idx], table_assign)
        if not text:
            text = _words_to_lines(buckets[idx], label=label)
        if not text:
            # A picture with no words under it stays a picture. Nothing to fuse.
            blocks.append(f"[{idx + 1}:{label}] (no text layer in this region)")
            continue
        prefix = "## " if label in {"section_header", "title"} else ""
        blocks.append(f"[{idx + 1}:{label}]\n{prefix}{text}")

    tail = _words_to_lines(leftover, label="list_item")
    return {
        "fused_text": "\n\n".join(blocks) + (f"\n\n[unplaced words]\n{tail}" if tail else ""),
        "region_count": len(boxed),
        "placed_words": sum(len(b) for b in buckets),
        "unplaced_words": len(leftover),
        "total_words": len(words),
    }


def fusion_settings(args: argparse.Namespace) -> dict[str, Any]:
    """The knobs that change fused text, and therefore bound page reuse.

    A fused page carries no record of how it was made. Without this, a run
    with --table-mode words silently inherits grid-mode pages and the corpus
    becomes a mix of two renderings that no later stage can tell apart.
    """
    return {
        "table_mode": args.table_mode,
        "table_assign": args.table_assign,
        "snap": args.snap,
    }


def page_is_verifiable(entry: Any) -> bool:
    """Does this summary entry carry the word counts that vouch for a page?

    Placement counts are the only thing separating a fused page from a
    fused-looking one: FLEXSTEEL-2024 passed the smoke test with no error
    while 98% of its words landed in no region.
    """
    return isinstance(entry, dict) and "placed_words" in entry


def stage_fuse(args: argparse.Namespace) -> int:
    cache_dir = args.layout_dir or args.work_dir / "docling_json"
    out_dir = args.fused_dir or args.work_dir / "fused"
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = args.fused_summary or args.work_dir / "fused_summary.json"
    settings_path = summary_path.with_suffix(".settings.json")
    settings = fusion_settings(args)
    previous_results = {}
    if summary_path.is_file() and not args.force:
        try:
            previous_results = json.loads(summary_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A damaged summary must never make a fused page look complete.
            previous_results = {}

    recorded = None
    if settings_path.is_file():
        try:
            recorded = json.loads(settings_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            recorded = None

    if recorded is None:
        # Pages fused before settings were recorded are unverifiable, not
        # wrong. Adopting them keeps an already-validated corpus intact rather
        # than discarding hours of work to learn what produced it. Every run
        # from here on records its settings, so this happens once.
        reusable = True
        print(f"fusion settings {settings}: adopting the fused pages already on disk")
    elif recorded == settings:
        reusable = True
    else:
        reusable = False
        changed = {
            key: f"{recorded.get(key)!r} -> {value!r}"
            for key, value in settings.items()
            if recorded.get(key) != value
        }
        print(f"fusion settings changed ({changed}); re-fusing every page")

    # Whole-document mode: when there is no gold-driven page selection, fuse
    # every page present in the cache. This is the path for running the
    # pipeline on arbitrary PDFs rather than benchmark pages.
    selected = args.work_dir / "selected_page_text.jsonl"
    if selected.exists():
        emitted = {
            json.loads(l)["item_id"]: json.loads(l)
            for l in selected.read_text(encoding="utf-8").splitlines()
            if l.strip()
        }
    else:
        emitted = {}
        for cache_path in sorted(cache_dir.glob("*.pages.json")):
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            for page_key in sorted(cached.get("pages", {}), key=lambda k: int(k)):
                item_id = f"{cached['pdf_stem']}_p{page_key}"
                emitted[item_id] = {
                    "pdf_file": cached["pdf_file"],
                    "pdf_stem": cached["pdf_stem"],
                    "page": int(page_key),
                }
        print(f"whole-document mode: {len(emitted)} page(s) from {cache_dir}")

    wanted = [i.strip() for i in args.items.split(",") if i.strip()] if args.items else list(emitted)
    if args.limit:
        wanted = wanted[: args.limit]

    results = {}
    reused = 0
    for item_id in wanted:
        record = emitted.get(item_id)
        if not record:
            print(f"skip {item_id}: not in selected_page_text.jsonl", file=sys.stderr)
            continue
        cache = cache_dir / f"{record['pdf_stem']}.pages.json"
        if not cache.exists():
            continue
        regions = json.loads(cache.read_text(encoding="utf-8")).get("pages", {}).get(str(record["page"]), [])
        matches = list(args.pdf_dir.rglob(record["pdf_file"]))
        if not matches:
            continue
        path = out_dir / f"{item_id}.txt"
        prior = previous_results.get(item_id)
        # A page is reusable only if it was made the same way AND its word
        # counts survived. Re-fusing an unverifiable page costs one page;
        # keeping it costs the evidence that would have condemned it.
        if (
            reusable
            and not args.force
            and path.is_file()
            and path.stat().st_size > 0
            and page_is_verifiable(prior)
        ):
            results[item_id] = prior
            reused += 1
            print(f"{item_id:26s} reused -> {path.name}")
            continue
        out = fuse_page(matches[0], record["page"], regions, args.snap, args.table_mode, args.table_assign)
        results[item_id] = out
        path.write_text(out["fused_text"], encoding="utf-8")
        print(
            f"{item_id:26s} regions={out['region_count']:3d} "
            f"placed={out['placed_words']:4d} unplaced={out['unplaced_words']:4d} "
            f"of {out['total_words']:4d} words -> {path.name}"
        )

    # Atomic, and the summary before the settings: a kill between the two
    # leaves settings unrecorded, which re-adopts on the next run. The other
    # order would vouch for pages whose telemetry was never written.
    _write_json_atomic(summary_path, results)
    _write_json_atomic(settings_path, settings)
    print(f"\n{len(results)} page(s) in {out_dir} ({reused} reused)")
    return 0


# --------------------------------------------------------------------------
# stage: review
# --------------------------------------------------------------------------


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def stage_review(args: argparse.Namespace) -> int:
    """Build an HTML page: overlay image + region list + both verdicts.

    Defaults to the pages where the two parsers DISAGREE, because those are the
    ones that decide whether a fused parser can take the better behaviour on
    each page. ``--all-pages`` shows the full sample.
    """
    import csv as _csv

    cache_dir = args.work_dir / "docling_json"
    overlays = args.work_dir / "overlays"

    def load_scores(path: Path) -> dict[str, dict[str, str]]:
        if not path.exists():
            return {}
        with path.open(encoding="utf-8") as handle:
            return {r["item_id"]: r for r in _csv.DictReader(handle)}

    docling_scores = load_scores(args.docling_scores)
    current_scores = load_scores(args.current_scores)

    gold = {
        json.loads(line)["item_id"]: json.loads(line)
        for line in args.gold.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    emitted = {
        json.loads(line)["item_id"]: json.loads(line)
        for line in (args.work_dir / "selected_page_text.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    }

    ok = lambda row: row and row["outcome"].startswith(("pass", "excluded"))
    items = []
    for item_id, record in emitted.items():
        d_row = docling_scores.get(item_id)
        c_row = current_scores.get(item_id)
        divergent = ok(d_row) != ok(c_row)
        if not args.all_pages and not divergent:
            continue
        items.append((item_id, record, d_row, c_row, divergent))

    # Disagreements first, then everything else.
    items.sort(key=lambda t: (not t[4], t[0]))
    if not items:
        print("nothing to review (run emit and both evaluator passes first)", file=sys.stderr)
        return 1

    parts = [
        "<title>Docling spike — region review</title>",
        "<style>",
        "body{font:14px/1.5 system-ui,sans-serif;margin:0;padding:24px;background:#f6f7f9;color:#111}",
        "@media(prefers-color-scheme:dark){body{background:#14161a;color:#e8e8ea}"
        ".card{background:#1d2026!important;border-color:#333!important}"
        "pre{background:#0f1114!important;color:#d8d8dc!important}}",
        ".card{background:#fff;border:1px solid #dcdfe4;border-radius:8px;padding:18px;margin:0 0 26px}",
        "h2{margin:0 0 4px;font-size:17px}",
        ".meta{color:#666;font-size:12px;margin-bottom:12px}",
        ".cols{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);gap:18px}",
        "@media(max-width:900px){.cols{grid-template-columns:1fr}}",
        "img{max-width:100%;border:1px solid #ccc;border-radius:4px}",
        "pre{background:#f0f1f4;padding:10px;border-radius:5px;overflow-x:auto;font-size:12px;max-height:340px}",
        "table{border-collapse:collapse;width:100%;font-size:12px}",
        "td,th{border-bottom:1px solid #e2e4e8;padding:4px 6px;text-align:left;vertical-align:top}",
        ".ok{color:#0a7d33;font-weight:600}.bad{color:#b3261e;font-weight:600}",
        ".tag{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;background:#e8eaee;margin-right:5px}",
        "</style>",
        "<h1>Docling spike — region review</h1>",
        f"<p class=meta>{len(items)} page(s). "
        "Numbers on each image are docling's reading order; colours are its labels. "
        "A box in the right place with the wrong number is an ORDER problem; "
        "a box in the wrong place is a DETECTION problem.</p>",
    ]

    for item_id, record, d_row, c_row, divergent in items:
        stem = record["pdf_stem"]
        page = record["page"]
        img = overlays / f"{stem}_p{page}.png"
        cached = cache_dir / f"{stem}.pages.json"
        regions = []
        if cached.exists():
            regions = json.loads(cached.read_text(encoding="utf-8")).get("pages", {}).get(str(page), [])

        def verdict(row: dict[str, str] | None, name: str) -> str:
            if not row:
                return f"{name}: <span class=meta>no score</span>"
            css = "ok" if ok(row) else "bad"
            reason = row.get("failure_reasons") or ""
            return (
                f"{name}: <span class={css}>{_esc(row['outcome'])}</span>"
                + (f" <span class=meta>{_esc(reason)}</span>" if reason else "")
            )

        rows = "".join(
            "<tr><td>{}</td><td><span class=tag>{}</span></td><td>{}</td><td>{}</td></tr>".format(
                i,
                _esc(r.get("label", "?")),
                _esc(", ".join(str(round(v)) for v in r["bbox"])) if r.get("bbox") else "-",
                _esc((r.get("text") or "")[:160].replace("\n", " ")),
            )
            for i, r in enumerate(regions, start=1)
        )

        gold_md = (gold.get(item_id, {}).get("reference_markdown") or "")[:1400]
        parts.append(
            f"""<div class=card>
<h2>{_esc(item_id)}{' &nbsp;<span class=tag>DIVERGENT</span>' if divergent else ''}</h2>
<p class=meta>{_esc(record['pdf_file'])} &middot; page {page} &middot;
{_esc(gold.get(item_id, {}).get('page_type', '?'))} &middot;
{_esc(gold.get(item_id, {}).get('sample_category', '?'))}<br>
{verdict(d_row, 'docling')}<br>{verdict(c_row, 'current')}</p>
<div class=cols>
  <div><img src="../overlays/{_esc(img.name)}" alt="{_esc(item_id)}"></div>
  <div>
    <b>docling regions ({len(regions)})</b>
    <table><tr><th>#</th><th>label</th><th>bbox</th><th>text</th></tr>{rows}</table>
    <b>gold reference</b>
    <pre>{_esc(gold_md)}</pre>
  </div>
</div></div>"""
        )

    out_dir = args.work_dir / "review"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "index.html"
    out_path.write_text("\n".join(parts), encoding="utf-8")
    print(f"wrote {out_path}  ({len(items)} page(s))")
    print("open it in a browser; images are referenced from ../overlays/")
    return 0


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stage", choices=["convert", "emit", "fusecheck", "overlay", "review", "fuse"])
    parser.add_argument(
        "--pdf-dir",
        type=Path,
        default=Path("outputs/esg_ai_gold_parser_20260731/input_pdfs"),
        help="directory searched recursively for source PDFs",
    )
    parser.add_argument(
        "--gold",
        type=Path,
        default=Path("data/00_reference/esg_ai_gold_v1.jsonl"),
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path("outputs/docling_spike"),
        help="all output lands here; nothing else is written",
    )
    parser.add_argument(
        "--layout-dir",
        type=Path,
        help="convert/fuse: Docling JSON cache directory (default: <work-dir>/docling_json)",
    )
    parser.add_argument(
        "--fused-dir",
        type=Path,
        help="fuse: fused page-text directory (default: <work-dir>/fused)",
    )
    parser.add_argument(
        "--fused-summary",
        type=Path,
        help="fuse: fused page summary path (default: <work-dir>/fused_summary.json)",
    )
    parser.add_argument("--split", default="development", help="gold split to emit ('' for all)")
    parser.add_argument("--limit", type=int, default=0, help="cap documents processed")
    parser.add_argument("--force", action="store_true", help="re-convert cached documents")
    parser.add_argument(
        "--gold-pages",
        action="store_true",
        help="convert only the pages the gold set scores (fast local decision test)",
    )
    parser.add_argument(
        "--drop-furniture",
        action="store_true",
        help="drop docling page_header/page_footer items in emit",
    )
    parser.add_argument("--pad", type=float, default=2.0, help="bbox padding in points for fusecheck")
    parser.add_argument(
        "--overlap-threshold",
        type=float,
        default=0.5,
        help="fusecheck: fraction of a word's area inside a box to count as assigned",
    )
    parser.add_argument(
        "--max-disagreements",
        type=int,
        default=25,
        help="fusecheck: per-page cap on recorded centroid/overlap disagreements",
    )
    parser.add_argument("--dpi", type=int, default=130, help="overlay: render resolution")
    parser.add_argument("--show-grid", action="store_true", help="overlay: draw table row/column cut lines")
    parser.add_argument("--table-assign", choices=["cell", "strict", "snap", "cuts"], default="cell",
                        help="fuse: how words are placed into table cells")
    parser.add_argument("--col-cut-share", type=float, default=0.9,
                        help="fuse: where a column boundary sits across the gutter (0.5=middle, 0.9=just before next column)")
    parser.add_argument(
        "--auto-ocr",
        action="store_true",
        help="convert: switch OCR off per-document when the PDF already has a text layer",
    )
    parser.add_argument("--items", default="", help="fuse: comma-separated item_ids")
    parser.add_argument("--table-mode", choices=["words", "grid"], default="words", help="fuse: how to render table regions")
    parser.add_argument("--pages", default="", help='convert: "file.pdf:1,2,3;other.pdf:9"')
    parser.add_argument("--random-pages", type=int, default=0, help="convert: add N random pages")
    parser.add_argument("--seed", type=int, default=0, help="seed for --random-pages")
    parser.add_argument("--heading-hierarchy", action="store_true", help="enable docling heading hierarchy")
    parser.add_argument("--backend-text", action="store_true", help="force_backend_text=True")
    parser.add_argument("--full-page-ocr", action="store_true", help="OCR the whole page image")
    parser.add_argument("--skip-cells", action="store_true", help="layout boxes only, no text assignment")
    parser.add_argument(
        "--no-auto-skip-cells",
        action="store_true",
        help="convert Type3 documents the old way (they will contribute no chunks)",
    )
    parser.add_argument("--snap", type=float, default=12.0, help="fuse: max points to snap a near-miss word to its closest region")
    parser.add_argument("--all-pages", action="store_true", help="review: show every page, not just divergent ones")
    parser.add_argument(
        "--docling-scores",
        type=Path,
        default=Path("reports/docling_spike_benchmark/page_scores.csv"),
    )
    parser.add_argument(
        "--current-scores",
        type=Path,
        default=Path("reports/docling_spike_baseline/page_scores.csv"),
    )
    parser.add_argument(
        "--no-ocr",
        action="store_true",
        help="disable docling OCR of picture regions (faster; text-layer PDFs only)",
    )
    parser.add_argument(
        "--shards",
        type=int,
        default=1,
        help="split the document list across this many parallel processes",
    )
    parser.add_argument(
        "--shard",
        type=int,
        default=0,
        help="which shard this process handles (0-based)",
    )
    parser.add_argument(
        "--time-budget-min",
        type=float,
        default=0.0,
        help="convert: stop cleanly at a document boundary after this many "
        "minutes (0 = no limit)",
    )
    args = parser.parse_args(argv)

    # Module-level because the cut helpers are called from several places.
    global COL_CUT_SHARE
    COL_CUT_SHARE = args.col_cut_share

    args.work_dir.mkdir(parents=True, exist_ok=True)
    if args.layout_dir:
        args.layout_dir.mkdir(parents=True, exist_ok=True)
    if args.fused_dir:
        args.fused_dir.mkdir(parents=True, exist_ok=True)
    if args.fused_summary:
        args.fused_summary.parent.mkdir(parents=True, exist_ok=True)
    stages = {
        "convert": stage_convert,
        "emit": stage_emit,
        "fusecheck": stage_fusecheck,
        "overlay": stage_overlay,
        "review": stage_review,
        "fuse": stage_fuse,
    }
    return stages[args.stage](args)


if __name__ == "__main__":
    raise SystemExit(main())
