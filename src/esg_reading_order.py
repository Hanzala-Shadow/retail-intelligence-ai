"""Deterministic coordinate-based reading-order reconstruction for ESG PDFs.

PDF text extractors normally emit words in visual row order.  That is unsafe
for a page with several full-height text columns because a sentence can jump
from the left column to the right column and back.  This module repairs only
the simple layouts for which the PDF's word coordinates provide enough
evidence: two to four dense, vertically overlapping columns with stable left
edges.  It deliberately declines sidebars, grids, tables, and irregular pages
so callers can quarantine those instead of inventing an order.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
import re
from statistics import median


Y_TOLERANCE = 3.0
MIN_COLUMN_COUNT = 2
MAX_COLUMN_COUNT = 4
MIN_COLUMN_LINE_COUNT = 6
MIN_COLUMN_WORD_COUNT = 55
MIN_COLUMN_VERTICAL_COVERAGE = 0.28
MIN_COLUMN_VERTICAL_OVERLAP = 0.35
MIN_COLUMN_GAP_SHARE = 0.14
HEADER_FOOTER_BAND_SHARE = 0.06
MIN_PRESERVATION_RATIO = 0.995
# Separates genuinely rotated words (bbox height grows with character count,
# tens to 100+pt) from a font-scaling quirk pdfminer also flags non-upright
# on some ordinary ~7-13pt horizontal text; corpus-measured, worst false
# positive was 13.0pt.
ROTATED_WORD_MIN_HEIGHT = 15.0


@dataclass(frozen=True)
class ReadingOrderResult:
    """Result for one page; ``status`` is safe to persist in QA evidence."""

    status: str
    text: str
    reason: str
    column_count: int
    source_word_count: int
    reconstructed_word_count: int
    preservation_ratio: float


def _number(word: dict, key: str, default: float = 0.0) -> float:
    try:
        return float(word.get(key, default))
    except (TypeError, ValueError):
        return default


def _usable_words(words: list[dict]) -> list[dict]:
    return [word for word in words if str(word.get("text", "")).strip()]


def _line_groups(words: list[dict]) -> list[list[dict]]:
    groups: list[list[dict]] = []
    tops: list[float] = []
    for word in sorted(words, key=lambda item: (_number(item, "top"), _number(item, "x0"))):
        top = _number(word, "top")
        if not groups or abs(top - tops[-1]) > Y_TOLERANCE:
            groups.append([word])
            tops.append(top)
        else:
            groups[-1].append(word)
    return groups


def _line_segments(line: list[dict], page_width: float) -> list[list[dict]]:
    """Split a visual row at the large gutter between independent columns."""

    if not line:
        return []
    # Gaps inside normal PDF text are normally a few points.  The floor keeps
    # justified text together while the relative term scales to report size.
    gap_threshold = max(16.0, page_width * 0.018)
    sorted_line = sorted(line, key=lambda item: _number(item, "x0"))
    segments: list[list[dict]] = [[sorted_line[0]]]
    previous = sorted_line[0]
    for word in sorted_line[1:]:
        gap = _number(word, "x0") - _number(previous, "x1")
        if gap > gap_threshold:
            segments.append([word])
        else:
            segments[-1].append(word)
        previous = word
    return segments


def _cluster_starts(segments: list[list[dict]], page_width: float) -> list[dict]:
    """Find recurring left edges, which are the strongest column signal."""

    tolerance = max(18.0, page_width * 0.035)
    clusters: list[dict] = []
    for segment in sorted(segments, key=lambda item: _number(item[0], "x0")):
        if len(segment) < 2:
            continue
        x0 = _number(segment[0], "x0")
        target = next(
            (cluster for cluster in clusters if abs(x0 - cluster["x"]) <= tolerance),
            None,
        )
        if target is None:
            target = {"x": x0, "starts": [], "words": 0, "tops": []}
            clusters.append(target)
        target["starts"].append(x0)
        target["words"] += len(segment)
        target["tops"].append(_number(segment[0], "top"))
        target["x"] = median(target["starts"])

    return [
        cluster
        for cluster in clusters
        if len(cluster["starts"]) >= MIN_COLUMN_LINE_COUNT
        and cluster["words"] >= MIN_COLUMN_WORD_COUNT
    ]


def _vertical_overlap(first: list[float], second: list[float], page_height: float) -> float:
    if not first or not second or page_height <= 0:
        return 0.0
    first_low, first_high = min(first), max(first)
    second_low, second_high = min(second), max(second)
    shared = max(0.0, min(first_high, second_high) - max(first_low, second_low))
    shorter = min(first_high - first_low, second_high - second_low)
    return shared / shorter if shorter > 0 else 0.0


def _stable_column_starts(
    words: list[dict],
    page_width: float,
    page_height: float,
) -> tuple[list[float], str]:
    groups = _line_groups(words)
    segments = [segment for group in groups for segment in _line_segments(group, page_width)]
    clusters = sorted(_cluster_starts(segments, page_width), key=lambda cluster: cluster["x"])
    if len(clusters) < MIN_COLUMN_COUNT:
        return [], "no_repeating_column_starts"

    minimum_gap = page_width * MIN_COLUMN_GAP_SHARE
    selected: list[dict] = []
    for cluster in clusters:
        if not selected or cluster["x"] - selected[-1]["x"] >= minimum_gap:
            selected.append(cluster)

    if len(selected) < MIN_COLUMN_COUNT:
        return [], "column_starts_not_separated"
    if len(selected) > MAX_COLUMN_COUNT:
        return [], f"too_many_column_starts={len(selected)}"

    for cluster in selected:
        coverage = (max(cluster["tops"]) - min(cluster["tops"])) / max(page_height, 1.0)
        if coverage < MIN_COLUMN_VERTICAL_COVERAGE:
            return [], f"column_vertical_coverage={coverage:.3f}"

    overlap = max(
        _vertical_overlap(first["tops"], second["tops"], page_height)
        for index, first in enumerate(selected)
        for second in selected[index + 1 :]
    )
    if overlap < MIN_COLUMN_VERTICAL_OVERLAP:
        return [], f"column_vertical_overlap={overlap:.3f}"

    # Return each column's LEFTMOST stable start, not the median of its starts.
    # The caller assigns words with ``bisect_right``, so the returned value is a
    # boundary, and a median sits inside its own column: every word on a ragged
    # left edge -- a bullet glyph, an indented run -- then sorts below its own
    # column and is handed to the previous one. On a real page a 0.1pt
    # difference was enough. The clustering above keeps using the median, which
    # is the right summary for judging separation and coverage.
    edges = [float(min(cluster["starts"])) for cluster in selected]
    if edges != sorted(edges):
        return [], "column_edges_not_ordered"
    return edges, "stable_columns"


def _lines_to_text(words: list[dict]) -> str:
    if not words:
        return ""
    return "\n".join(
        " ".join(str(word.get("text", "")).strip() for word in sorted(group, key=lambda item: _number(item, "x0")))
        for group in _line_groups(words)
        if group
    )


def _word_counter(text: str) -> Counter[str]:
    return Counter(part.casefold() for part in text.split() if part)


def canonical_order_text(text: str) -> str:
    """Normalise harmless whitespace so parser and QA can compare page order."""

    return " ".join(text.casefold().split())


def _looks_like_contents_page(words: list[dict], page_height: float) -> bool:
    labels = [str(word.get("text", "")).strip().casefold() for word in words]
    numeric_labels = sum(
        1 for label in labels if re.fullmatch(r"\d{1,3}", label.rstrip(".,"))
    )
    contents_words = [
        word for word in words if str(word.get("text", "")).strip().casefold() == "contents"
    ]
    has_body_contents = any(
        _number(word, "top") > page_height * HEADER_FOOTER_BAND_SHARE
        for word in contents_words
    )
    return numeric_labels >= 8 and (len(contents_words) >= 2 or has_body_contents)


def reconstruct_column_order(
    words: list[dict],
    page_width: float,
    page_height: float,
    structural_grid_risk: bool = False,
) -> ReadingOrderResult:
    """Return left-to-right column order only when the geometry is clear.

    ``not_applicable`` means no material multi-column signal was found.
    ``ambiguous`` means the page looked multi-column but did not meet the
    reconstruction safety conditions.  Callers must not silently index the
    latter without an alternative verified parser output.
    """

    usable = _usable_words(words)
    if page_width <= 0 or page_height <= 0:
        return ReadingOrderResult(
            "not_applicable", "", "insufficient_geometry_or_words", 0, len(usable), 0, 0.0
        )
    if _looks_like_contents_page(usable, page_height):
        return ReadingOrderResult(
            "ambiguous", "", "navigation_contents_layout", 0, len(usable), 0, 0.0
        )
    # A chart, card grid, or dense table can expose repeated text starts but
    # has no defensible single prose order. The caller derives this signal from
    # grid-specific geometry rather than raw vector-curve counts, because some
    # PDFs encode ordinary text as curves.
    if structural_grid_risk:
        return ReadingOrderResult(
            "ambiguous",
            "",
            "structural_grid_or_table_layout",
            0,
            len(usable),
            0,
            0.0,
        )
    if len(usable) < MIN_COLUMN_WORD_COUNT:
        return ReadingOrderResult(
            "not_applicable", "", "insufficient_geometry_or_words", 0, len(usable), 0, 0.0
        )

    starts, reason = _stable_column_starts(usable, page_width, page_height)
    if not starts:
        status = "ambiguous" if reason not in {"no_repeating_column_starts", "column_starts_not_separated"} else "not_applicable"
        return ReadingOrderResult(status, "", reason, 0, len(usable), 0, 0.0)

    header_limit = page_height * HEADER_FOOTER_BAND_SHARE
    footer_limit = page_height * (1.0 - HEADER_FOOTER_BAND_SHARE)
    headers: list[dict] = []
    footers: list[dict] = []
    columns: list[list[dict]] = [[] for _ in starts]

    for word in usable:
        top = _number(word, "top")
        bottom = _number(word, "bottom", top)
        # Rotated (non-upright) text -- a vertical sidebar title, a rotated
        # nav tab -- has a bounding box that spans whatever vertical range the
        # glyph run occupies, not one line's height, so it rarely lands in the
        # header/footer band below; it falls through to column assignment and
        # corrupts body text instead. Drop it before any classification, but
        # only once it's tall enough to be a real rotated run rather than the
        # font-scaling false positive ROTATED_WORD_MIN_HEIGHT guards against.
        if not word.get("upright", True) and (bottom - top) > ROTATED_WORD_MIN_HEIGHT:
            continue
        if bottom <= header_limit:
            headers.append(word)
            continue
        if top >= footer_limit:
            footers.append(word)
            continue
        # A column's left edge is stable, while the right edge can vary with
        # justification.  Assign from the next left edge rather than the
        # midpoint between anchors; otherwise long words at the right edge of
        # a narrow column can be incorrectly pulled into the next column.
        column_index = max(0, bisect_right(starts, _number(word, "x0")) - 1)
        columns[column_index].append(word)

    parts = [_lines_to_text(headers)]
    parts.extend(_lines_to_text(column) for column in columns)
    parts.append(_lines_to_text(footers))
    text = "\n\n".join(part for part in parts if part).strip()

    source_counter = Counter(str(word.get("text", "")).strip().casefold() for word in usable)
    reconstructed_counter = _word_counter(text)
    preserved = sum(
        min(count, reconstructed_counter.get(token, 0)) for token, count in source_counter.items()
    )
    preservation_ratio = preserved / len(usable) if usable else 0.0
    if preservation_ratio < MIN_PRESERVATION_RATIO:
        return ReadingOrderResult(
            "ambiguous",
            "",
            f"word_preservation_ratio={preservation_ratio:.4f}",
            len(starts),
            len(usable),
            sum(reconstructed_counter.values()),
            preservation_ratio,
        )

    return ReadingOrderResult(
        "reconstructed",
        text,
        "stable_columns_left_to_right",
        len(starts),
        len(usable),
        sum(reconstructed_counter.values()),
        preservation_ratio,
    )
