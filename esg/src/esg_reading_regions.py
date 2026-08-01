"""Candidate region-aware reading-order reconstruction for ESG PDFs.

``esg_reading_order.reconstruct_column_order`` picks ONE column count for the
whole page. That is unsafe whenever a page is not uniform: a full-width
heading sitting above a block of columns, a page that changes from two
columns to four, an unruled table whose row labels must stay next to their
values, or a column detector that under-counts (three narrow columns merged
into one wide "column"). See ``reports/reading_order_handoff_2026-07-30.md``
for the measured evidence behind each of those failure modes.

This module is a **candidate**, read-only against the corpus. It does not
replace ``esg_reading_order`` and nothing here changes production parsing.
It is deliberately built on top of the already-validated primitives in
``esg_reading_order`` (line grouping, gutter splitting, stable-column
clustering) rather than re-deriving them, so a two-column page reconstructs
identically either way.

Pipeline, top to bottom:

1. Words -> visual lines (``_line_groups``) -> gutter-split segments
   (``_line_segments``), reused unchanged from ``esg_reading_order``.
2. The body (header/footer bands already excluded, matching the existing
   ``HEADER_FOOTER_BAND_SHARE`` policy) is split into vertical *regions* at
   large vertical gaps, full-width heading lines, and persistent changes in
   how many segments a line has. A short-lived change (a single indented or
   truncated line) is smoothed back into its neighbour rather than starting
   a new region -- see ``MIN_REGION_RUN_LINES``.
3. Each region gets its own column detection via
   ``esg_reading_order._stable_column_starts``, scoped to that region's own
   height so the vertical-coverage/-overlap ratios mean "stable within this
   region", not "stable across the whole page".
4. Recursive refinement: a region's detected columns are each individually
   re-examined for a second, narrower internal gutter. This is how a page
   whose four visual columns were merged into two wide ones (narrow inner
   gutters below the page-level gap floor) can still be recovered. The
   thresholds used only at this inner pass (``INNER_GAP_FLOOR`` etc.) are
   **not** corpus-validated -- they exist so the mechanism is testable, and
   must be checked against human review before any production use.
5. Each region is classified and ordered:
   - a single full-width heading line -> its own region, read as-is.
   - one segment per line throughout -> ``single_column_prose``, natural
     top-to-bottom order (no reordering needed or attempted).
   - stable columns, low per-segment fill (reusing the cross-validated
     ``esg_row_structure.MAX_P25_FILL`` cut) -> ``row_structured``, read
     across each row (the source order already preserves this).
   - stable columns, ordinary fill -> ``multi_column_prose``, read down each
     column, left to right.
   - anything else with a real multi-segment signal that does not meet the
     stability bar -> ``uncertain``: held, not guessed.
6. Regions are joined top to bottom. A trailing block preserves any words the
   geometry pass declined to place (genuinely rotated decorative runs, using
   the same detection as ``esg_reading_order._linear_words``) so no source
   word is ever silently dropped, only clearly set apart from body order.

Safety invariant: every non-blank source word appears in the output exactly
once. The default path re-checks this with the original exact token-multiset
comparison. Verified region-table markdown adds ``|`` and ``---``, so used
regions instead keep the audit's strict semantic recall/extra-token checks;
every region not substituted still passes the exact original checksum. This
is a guard against accidental loss/duplication, not a correctness measure.
The real correctness signal for this module is human Better/Same/Worse review,
not the preservation ratio.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from statistics import median
import re

from esg_reading_order import (
    HEADER_FOOTER_BAND_SHARE,
    MAX_COLUMN_COUNT,
    MIN_COLUMN_LINE_COUNT,
    MIN_COLUMN_WORD_COUNT,
    _line_groups,
    _line_segments,
    _linear_words,
    _number,
    _stable_column_starts,
    _usable_words,
    _word_counter,
)
from esg_row_structure import MAX_P25_FILL

# A run of lines with a different segment count than its neighbours must
# persist at least this long before it is treated as a genuine region
# boundary rather than one short/indented outlier line folded into its
# neighbour's structure.
MIN_REGION_RUN_LINES = 3

# A vertical gap between consecutive lines larger than this (relative to the
# typical gap on the page, with a floor) starts a new region.
LARGE_GAP_MIN_ABS = 18.0
LARGE_GAP_PITCH_MULTIPLIER = 3.0
LARGE_GAP_HEIGHT_SHARE = 0.035

# A line with exactly one segment spanning at least this share of the body's
# observed content width is treated as a full-width heading and always
# starts a new region.
FULL_WIDTH_MIN_CONTENT_SHARE = 0.6

# A blank vertical strip this wide can separate independent side-by-side
# panels.  It scales with the page and is only used when the two sides do not
# behave like a normal dense prose grid.
PANEL_GAP_WIDTH_SHARE = 0.035
PANEL_MIN_WORDS = 10
PANEL_MIN_WIDTH_SHARE = 0.12
PANEL_MIN_VERTICAL_OVERLAP = 0.30
PANEL_MAX_SHARED_LINE_SHARE = 0.82

# How many times a region may be peeled into side-by-side panels before it is
# split into vertical runs. Each peel strictly shrinks both sides (a panel
# split needs PANEL_MIN_WORDS on each side), so recursion terminates on its
# own; this cap only keeps the nesting bounded and inspectable.
MAX_PANEL_PEEL_DEPTH = 3

# Compare short runs on both sides of a possible horizontal boundary.  A
# lasting change in the number or position of column starts is a region cut.
STRUCTURE_WINDOW_LINES = 4
STRUCTURE_START_TOLERANCE_SHARE = 0.055

# --- Recursive inner-gutter thresholds -------------------------------------
# Deliberately smaller than esg_reading_order's page-level constants (16.0pt
# floor, 0.14 page-width gap share) because an inner gutter separating two
# sub-columns of an already-detected column is typically narrower, relative
# to that column, than the gutter between independent page-level columns.
# UNVALIDATED: chosen to make the recursion mechanism exercisable and
# testable, not fitted to any labelled sample. Do not treat pages recovered
# only via this pass as trustworthy until a human has reviewed them.
INNER_GAP_FLOOR = 6.0
INNER_GAP_WIDTH_SHARE = 0.018
INNER_CLUSTER_TOLERANCE_FLOOR = 10.0
INNER_CLUSTER_TOLERANCE_WIDTH_SHARE = 0.03
INNER_MIN_COLUMN_GAP_SHARE = 0.08

# A ruled table box is usually a little larger than the words used to build a
# RegionInfo box. Exact bbox equality would therefore reject valid matches.
# A table is a candidate for one region when it covers at least this share of
# the region's area. Substitution is still refused unless the match is unique
# in both directions and the strict region-scoped table token audit passes.
VERIFIED_REGION_MIN_CONTAINMENT = 0.80


@dataclass(frozen=True)
class RegionInfo:
    """One vertical region of the page, for both ordering and review display."""

    top: float
    bottom: float
    left: float
    right: float
    region_type: str  # heading | single_column_prose | multi_column_prose | row_structured | table_verified | uncertain
    column_count: int
    reason: str


@dataclass(frozen=True)
class RegionReadingOrderResult:
    """Candidate result for one page. Never written back into the corpus."""

    status: str  # "candidate_ready" | "needs_review"
    text: str
    reason: str
    regions: tuple[RegionInfo, ...]
    source_word_count: int
    candidate_word_count: int
    preservation_ratio: float
    # Final text for each item in ``regions``, aligned index-for-index. Header,
    # footer, and rotated text are intentionally not included here.
    region_texts: tuple[str, ...] = ()


def _lines_with_segments(words: list[dict], page_width: float) -> list[dict]:
    lines: list[dict] = []
    for group in _line_groups(words):
        segments = _line_segments(group, page_width)
        seg_infos = []
        for segment in segments:
            x0 = _number(segment[0], "x0")
            x1 = max(_number(word, "x1", x0) for word in segment)
            seg_infos.append({"x0": x0, "x1": x1, "words": segment})
        lines.append(
            {
                "top": min(_number(word, "top") for word in group),
                "bottom": max(_number(word, "bottom", _number(word, "top")) for word in group),
                "segments": seg_infos,
            }
        )
    return lines


def _is_heading_line(line: dict, content_width: float) -> bool:
    if len(line["segments"]) != 1:
        return False
    segment = line["segments"][0]
    return (segment["x1"] - segment["x0"]) >= FULL_WIDTH_MIN_CONTENT_SHARE * content_width


def _large_gap(lines: list[dict], page_height: float) -> float:
    """The vertical gap above which two lines belong to different runs."""

    gaps = [lines[i]["top"] - lines[i - 1]["bottom"] for i in range(1, len(lines))]
    positive_gaps = [gap for gap in gaps if gap > 0]
    typical_gap = median(positive_gaps) if positive_gaps else 0.0
    return max(LARGE_GAP_MIN_ABS, typical_gap * LARGE_GAP_PITCH_MULTIPLIER, page_height * LARGE_GAP_HEIGHT_SHARE)


def _row_gap_middles(lines: list[dict], page_height: float) -> list[float]:
    """Heights at which `lines` would be cut into separate runs by a large gap."""

    if len(lines) < 2:
        return []
    large_gap = _large_gap(lines, page_height)
    return [
        (lines[index - 1]["bottom"] + lines[index]["top"]) / 2
        for index in range(1, len(lines))
        if lines[index]["top"] - lines[index - 1]["bottom"] > large_gap
    ]


def _bridges_row_gap(first: list[dict], second: list[dict], page_height: float) -> bool:
    """Does either side reach across a row gap that would split the other?

    This is the one configuration a run-first reader cannot get right. Runs are
    cut at large vertical gaps; if a panel has content both above and below such
    a gap in the panel beside it, that gap can never become a run boundary while
    both are considered together, so every run holds one slice of each panel and
    the two are emitted alternately. When neither side reaches across the other's
    row gaps, the existing order -- runs first, then a panel split inside each
    run -- already separates them correctly, and is left to do so.
    """

    for main, panel in ((first, second), (second, first)):
        for middle in _row_gap_middles(main, page_height):
            above = any(line["bottom"] <= middle for line in panel)
            below = any(line["top"] >= middle for line in panel)
            if above and below:
                return True
    return False


def _split_into_runs(lines: list[dict], page_height: float) -> list[list[dict]]:
    """Group lines into vertical runs at gaps, headings, and stable segment counts."""

    if not lines:
        return []

    large_gap = _large_gap(lines, page_height)

    content_left = min(seg["x0"] for line in lines for seg in line["segments"])
    content_right = max(seg["x1"] for line in lines for seg in line["segments"])
    content_width = max(content_right - content_left, 1.0)

    runs: list[list[dict]] = [[lines[0]]]
    for index in range(1, len(lines)):
        previous_line = lines[index - 1]
        current_line = lines[index]
        gap = current_line["top"] - previous_line["bottom"]
        # A full-width line only forces a region boundary when it sits next to
        # a differently-structured (multi-segment) neighbour -- i.e. it looks
        # like a heading sitting over columns. Without this contrast check,
        # ordinary single-column prose (every line is "full width" because
        # there is only one column) would fragment into one region per line.
        heading_into_columns = _is_heading_line(previous_line, content_width) and len(current_line["segments"]) != 1
        columns_into_heading = _is_heading_line(current_line, content_width) and len(previous_line["segments"]) != 1
        boundary = gap > large_gap or heading_into_columns or columns_into_heading
        if boundary:
            runs.append([current_line])
        else:
            runs[-1].append(current_line)

    merged = _merge_short_runs(runs, content_width)
    structured: list[list[dict]] = []
    for run in merged:
        structured.extend(_split_structure_changes(run, content_width))
    return structured


def _rough_starts(lines: list[dict], content_width: float) -> list[float]:
    """Return repeated segment starts for a short vertical slice."""

    tolerance = max(8.0, content_width * STRUCTURE_START_TOLERANCE_SHARE)
    clusters: list[list[float]] = []
    for line in lines:
        for segment in line["segments"]:
            x0 = segment["x0"]
            target = next((c for c in clusters if abs(median(c) - x0) <= tolerance), None)
            if target is None:
                clusters.append([x0])
            else:
                target.append(x0)
    return sorted(float(median(c)) for c in clusters if len(c) >= 2)


def _different_structure(first: list[float], second: list[float], content_width: float) -> bool:
    if not first or not second:
        return False
    # Only the strongest transition is safe here: a full-width block changing
    # to two or more repeated starts (or the reverse). Charts and tables often
    # shift individual starts from row to row, which is not a region boundary.
    return (len(first) == 1 and len(second) >= 2) or (len(second) == 1 and len(first) >= 2)


def _is_single_column_slice(slice_lines: list[dict]) -> bool:
    """Does this window slice really consist of one-segment lines?

    ``_different_structure`` reads "one repeated start" as "a full-width
    block". That inference only holds if the lines on that side each actually
    have one segment. ``_rough_starts`` needs a start to repeat twice before
    it reports a cluster at all, so inside a slice only ``window`` lines tall
    it silently under-reports any column that happens to be sparse there --
    and a side whose second column merely failed to repeat looks identical to
    a genuinely full-width one. Checking the lines' own segment counts is the
    direct evidence, and it is the same signal the cut selection below already
    trusts to pinpoint the boundary line.
    """

    return all(len(line["segments"]) == 1 for line in slice_lines)


def _split_structure_changes(run: list[dict], content_width: float) -> list[list[dict]]:
    """Split at the strongest persistent change in local column starts."""

    window = STRUCTURE_WINDOW_LINES
    if len(run) < window * 2:
        return [run]
    candidates: list[int] = []
    # The "before"/"after" slices are clamped rather than bounded to a fixed
    # `window`-sized range on both sides: a transition close to the start (or
    # end) of the run must still be reachable. `_rough_starts` requires two
    # occurrences of a given start before it counts a cluster at all (its own
    # noise guard), so a clamped, shorter slice can only ever under-report
    # structure, never fabricate it -- it is not a source of false positives.
    # Fixing the range to `[window, len(run) - window + 1)` (the original
    # bound) makes the earliest reachable index `window`, one line too late
    # whenever the real transition sits inside the first `window` lines of
    # the run (measured on BBY-2024 p46: the first line of a 4-column body
    # was then unreachable as a cut point and stayed merged into the
    # preceding single-column paragraph).
    for index in range(1, len(run)):
        before_slice = run[max(0, index - window):index]
        after_slice = run[index:index + window]
        before = _rough_starts(before_slice, content_width)
        after = _rough_starts(after_slice, content_width)
        if not _different_structure(before, after, content_width):
            continue
        # Corroborate the "full-width" side against its own lines before
        # accepting the transition (see `_is_single_column_slice`). Without
        # this, any two-panel layout whose lines interleave across the panels
        # produces phantom boundaries wherever one panel's starts happen not
        # to repeat inside the window -- measured on AEO-2024 p8, where a
        # "before" slice containing two clearly two-segment lines
        # ("sustainable sources | 60%", "2028 GOAL: | 46%") still reported a
        # single start cluster, cutting the progress card in half so the whole
        # right-hand chart was emitted between the card's two pieces.
        single_side = before_slice if len(before) == 1 else after_slice
        if not _is_single_column_slice(single_side):
            continue
        candidates.append(index)
    if not candidates:
        return [run]
    # Adjacent candidate indices all describe the same one transition: a
    # window comparison fires anywhere its "after" side has accumulated two
    # lines of the new structure while its "before" side still has two of
    # the old one, which can span several consecutive indices around the
    # real boundary. That span brackets the transition but does not pinpoint
    # it -- picking its midpoint (or its first or last index) is a guess
    # that is only right by coincidence: a window can fire one line before
    # the actual change (its "after" slice reaching past the boundary to
    # borrow two new-structure lines) or persist one line after it (its
    # "before" slice still reaching back before the boundary), and which
    # happens depends on how the surrounding lines land, not on where in the
    # group's index range the true boundary sits (measured on BBY-2024 p46:
    # the midpoint of group [4] landed one line late; the group's own first
    # index, once the search range below was widened to reach it, landed one
    # line early instead -- neither endpoint is reliable).
    #
    # The group's index range is only used to bound a direct, per-line
    # check: within that range, find the first line whose OWN segment count
    # already matches the "after" side rather than the "before" side. That
    # line -- not any position derived from the group's shape -- is the
    # actual first line of the new region.
    groups: list[list[int]] = [[candidates[0]]]
    for item in candidates[1:]:
        if item <= groups[-1][-1] + 1:
            groups[-1].append(item)
        else:
            groups.append([item])
    group = groups[0]
    before0 = _rough_starts(run[max(0, group[0] - window):group[0]], content_width)
    transitioning_to_multi = len(before0) == 1
    cut = group[0]
    for index in range(group[0], group[-1] + 1):
        is_multi_segment = len(run[index]["segments"]) != 1
        if is_multi_segment == transitioning_to_multi:
            cut = index
            break
    else:
        cut = group[len(group) // 2]
    if cut < MIN_REGION_RUN_LINES or len(run) - cut < MIN_REGION_RUN_LINES:
        return [run]
    return [run[:cut], run[cut:]]


def _panel_split(
    run: list[dict],
    page_width: float,
    require: Callable[[list[dict], list[dict]], bool] | None = None,
) -> tuple[list[dict], list[dict]] | None:
    """Find a strong empty vertical strip between independent panels.

    ``require`` is an extra condition the two sides must satisfy. Candidate
    strips are tried widest first, so a caller that needs more than a blank
    strip keeps the search going past the strips that do not qualify instead of
    losing the split to the widest one.
    """

    words = _run_words(run)
    intervals = sorted((_number(w, "x0"), _number(w, "x1", _number(w, "x0"))) for w in words)
    merged: list[list[float]] = []
    for left, right in intervals:
        if not merged or left > merged[-1][1]:
            merged.append([left, right])
        else:
            merged[-1][1] = max(merged[-1][1], right)
    gaps = [(merged[i + 1][0] - merged[i][1], (merged[i + 1][0] + merged[i][1]) / 2) for i in range(len(merged) - 1)]
    for gap, cut in sorted(gaps, reverse=True):
        if gap < page_width * PANEL_GAP_WIDTH_SHARE:
            break
        left_words = [w for w in words if _number(w, "x1", _number(w, "x0")) <= cut]
        right_words = [w for w in words if _number(w, "x0") >= cut]
        if len(left_words) < PANEL_MIN_WORDS or len(right_words) < PANEL_MIN_WORDS:
            continue
        left_width = max(_number(w, "x1") for w in left_words) - min(_number(w, "x0") for w in left_words)
        right_width = max(_number(w, "x1") for w in right_words) - min(_number(w, "x0") for w in right_words)
        if min(left_width, right_width) < page_width * PANEL_MIN_WIDTH_SHARE:
            continue
        left_top, left_bottom = min(_number(w, "top") for w in left_words), max(_number(w, "bottom") for w in left_words)
        right_top, right_bottom = min(_number(w, "top") for w in right_words), max(_number(w, "bottom") for w in right_words)
        overlap = max(0.0, min(left_bottom, right_bottom) - max(left_top, right_top))
        shorter = min(left_bottom - left_top, right_bottom - right_top)
        if shorter <= 0 or overlap / shorter < PANEL_MIN_VERTICAL_OVERLAP:
            continue
        groups = _line_groups(words)
        shared = sum(bool([w for w in g if _number(w, "x1") <= cut]) and bool([w for w in g if _number(w, "x0") >= cut]) for g in groups)
        if shared / max(len(groups), 1) > PANEL_MAX_SHARED_LINE_SHARE:
            continue
        left_lines = _lines_with_segments(left_words, page_width)
        right_lines = _lines_with_segments(right_words, page_width)
        if require is not None and not require(left_lines, right_lines):
            continue
        return left_lines, right_lines
    return None


def _merge_short_runs(runs: list[list[dict]], content_width: float) -> list[list[dict]]:
    """Fold a short, non-heading run into the previous run.

    A single truncated or indented line can look like a segment-count change
    for one line; without this smoothing pass it would fracture a stable
    column block into spurious extra regions.
    """

    merged: list[list[dict]] = []
    for run in runs:
        is_heading_run = len(run) == 1 and _is_heading_line(run[0], content_width)
        if merged and not is_heading_run and len(run) < MIN_REGION_RUN_LINES:
            previous_is_heading = len(merged[-1]) == 1 and _is_heading_line(merged[-1][0], content_width)
            if not previous_is_heading:
                merged[-1].extend(run)
                continue
        merged.append(run)
    return merged


def _run_words(run: list[dict]) -> list[dict]:
    return [word for line in run for segment in line["segments"] for word in segment["words"]]


def _fill_ratios(run_words: list[dict], edges: list[float], page_width: float) -> list[float]:
    """Per-segment fill ratio against each column's observed right edge.

    Mirrors ``esg_row_structure``'s measurement but against externally
    supplied (possibly recursively refined) edges rather than recomputing
    them, which that module's ``analyse_page`` cannot do.
    """

    column_right: dict[int, float] = {}
    records: list[tuple[int, float]] = []
    for group in _line_groups(run_words):
        for segment in _line_segments(group, page_width):
            x0 = _number(segment[0], "x0")
            x1 = max(_number(word, "x1", x0) for word in segment)
            index = max(0, bisect_right(edges, x0) - 1)
            records.append((index, max(0.0, x1 - x0)))
            column_right[index] = max(column_right.get(index, x1), x1)

    fills: list[float] = []
    for index, width in records:
        right = column_right.get(index)
        left = edges[index] if index < len(edges) else None
        if right is None or left is None:
            continue
        available = right - left
        if available > 1.0:
            fills.append(min(1.0, width / available))
    return sorted(fills)


def _p25(values: list[float]) -> float:
    if not values:
        return 1.0
    return values[int(0.25 * (len(values) - 1))]


def _refine_column_once(
    column_words: list[dict],
    left_edge: float,
    right_edge: float,
    run_height: float,
) -> list[float]:
    """Look inside one detected column for a second, narrower gutter.

    Returns the original ``[left_edge]`` unchanged unless a stable inner
    split is found (see module docstring for why the thresholds here differ
    from the page-level ones, and why they are unvalidated).
    """

    if len(column_words) < MIN_COLUMN_WORD_COUNT:
        return [left_edge]

    local_width = max(right_edge - left_edge, 1.0)
    shifted = [
        {**word, "x0": _number(word, "x0") - left_edge, "x1": _number(word, "x1", _number(word, "x0")) - left_edge}
        for word in column_words
    ]

    gap_threshold = max(INNER_GAP_FLOOR, local_width * INNER_GAP_WIDTH_SHARE)
    cluster_tolerance = max(INNER_CLUSTER_TOLERANCE_FLOOR, local_width * INNER_CLUSTER_TOLERANCE_WIDTH_SHARE)
    minimum_gap = local_width * INNER_MIN_COLUMN_GAP_SHARE

    groups = _line_groups(shifted)
    segments = []
    for group in groups:
        sorted_line = sorted(group, key=lambda item: _number(item, "x0"))
        current = [sorted_line[0]]
        for word in sorted_line[1:]:
            gap = _number(word, "x0") - _number(current[-1], "x1")
            if gap > gap_threshold:
                segments.append(current)
                current = [word]
            else:
                current.append(word)
        segments.append(current)

    clusters: list[dict] = []
    for segment in sorted(segments, key=lambda item: _number(item[0], "x0")):
        if len(segment) < 2:
            continue
        x0 = _number(segment[0], "x0")
        target = next((cluster for cluster in clusters if abs(x0 - cluster["x"]) <= cluster_tolerance), None)
        if target is None:
            target = {"x": x0, "starts": [], "words": 0, "tops": []}
            clusters.append(target)
        target["starts"].append(x0)
        target["words"] += len(segment)
        target["tops"].append(_number(segment[0], "top"))
        target["x"] = median(target["starts"])

    clusters = [c for c in clusters if len(c["starts"]) >= MIN_COLUMN_LINE_COUNT and c["words"] >= MIN_COLUMN_WORD_COUNT]
    clusters.sort(key=lambda c: c["x"])
    if len(clusters) < 2:
        return [left_edge]

    selected = [clusters[0]]
    for cluster in clusters[1:]:
        if cluster["x"] - selected[-1]["x"] >= minimum_gap:
            selected.append(cluster)
    if len(selected) < 2:
        return [left_edge]

    coverage_ok = all(
        (max(c["tops"]) - min(c["tops"])) / max(run_height, 1.0) >= 0.20 for c in selected
    )
    if not coverage_ok:
        return [left_edge]

    return [left_edge + float(min(c["starts"])) for c in selected]


def _classify_and_order_run(
    run: list[dict],
    page_width: float,
) -> tuple[RegionInfo, str]:
    words = _run_words(run)
    top = run[0]["top"]
    bottom = run[-1]["bottom"]
    run_height = max(bottom - top, 1.0)

    if len(run) == 1 and len(run[0]["segments"]) == 1:
        text = " ".join(str(w.get("text", "")).strip() for w in sorted(words, key=lambda w: _number(w, "x0")))
        return RegionInfo(top, bottom, min(_number(w, "x0") for w in words), max(_number(w, "x1") for w in words), "heading", 0, "full_width_heading_line"), text

    if len(words) < MIN_COLUMN_WORD_COUNT:
        text = _lines_to_text(run)
        return RegionInfo(top, bottom, min(_number(w, "x0") for w in words), max(_number(w, "x1") for w in words), "single_column_prose", 1, "insufficient_words_for_columns"), text

    # Try column detection unconditionally rather than gating on "most common
    # segment count per line": a genuine multi-column page routinely has more
    # short (1-segment) lines than full ones (paragraph ends, bullets, empty
    # cells in the other column), so a per-line mode is not a reliable
    # single-vs-multi-column signal. This mirrors how
    # esg_reading_order.reconstruct_column_order itself decides -- it never
    # pre-gates on segment count either, only on the *reason*
    # _stable_column_starts fails for.
    edges, reason = _stable_column_starts(words, page_width, run_height)
    labels = [str(w.get("text", "")).strip().casefold() for w in words]
    contents_layout = "contents" in labels and sum(bool(re.fullmatch(r"\d{1,3}", label.rstrip(".,"))) for label in labels) >= 8
    if not edges and contents_layout:
        repeated = _rough_starts(run, page_width)
        # Contents pages commonly expose four starts: entry text and page
        # number for the left column, then entry text and page number for the
        # right. The entry starts are the first and middle clusters.
        if len(repeated) >= 4:
            recovered = [repeated[0], repeated[len(repeated) // 2]]
        else:
            left = min(_number(w, "x0") for w in words)
            right = max(_number(w, "x1", _number(w, "x0")) for w in words)
            recovered = _refine_column_once(words, left, right, run_height)
        if 2 <= len(recovered) <= MAX_COLUMN_COUNT:
            edges, reason = recovered, "contents_hidden_columns"
    if not edges:
        text = _lines_to_text(run)
        if reason in {"no_repeating_column_starts", "column_starts_not_separated"}:
            return RegionInfo(top, bottom, min(_number(w, "x0") for w in words), max(_number(w, "x1") for w in words), "single_column_prose", 1, reason), text
        return RegionInfo(top, bottom, min(_number(w, "x0") for w in words), max(_number(w, "x1") for w in words), "uncertain", 0, reason), text

    # Recursive refinement: look inside each detected column for one more split.
    refined: list[float] = []
    right_bounds = edges[1:] + [max(_number(w, "x1", _number(w, "x0")) for w in words)]
    for edge, right in zip(edges, right_bounds):
        column_words = [w for w in words if edge <= _number(w, "x0") < right]
        refined.extend(_refine_column_once(column_words, edge, right, run_height))
    refined = sorted(set(refined))
    if len(refined) > len(edges) and len(refined) <= MAX_COLUMN_COUNT:
        edges = refined  # a recursive split fired and stayed within the trusted column-count bound

    fills = _fill_ratios(words, edges, page_width)
    p25 = _p25(fills)

    if p25 <= MAX_P25_FILL and not contents_layout:
        text = _lines_to_text(run)
        info = RegionInfo(top, bottom, min(_number(w, "x0") for w in words), max(_number(w, "x1") for w in words), "row_structured", len(edges), f"p25_fill={p25:.3f}")
        return info, text

    columns: list[list[dict]] = [[] for _ in edges]
    for word in words:
        index = max(0, bisect_right(edges, _number(word, "x0")) - 1)
        columns[index].append(word)
    text = "\n\n".join(part for part in (_lines_to_text_words(column) for column in columns) if part)
    info = RegionInfo(top, bottom, min(_number(w, "x0") for w in words), max(_number(w, "x1") for w in words), "multi_column_prose", len(edges), f"p25_fill={p25:.3f}")
    return info, text


def _lines_to_text(run: list[dict]) -> str:
    return "\n".join(
        " ".join(str(word.get("text", "")).strip() for word in sorted(line_words, key=lambda w: _number(w, "x0")))
        for line_words in (_run_words([line]) for line in run)
        if line_words
    )


def _lines_to_text_words(words: list[dict]) -> str:
    if not words:
        return ""
    return "\n".join(
        " ".join(str(word.get("text", "")).strip() for word in sorted(group, key=lambda w: _number(w, "x0")))
        for group in _line_groups(words)
        if group
    )


def _content_width(lines: list[dict]) -> float:
    if not lines:
        return 1.0
    left = min(seg["x0"] for line in lines for seg in line["segments"])
    right = max(seg["x1"] for line in lines for seg in line["segments"])
    return max(right - left, 1.0)


def _merge_orphan_panel_titles(runs: list[list[dict]], page_width: float) -> list[list[dict]]:
    """Reattach a run of short, single-segment panel titles to the run that
    completes the panel split, when the two were separated into different
    runs by ``_split_into_runs``.

    A run can end up holding nothing but two side-by-side panels' own short
    titles (each panel's title line, not its body) when a page has a
    page-spanning heading directly above two panels: the heading's own
    segment reaches across both panels' x-ranges (that is what makes it a
    heading), so it is peeled off first here, the same way
    ``_is_heading_line`` already lets ``_split_into_runs`` recognise one. What
    is left -- just the two titles -- is too few words on either side to
    pass `_panel_split`'s own word-count floor (``PANEL_MIN_WORDS``), a floor
    that exists precisely so a couple of stray words cannot force a guess.
    The words each title needs are in the NEXT run, one panel's body per
    side. Merging is kept only when the two runs' words, combined, actually
    panel-split (checked directly, not assumed) -- a run that does not merge
    usefully is left exactly as ``_split_into_runs`` produced it, so an
    ordinary single-column paragraph (every line also has one segment) is
    never pulled into its neighbour by this pass.
    """

    if len(runs) < 2:
        return runs
    merged: list[list[dict]] = []
    index = 0
    while index < len(runs):
        run = runs[index]
        has_next = index + 1 < len(runs)
        word_count = sum(len(seg["words"]) for line in run for seg in line["segments"])
        # Gated on the run's OWN, unmodified word count: an ordinary
        # multi-line paragraph (BBY-2024 p46's 3-line, 42-word introduction,
        # for instance) must never enter the peel loop below at all, because
        # peeling against a content_width recomputed from an
        # already-shrinking remainder is a self-referential check -- the
        # widest line left keeps trivially measuring as ">= 60% of itself",
        # peeling one genuine paragraph line after another until only the
        # narrowest line remains (measured on BBY-2024 p46, where this
        # mispeeled the introduction into three separate one-line "heading"
        # regions). A run this short and this light on words is never a
        # real paragraph, only candidate title fragments.
        if not (has_next and len(run) > 1 and all(len(line["segments"]) == 1 for line in run) and word_count < PANEL_MIN_WORDS * 2):
            merged.append(run)
            index += 1
            continue
        # `content_width` is fixed once, from this run's own original lines,
        # before anything is popped -- so each candidate heading line is
        # judged against the whole cluster's width, not a shrinking one.
        content_width = _content_width(run)
        remainder = list(run)
        heading_lines: list[dict] = []
        while len(remainder) > 1 and _is_heading_line(remainder[0], content_width):
            heading_lines.append(remainder.pop(0))
        if remainder and _panel_split(remainder + runs[index + 1], page_width) is not None:
            merged.extend([heading_line] for heading_line in heading_lines)
            merged.append(remainder + runs[index + 1])
            index += 2
            continue
        merged.append(run)
        index += 1
    return merged


def _regions_from_lines(
    lines: list[dict], page_width: float, page_height: float, depth: int = 0
) -> tuple[list[RegionInfo], list[str], list[list[dict]], bool]:
    """Split `lines` into runs and classify each. Shared by the top-level call
    and by each side of a panel split.

    A panel split is attempted on the whole set of lines *before* they are cut
    into vertical runs. Splitting into runs first and only then looking for
    side-by-side panels within each run assumes every panel starts and ends at
    the same heights as its neighbours, which a page-spanning panel -- a tall
    pull-quote or sidebar running alongside content that has its own row
    breaks -- does not. Such a panel bridges the row gaps in the content beside
    it, so no run boundary can fall between those rows; each run then holds one
    slice of every panel, and splitting run by run interleaves them (measured
    on BBWI-2023 p22: the diversity infographic, the awards panel and the right
    pull-quote were emitted a slice at a time, mid-sentence).

    Peeling first is safe because a strip only qualifies here if it is blank
    across the whole region: a heading or paragraph that reaches over a gutter
    keeps that gutter from being a panel boundary at this scope, which is what
    stops the columns of one flowing block from being torn apart. `_panel_split`
    then still has to clear its own independence bars (words and width on each
    side, vertical overlap, and above all a shared-visual-line share below
    `PANEL_MAX_SHARED_LINE_SHARE`, which is what separates two independent
    panels from two columns of a single flow).
    """

    regions: list[RegionInfo] = []
    parts: list[str] = []
    region_words: list[list[dict]] = []
    any_uncertain = False

    if depth < MAX_PANEL_PEEL_DEPTH and len(lines) > 1:
        panels = _panel_split(
            lines,
            page_width,
            require=lambda left, right: _bridges_row_gap(left, right, page_height),
        )
        if panels is not None:
            for panel in panels:
                panel_regions, panel_parts, panel_words, panel_uncertain = _regions_from_lines(
                    panel, page_width, page_height, depth + 1
                )
                regions.extend(panel_regions)
                parts.extend(panel_parts)
                region_words.extend(panel_words)
                any_uncertain = any_uncertain or panel_uncertain
            return regions, parts, region_words, any_uncertain

    runs = _merge_orphan_panel_titles(_split_into_runs(lines, page_height), page_width)
    for run in runs:
        info, text = _classify_and_order_run(run, page_width)
        panel_parts = _panel_split(run, page_width) if info.region_type in {"single_column_prose", "row_structured", "uncertain"} else None
        classified_runs = panel_parts if panel_parts else [run]
        classified = [_classify_and_order_run(part, page_width) for part in classified_runs] if panel_parts else [(info, text)]
        for classified_run, (region_info, region_text) in zip(classified_runs, classified):
            regions.append(region_info)
            region_words.append(_run_words(classified_run))
            if region_info.region_type == "uncertain":
                any_uncertain = True
            # A classified run always contains words, so its text is non-empty.
            # Keeping this aligned with ``regions`` makes safe local table
            # substitution and debugging possible.
            parts.append(region_text)
    return regions, parts, region_words, any_uncertain


def _region_containment_share(
    region: RegionInfo, table_bbox: tuple[float, float, float, float]
) -> float:
    """Share of a region bbox covered by a ruled-table bbox.

    Both bboxes use PDF coordinates: ``(left, top, right, bottom)``. The
    denominator is the region area because navigation stripping makes region
    boxes smaller than the ruling-line boxes around them.
    """

    table_left, table_top, table_right, table_bottom = table_bbox
    region_width = max(region.right - region.left, 0.0)
    region_height = max(region.bottom - region.top, 0.0)
    region_area = region_width * region_height
    if region_area <= 0.0 or table_right <= table_left or table_bottom <= table_top:
        return 0.0
    intersection_width = max(
        0.0, min(region.right, table_right) - max(region.left, table_left)
    )
    intersection_height = max(
        0.0, min(region.bottom, table_bottom) - max(region.top, table_top)
    )
    return intersection_width * intersection_height / region_area


def _verified_region_replacements(
    regions: list[RegionInfo],
    region_words: list[list[dict]],
    verified_region_tables: Sequence[
        tuple[tuple[float, float, float, float], str]
    ],
) -> dict[int, tuple[str, RegionInfo]]:
    """Return safe, one-to-one region table replacements.

    Ambiguous geometry is left unchanged: one table may not replace several
    regions, and several tables may not compete for one region. The caller is
    expected to report those cases. This function also reruns the existing
    strict table audit at region scope, so the word ``verified`` in the public
    parameter cannot bypass token preservation.
    """

    from esg_layout_qa import (
        TABLE_MAX_EXTRA_TOKEN_RATIO,
        TABLE_MIN_TOKEN_RECALL,
        _markdown_table_shape,
        _semantic_token_counter,
        _upright_source_tokens,
    )

    normalized: list[tuple[tuple[float, float, float, float], str]] = []
    for bbox, markdown in verified_region_tables:
        try:
            normalized_bbox = tuple(float(value) for value in bbox)
        except (TypeError, ValueError):
            continue
        if len(normalized_bbox) != 4 or not isinstance(markdown, str):
            continue
        normalized.append((normalized_bbox, markdown))

    table_to_regions: list[list[int]] = []
    region_to_tables: list[list[int]] = [[] for _ in regions]
    for table_index, (bbox, _markdown) in enumerate(normalized):
        matched = [
            region_index
            for region_index, region in enumerate(regions)
            if _region_containment_share(region, bbox)
            >= VERIFIED_REGION_MIN_CONTAINMENT
        ]
        table_to_regions.append(matched)
        for region_index in matched:
            region_to_tables[region_index].append(table_index)

    replacements: dict[int, tuple[str, RegionInfo]] = {}
    for table_index, matched_regions in enumerate(table_to_regions):
        if len(matched_regions) != 1:
            continue
        region_index = matched_regions[0]
        if len(region_to_tables[region_index]) != 1:
            continue

        _bbox, markdown = normalized[table_index]
        row_count, column_count = _markdown_table_shape(markdown)
        if row_count < 2 or column_count < 2:
            continue
        source = _upright_source_tokens(region_words[region_index])
        output = _semantic_token_counter(markdown)
        source_count = sum(source.values())
        if source_count == 0:
            continue
        recall = sum((source & output).values()) / source_count
        extra = sum((output - source).values()) / source_count
        if recall < TABLE_MIN_TOKEN_RECALL or extra > TABLE_MAX_EXTRA_TOKEN_RATIO:
            continue

        original = regions[region_index]
        verified = RegionInfo(
            original.top,
            original.bottom,
            original.left,
            original.right,
            "table_verified",
            column_count,
            "verified_region_table_extraction_reused: "
            f"prior_type={original.region_type}; recall={recall:.4f}; "
            f"extra_token_ratio={extra:.4f}",
        )
        replacements[region_index] = (markdown, verified)
    return replacements


def reconstruct_by_regions(
    words: list[dict],
    page_width: float,
    page_height: float,
    verified_table_text: str | None = None,
    verified_region_tables: Sequence[
        tuple[tuple[float, float, float, float], str]
    ]
    | None = None,
) -> RegionReadingOrderResult:
    """Region-aware candidate reading order. Read-only; changes nothing.

    ``verified_table_text`` lets a caller defer to the existing, already
    verified table-extraction path (item 7 of the design) for a page whose
    current audit decision is ``auto_pass_verified_table_extraction``,
    instead of re-deriving table structure here.

    ``verified_region_tables`` is an additive, region-scoped form of that
    hook. Each item is ``((left, top, right, bottom), markdown)``. A ruled
    table box may be slightly larger than its navigation-stripped region, so
    matching uses bbox overlap rather than exact equality: at least 80% of the
    region area must fall inside the table box. Only unique one-table/one-region
    matches are used, and the same strict table token audit is rerun against
    that region's words. All other regions, plus header, footer, and rotated
    text, keep their existing order and text.
    """

    usable = _usable_words(words)
    if page_width <= 0 or page_height <= 0 or not usable:
        return RegionReadingOrderResult(
            "candidate_ready", "", "no_words_or_geometry", (), 0, 0, 1.0, ()
        )

    if verified_table_text is not None:
        region = RegionInfo(0.0, page_height, 0.0, page_width, "table_verified", 0, "verified_table_extraction_reused")
        candidate_counter = _word_counter(verified_table_text)
        source_counter = Counter(str(w.get("text", "")).strip().casefold() for w in usable)
        preserved = sum(min(c, candidate_counter.get(t, 0)) for t, c in source_counter.items())
        ratio = preserved / len(usable) if usable else 1.0
        return RegionReadingOrderResult(
            "candidate_ready",
            verified_table_text,
            "verified_table_extraction_reused",
            (region,),
            len(usable),
            sum(candidate_counter.values()),
            ratio,
            (verified_table_text,),
        )

    linear = _linear_words(usable)
    linear_ids = {id(word) for word in linear}
    rotated = [word for word in usable if id(word) not in linear_ids]

    header_limit = page_height * HEADER_FOOTER_BAND_SHARE
    footer_limit = page_height * (1.0 - HEADER_FOOTER_BAND_SHARE)
    headers = [w for w in linear if _number(w, "bottom", _number(w, "top")) <= header_limit]
    footers = [w for w in linear if _number(w, "top") >= footer_limit]
    header_footer_ids = {id(w) for w in headers} | {id(w) for w in footers}
    body = [w for w in linear if id(w) not in header_footer_ids]

    lines = _lines_with_segments(body, page_width)

    regions: list[RegionInfo] = []
    parts: list[str] = []

    header_text = _lines_to_text_words(headers)
    if header_text:
        parts.append(header_text)

    region_list, part_list, region_word_lists, any_uncertain = _regions_from_lines(
        lines, page_width, page_height
    )

    replacements: dict[int, tuple[str, RegionInfo]] = {}
    if verified_region_tables:
        replacements = _verified_region_replacements(
            region_list, region_word_lists, verified_region_tables
        )
        for region_index, (replacement_text, replacement_info) in replacements.items():
            part_list[region_index] = replacement_text
            region_list[region_index] = replacement_info
        any_uncertain = any(
            region.region_type == "uncertain" for region in region_list
        )

    regions.extend(region_list)
    parts.extend(part_list)

    footer_text = _lines_to_text_words(footers)
    if footer_text:
        parts.append(footer_text)

    if rotated:
        rotated_text = _lines_to_text_words(rotated)
        if rotated_text:
            parts.append("[excluded rotated/decorative text, verbatim, position uncertain]\n" + rotated_text)

    text = "\n\n".join(parts).strip()

    candidate_counter = _word_counter(text)

    # Keep the original checksum byte-for-byte when no local table was used.
    # Markdown punctuation makes that whitespace-token checksum unsuitable for
    # substituted regions, so used paths switch to semantic recall while also
    # checking every non-substituted region with the exact original checksum.
    source_counter = Counter(
        str(w.get("text", "")).strip().casefold() for w in usable
    )
    preserved = sum(
        min(count, candidate_counter.get(token, 0))
        for token, count in source_counter.items()
    )
    ratio = preserved / len(usable) if usable else 1.0
    local_safety_failed = False
    if replacements:
        from esg_layout_qa import _semantic_token_counter

        source_semantic = _semantic_token_counter(
            " ".join(str(word.get("text") or "") for word in usable)
        )
        output_semantic = _semantic_token_counter(text)
        semantic_count = sum(source_semantic.values())
        ratio = (
            sum((source_semantic & output_semantic).values()) / semantic_count
            if semantic_count
            else 1.0
        )
        for region_index, (region_text, words_for_region) in enumerate(
            zip(part_list, region_word_lists)
        ):
            if region_index in replacements:
                continue
            expected = Counter(
                str(word.get("text", "")).strip().casefold()
                for word in words_for_region
            )
            if _word_counter(region_text) != expected:
                local_safety_failed = True
                break

        assigned_ids = [
            id(word) for words_for_region in region_word_lists for word in words_for_region
        ]
        if len(assigned_ids) != len(set(assigned_ids)) or set(assigned_ids) != {
            id(word) for word in body
        }:
            local_safety_failed = True

    status = "candidate_ready"
    reason = "regions_classified"
    if any_uncertain:
        status = "needs_review"
        reason = "one_or_more_regions_uncertain"
    if replacements:
        reason = f"regions_classified_with_verified_region_tables={len(replacements)}"
        if any_uncertain:
            reason += "; one_or_more_regions_uncertain"
    if local_safety_failed:
        status = "needs_review"
        reason = "non_substituted_region_preservation_check_failed"
    if ratio < 1.0:
        status = "needs_review"
        reason = f"word_preservation_check_failed={ratio:.4f}"

    return RegionReadingOrderResult(
        status,
        text,
        reason,
        tuple(regions),
        len(usable),
        sum(candidate_counter.values()),
        ratio,
        tuple(part_list),
    )
