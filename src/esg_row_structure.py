"""Independent row-structure and full-width-span checks for ESG pages.

The existing layout gate accepts a column reconstruction when its word
preservation ratio clears a threshold. That ratio compares the reconstruction
against the same word list it was built from, so it is a permutation checksum:
it proves no word was dropped while rearranging, and cannot observe whether the
resulting order is correct or whether the column count was right. Corpus-wide it
passes 3,004 of 3,005 pages.

This module adds two checks that measure the page against evidence the
reconstruction did not produce, so they are able to fail:

``row_structure``
    A row-structured region (an unruled table, a disclosure index, a metric
    grid) holds short cells that occupy a small fraction of the column they sit
    in, while prose lines nearly fill their column. The discriminator is the
    25th percentile of per-segment fill: a table has a substantial tail of very
    short segments, a prose column does not. A row-structured page must not be
    read column-major, because doing so detaches every row label from its
    values.

    Chosen by measuring nine candidate features against the 135 joinable
    two-annotator gold pages. Cross-validated (5-fold, 6 repeats, thresholds
    refitted inside each training fold) this reaches 71% recall on
    ``table_dominant`` at an 8% false-positive rate on ``prose``, and beats
    every two-feature combination tried. Band co-occupancy -- the first
    hypothesis -- was measured at AUC 0.511, i.e. noise, and is retained only as
    reported evidence, never as a decision input.

``full_width_spans``
    A heading spanning the whole text block above a set of columns belongs to
    all of them. The reconstruction hoists only what falls inside the top
    ``HEADER_FOOTER_BAND_SHARE`` of page height (6%), so a heading placed
    mid-page is assigned to whichever single column its left edge lands in, and
    the remaining columns lose it. This reports those spans with their y
    positions so a caller can band the page at them.

Neither check rewrites text. Both are read-only measurements over word boxes.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from statistics import median

from esg_reading_order import (
    HEADER_FOOTER_BAND_SHARE,
    _line_groups,
    _line_segments,
    _linear_words,
    _number,
    _stable_column_starts,
)

# Gate threshold: 25th-percentile per-segment fill at or below this marks the
# page row-structured. Fitted on the 135 joinable gold pages; see module
# docstring for the cross-validated operating point.
MAX_P25_FILL = 0.2455
# A span must cross at least this many column boundaries to be full-width.
MIN_SPAN_COLUMNS = 2


@dataclass(frozen=True)
class RowStructureResult:
    """Read-only verdict for one page."""

    status: str
    reason: str
    column_count: int
    band_count: int
    shared_band_share: float          # reported evidence only, not a decision input
    median_fill_ratio: float
    p25_fill_ratio: float = 0.0       # the gate feature
    full_width_spans: tuple[float, ...] = field(default=())

    @property
    def row_structured(self) -> bool:
        return self.status == "row_structured"


def _column_index(x0: float, edges: list[float]) -> int:
    return max(0, bisect_right(edges, x0) - 1)


def analyse_page(
    words: list[dict],
    page_width: float,
    page_height: float,
) -> RowStructureResult:
    """Measure row structure and full-width spans for one page."""

    usable = _linear_words(words)
    if page_width <= 0 or page_height <= 0 or not usable:
        return RowStructureResult(
            "not_applicable", "insufficient_geometry_or_words", 0, 0, 0.0, 0.0
        )

    edges, reason = _stable_column_starts(usable, page_width, page_height)
    if not edges:
        return RowStructureResult("not_applicable", reason, 0, 0, 0.0, 0.0)

    header_limit = page_height * HEADER_FOOTER_BAND_SHARE
    footer_limit = page_height * (1.0 - HEADER_FOOTER_BAND_SHARE)

    bands: list[set[int]] = []
    segment_records: list[tuple[int, float]] = []   # (column index, width)
    column_right: dict[int, float] = {}
    spans: list[float] = []

    for group in _line_groups(usable):
        body = [
            word for word in group
            if _number(word, "bottom", _number(word, "top")) > header_limit
            and _number(word, "top") < footer_limit
        ]
        if not body:
            continue
        occupied: set[int] = set()
        for segment in _line_segments(body, page_width):
            x0 = _number(segment[0], "x0")
            x1 = max(_number(word, "x1", x0) for word in segment)
            index = _column_index(x0, edges)
            occupied.add(index)
            segment_records.append((index, max(0.0, x1 - x0)))
            column_right[index] = max(column_right.get(index, x1), x1)
            # A single segment straddling >=2 column edges is a full-width span:
            # it is one visual run, so the gutter split did not separate it.
            crossed = sum(1 for edge in edges if x0 < edge <= x1)
            if crossed >= MIN_SPAN_COLUMNS - 1 and index == 0:
                spans.append(_number(segment[0], "top"))
        if occupied:
            bands.append(occupied)

    if not bands or not segment_records:
        return RowStructureResult(
            "not_applicable", "no_body_lines", len(edges), 0, 0.0, 0.0, 0.0,
            tuple(sorted(spans)),
        )

    shared = sum(1 for band in bands if len(band) >= 2)
    shared_share = shared / len(bands)

    fills: list[float] = []
    for index, width in segment_records:
        right = column_right.get(index)
        if right is None:
            continue
        available = right - edges[index]
        if available > 1.0:
            fills.append(min(1.0, width / available))
    if not fills:
        return RowStructureResult(
            "not_applicable", "no_measurable_columns", len(edges), len(bands),
            shared_share, 1.0, 1.0, tuple(sorted(spans)),
        )
    fills.sort()
    fill = median(fills)
    p25 = fills[int(0.25 * (len(fills) - 1))]

    status = "row_structured" if p25 <= MAX_P25_FILL else "column_safe"
    reason = (
        f"p25_fill={p25:.3f}; median_fill={fill:.3f}; "
        f"shared_band_share={shared_share:.3f}"
    )

    return RowStructureResult(
        status,
        reason,
        len(edges),
        len(bands),
        shared_share,
        fill,
        p25,
        tuple(sorted(spans)),
    )
