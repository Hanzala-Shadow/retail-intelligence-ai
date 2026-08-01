"""Persistent spatial regions and unruled-table structure for one PDF page.

``esg_order_safety`` used to derive a page's structure from
``esg_reading_order._cluster_starts``. That function answers a different
question -- "does this page have full-height prose columns?" -- and to answer it
safely it demands a lot of a column: at least ``MIN_COLUMN_LINE_COUNT`` lines
and ``MIN_COLUMN_WORD_COUNT`` words, from line pieces of two words or more.

A card, a map panel, an infographic block and a numeric table cell are all
*shorter* than that bar. On every page of the first Terra sample the detector
therefore found fewer than two columns, the safety gate fell back to a single
``(region=0, column=0)`` bucket, and its interleaving, monotonicity and
heading-attachment checks became unfalsifiable. See
``reports/esg_recovery_gate_diagnosis_2026-07-31.md``.

This module rebuilds the structure from evidence that survives short content.

``persistent_regions``
    Groups segments into vertical flows by proximity: a segment joins an open
    region when it sits close below it *and* shares its left edge or overlaps
    its horizontal span. A region is a panel, a card, a map callout, a table
    column, or a paragraph -- whatever the page actually keeps together. Unlike
    an XY-cut this survives panels whose bounding boxes overlap, which is the
    normal case for a map with scattered callouts.

``table_blocks``
    Finds unruled tables: runs of consecutive lines whose cells land on the
    same repeated left or right anchors. Ruled tables are already caught by the
    parser's own table candidates; these are the ones that carry no drawn
    lines and so never became a table candidate at all.

Both are read-only measurements over word boxes. No vision, no model call, no
gold, and nothing keyed to a particular document, ticker or page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from statistics import median

from esg_reading_order import Y_TOLERANCE

#: A segment joins an open region when the vertical gap to it is under this
#: multiple of the page's own line pitch. Above ~2 the gap between two stacked
#: paragraphs stops being distinguishable from the gap inside one.
REGION_JOIN_PITCH_MULTIPLE = 1.6
REGION_JOIN_GAP_FLOOR = 4.0
#: Left edges this close count as the same edge. Mirrors the intent of
#: ``esg_reading_order``'s cluster tolerance, scaled down because a panel edge
#: is far more exact than a prose column's ragged start.
REGION_ALIGN_WIDTH_SHARE = 0.010
REGION_ALIGN_FLOOR = 6.0
#: Horizontal overlap, as a share of the narrower span, that makes a segment a
#: continuation of a region whose left edge it does not share -- a centred
#: caption under a wider heading, a right-aligned number under another.
REGION_OVERLAP_MIN = 0.50

#: Table geometry. A row needs this many cells before its alignment means
#: anything; two cells is just a label and a value, which prose also produces.
MIN_TABLE_COLUMNS = 3
#: An anchor must recur on this many lines, and a block must contain this many
#: full-width rows, before a repeated left edge counts as a column rather than
#: a coincidence.
MIN_TABLE_ROWS = 4
TABLE_ALIGN_WIDTH_SHARE = 0.012
TABLE_ALIGN_FLOOR = 4.0
#: A line joins a table block when at most this many of its cells sit off the
#: block's anchors. One stray is allowed because a real table row carries the
#: occasional indented sub-label or footnote marker; a share threshold would
#: instead make the allowance depend on how many columns the table happens to
#: have. Applied to every line, not only to dense ones, so that a wrapped
#: cell's continuation line stays inside its own table.
TABLE_MAX_OFF_ANCHOR_CELLS = 1
#: Non-aligned lines a block may bridge. A table often carries a section title
#: or a stray note between its rows; ending the block there would hand the rest
#: of the table to the prose rules.
TABLE_RUN_BRIDGE_LINES = 1
#: Share of a block's anchors a line must fill to count as a complete row.
TABLE_FULL_ROW_ANCHOR_SHARE = 0.75

NUMERIC_RE = re.compile(r"^[\s$€£+\-(]*\d[\d,.\s%)]*$")
SENTENCE_END_RE = re.compile(r"[.?!][\"'”’)\]]*$")


@dataclass
class Region:
    """One persistent vertical flow: a panel, a card, or a table column."""

    index: int
    members: list[int] = field(default_factory=list)
    x0: float = 0.0
    x1: float = 0.0
    left: float = 0.0
    top: float = 0.0
    bottom: float = 0.0

    def overlap_share(self, seg_x0: float, seg_x1: float) -> float:
        shared = max(0.0, min(self.x1, seg_x1) - max(self.x0, seg_x0))
        narrower = min(self.x1 - self.x0, seg_x1 - seg_x0)
        return shared / narrower if narrower > 0 else 0.0


@dataclass
class TableBlock:
    """A run of consecutive lines that share one column signature."""

    lines: list[int]
    left_anchors: list[float]
    #: Share of the block's lines carrying a cell on the leftmost anchor. A row
    #: key that appears on only some lines means the other lines' values have
    #: no row to belong to.
    row_key_share: float
    #: Share of the block's lines that fill most of its anchors. A table of
    #: single-line cells is nearly all complete rows; one whose cells wrap over
    #: several lines is not, and then a visual line is no longer a record.
    full_row_share: float
    #: Lines on which one non-numeric label appears at two different anchors.
    parallel_label_lines: int
    #: Distinct labels involved in that repetition.
    parallel_labels: int


def visual_lines(segments: list) -> list[list[int]]:
    """Group segment indices into visual lines, top to bottom."""

    lines: list[list[int]] = []
    tops: list[float] = []
    for segment in sorted(segments, key=lambda s: (s.top, s.x0)):
        if not lines or abs(segment.top - tops[-1]) > Y_TOLERANCE:
            lines.append([segment.index])
            tops.append(segment.top)
        else:
            lines[-1].append(segment.index)
    lookup = {s.index: s for s in segments}
    return [sorted(line, key=lambda i: lookup[i].x0) for line in lines]


def line_pitch(segments: list, lines: list[list[int]]) -> float:
    """The page's own line spacing, used to scale every vertical tolerance."""

    lookup = {s.index: s for s in segments}
    tops = [min(lookup[i].top for i in line) for line in lines]
    gaps = [b - a for a, b in zip(tops, tops[1:]) if b > a]
    if gaps:
        return median(gaps)
    heights = [s.bottom - s.top for s in segments if s.bottom > s.top]
    return median(heights) if heights else 12.0


def persistent_regions(
    segments: list, page_width: float, lines: list[list[int]] | None = None
) -> list[Region]:
    """Cluster segments into vertical flows that the page keeps together.

    Walks the page a visual line at a time. Each segment either continues an
    open region -- close below it, and sharing its left edge or its span -- or
    opens a new one. A region may take at most one segment per line: two pieces
    of the same line were split at a gutter, so they are by construction in
    different panels, and letting one region swallow both would merge the very
    panels this is meant to separate.
    """

    if not segments:
        return []
    lines = lines if lines is not None else visual_lines(segments)
    lookup = {s.index: s for s in segments}
    pitch = line_pitch(segments, lines)
    join_gap = max(REGION_JOIN_GAP_FLOOR, pitch * REGION_JOIN_PITCH_MULTIPLE)
    align_tolerance = max(REGION_ALIGN_FLOOR, page_width * REGION_ALIGN_WIDTH_SHARE)

    regions: list[Region] = []
    open_regions: list[Region] = []
    for line in lines:
        line_top = min(lookup[i].top for i in line)
        open_regions = [r for r in open_regions if line_top - r.bottom <= join_gap]
        taken: set[int] = set()
        for index in line:
            segment = lookup[index]
            best, best_score = None, 0.0
            for region in open_regions:
                if region.index in taken:
                    continue
                overlap = region.overlap_share(segment.x0, segment.x1)
                aligned = abs(segment.x0 - region.left) <= align_tolerance
                if aligned:
                    score = 2.0 + overlap
                elif overlap >= REGION_OVERLAP_MIN:
                    score = 1.0 + overlap
                else:
                    continue
                if score > best_score:
                    best, best_score = region, score
            if best is None:
                best = Region(
                    index=len(regions),
                    x0=segment.x0,
                    x1=segment.x1,
                    left=segment.x0,
                    top=segment.top,
                )
                regions.append(best)
                open_regions.append(best)
            best.members.append(index)
            best.x0 = min(best.x0, segment.x0)
            best.x1 = max(best.x1, segment.x1)
            best.bottom = max(best.bottom, segment.bottom)
            taken.add(best.index)
    return regions


def _cluster_anchors(
    values: list[tuple[float, int]], tolerance: float, min_lines: int
) -> list[float]:
    """Recurring coordinates, kept only when enough distinct lines support one."""

    clusters: list[dict] = []
    for value, line_index in sorted(values):
        target = next(
            (c for c in clusters if abs(value - c["x"]) <= tolerance), None
        )
        if target is None:
            target = {"x": value, "values": [], "lines": set()}
            clusters.append(target)
        target["values"].append(value)
        target["lines"].add(line_index)
        target["x"] = median(target["values"])
    return sorted(c["x"] for c in clusters if len(c["lines"]) >= min_lines)


def _normalise(text: str) -> str:
    return " ".join((text or "").split()).strip(" .:").casefold()


def table_blocks(
    segments: list, page_width: float, lines: list[list[int]] | None = None
) -> list[TableBlock]:
    """Find unruled tables: runs of lines whose cells share column anchors.

    Anchors are collected from both left and right edges because a numeric
    column is normally right-aligned, so its cells share ``x1`` and nothing
    else. A line belongs to the block when most of its cells sit on a
    recurring anchor of either kind.
    """

    if not segments:
        return []
    lines = lines if lines is not None else visual_lines(segments)
    lookup = {s.index: s for s in segments}
    tolerance = max(TABLE_ALIGN_FLOOR, page_width * TABLE_ALIGN_WIDTH_SHARE)

    dense = [i for i, line in enumerate(lines) if len(line) >= MIN_TABLE_COLUMNS]
    if len(dense) < MIN_TABLE_ROWS:
        return []
    lefts = [(lookup[j].x0, i) for i in dense for j in lines[i]]
    rights = [(lookup[j].x1, i) for i in dense for j in lines[i]]
    left_anchors = _cluster_anchors(lefts, tolerance, MIN_TABLE_ROWS)
    right_anchors = _cluster_anchors(rights, tolerance, MIN_TABLE_ROWS)
    if len(left_anchors) < MIN_TABLE_COLUMNS:
        return []

    def on_anchor(value: float, anchors: list[float]) -> bool:
        return any(abs(value - a) <= tolerance for a in anchors)

    # Alignment is tested on every line, not only on dense ones: a wrapped
    # cell's continuation line carries one or two cells, still on the table's
    # own anchors, and excluding it would end the block at every wrap and leave
    # the table's own text to be judged as if it were free prose. But two cells
    # is the floor -- a single cell cannot show that its line takes part in a
    # row structure at all, and a footnote wrapped under a table's first column
    # would otherwise be swallowed by the table it sits below.
    aligned: set[int] = set()
    for i, cells in enumerate(lines):
        if len(cells) < 2:
            continue
        hits = sum(
            1
            for j in cells
            if on_anchor(lookup[j].x0, left_anchors)
            or on_anchor(lookup[j].x1, right_anchors)
        )
        if hits and len(cells) - hits <= TABLE_MAX_OFF_ANCHOR_CELLS:
            aligned.add(i)

    dense_set = set(dense)
    blocks: list[TableBlock] = []
    run: list[int] = []
    pending: list[int] = []

    def flush() -> None:
        if sum(1 for j in run if j in dense_set) >= MIN_TABLE_ROWS:
            blocks.append(
                _describe_block(run, lines, lookup, left_anchors, right_anchors, tolerance)
            )

    for i in range(len(lines)):
        if i in aligned:
            run.extend(pending)
            pending = []
            run.append(i)
        elif run and len(pending) < TABLE_RUN_BRIDGE_LINES:
            pending.append(i)
        else:
            flush()
            run, pending = [], []
    flush()
    return blocks


def _describe_block(
    run: list[int],
    lines: list[list[int]],
    lookup: dict,
    left_anchors: list[float],
    right_anchors: list[float],
    tolerance: float,
) -> TableBlock:
    """Measure the row-integrity properties of one table block."""

    block_anchors = sorted(
        a
        for a in left_anchors
        if any(
            abs(lookup[j].x0 - a) <= tolerance for i in run for j in lines[i]
        )
    )
    key_anchor = block_anchors[0] if block_anchors else 0.0
    with_key = sum(
        1
        for i in run
        if any(abs(lookup[j].x0 - key_anchor) <= tolerance for j in lines[i])
    )

    filled = 0
    for i in run:
        hit = sum(
            1
            for a in block_anchors
            if any(abs(lookup[j].x0 - a) <= tolerance for j in lines[i])
        )
        if block_anchors and hit >= TABLE_FULL_ROW_ANCHOR_SHARE * len(block_anchors):
            filled += 1

    # A *row key* repeated further right on the same line is the signature of
    # two independent tables printed side by side and read across the gutter:
    # the second table brought its own label column with it. The test is tied
    # to the key anchor on purpose. A repeated value -- "N/A N/A N/A" -- or a
    # repeated column header -- "FY2022 FY2023 FY2022 FY2023" over regional
    # groups of one table -- is ordinary, and neither sits in the key column.
    parallel_lines, parallel_labels = 0, set()
    for i in run:
        keys = {
            _normalise(lookup[j].raw_text)
            for j in lines[i]
            if abs(lookup[j].x0 - key_anchor) <= tolerance
            and not NUMERIC_RE.match(lookup[j].raw_text.strip())
        }
        keys.discard("")
        if not keys:
            continue
        elsewhere = {
            _normalise(lookup[j].raw_text)
            for j in lines[i]
            if abs(lookup[j].x0 - key_anchor) > tolerance
        }
        repeated = keys & elsewhere
        if repeated:
            parallel_lines += 1
            parallel_labels.update(repeated)

    return TableBlock(
        lines=list(run),
        left_anchors=block_anchors,
        row_key_share=with_key / len(run) if run else 0.0,
        full_row_share=filled / len(run) if run else 0.0,
        parallel_label_lines=parallel_lines,
        parallel_labels=len(parallel_labels),
    )


def continues(first: str, second: str) -> bool:
    """Does ``second`` continue the sentence or list that ``first`` starts?

    Deliberately narrow. It fires on the two patterns a reordering can break
    without any loss of text: a lead-in colon whose list must follow it, and a
    line that ends mid-sentence followed by a lower-case word. The first token
    is tested rather than the first letter so that "$33.1 million raised" reads
    as a new statement, which it is, and not as a continuation.
    """

    first, second = (first or "").strip(), (second or "").strip()
    if not first or not second:
        return False
    if first.endswith(":"):
        return True
    if SENTENCE_END_RE.search(first):
        return False
    token = second.split()[0]
    head = token[0]
    return head.isalpha() and head.islower()
