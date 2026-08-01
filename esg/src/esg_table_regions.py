"""Default-off table-box-first reading-order candidate.

The existing region reader discovers prose regions and only then tries to
match a table to one of them.  Real table boxes often cut across several of
those regions.  This module reverses that order:

1. normalise ruled-table candidates supplied by the parser and detect the
   existing unruled anchor blocks;
2. remove table words before any prose-region clustering;
3. keep each table as one atomic unit;
4. cluster and reconstruct the remaining prose around those hard boxes; and
5. order the atomic units with whitespace cuts without ever splitting a table.

Nothing imports this module from ``pdf_parser.py``.  It is a read-only
candidate for measurement and is deliberately default-off.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from esg_layout_qa import _semantic_token_counter
from esg_page_geometry import (
    TABLE_ALIGN_FLOOR,
    TABLE_ALIGN_WIDTH_SHARE,
    persistent_regions,
    table_blocks,
    visual_lines,
)
from esg_reading_order import (
    _line_groups,
    _line_segments,
    _linear_words,
    _number,
    _usable_words,
)
from esg_reading_regions import reconstruct_by_regions


# These are the already-used production region-pass cut floors.  They are
# copied here so this experimental module has no dependency on pdf_parser's
# private _LayoutBlock class or _xy_cut_order implementation.
MIN_X_CUT = 18.0
MIN_Y_CUT = 8.0
Y_CUT_RELATIVE_PRIORITY = 0.65
MAX_CUT_DEPTH = 24

# Near-identical detections from ruled and unruled paths describe one box.
# Ruled boxes win because they carry a cell-aware serialisation.
BOX_DEDUP_IOU = 0.80


@dataclass(frozen=True)
class TableBox:
    """One detected table boundary and its optional cell serialisation."""

    left: float
    top: float
    right: float
    bottom: float
    text: str = ""
    source: str = "ruled"

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return self.left, self.top, self.right, self.bottom


@dataclass
class _Segment:
    index: int
    top: float
    bottom: float
    x0: float
    x1: float
    raw_text: str
    words: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class TableRegionUnit:
    """An atomic table or one reconstructed surrounding prose region."""

    left: float
    top: float
    right: float
    bottom: float
    text: str
    kind: str
    source_word_count: int


@dataclass(frozen=True)
class TableRegionResult:
    status: str
    text: str
    reason: str
    table_boxes: tuple[TableBox, ...]
    units: tuple[TableRegionUnit, ...]
    source_token_count: int
    output_token_count: int
    preservation_ratio: float
    extra_token_ratio: float
    table_markdown_used: int = 0
    table_source_fallbacks: int = 0


def _center_in(box: TableBox, word: dict) -> bool:
    x0 = _number(word, "x0")
    x1 = _number(word, "x1", x0)
    top = _number(word, "top")
    bottom = _number(word, "bottom", top)
    center_x = (x0 + x1) / 2
    center_y = (top + bottom) / 2
    return box.left <= center_x <= box.right and box.top <= center_y <= box.bottom


def _line_text(words: list[dict]) -> str:
    if not words:
        return ""
    return "\n".join(
        " ".join(
            str(word.get("text", "")).strip()
            for word in sorted(group, key=lambda item: _number(item, "x0"))
            if str(word.get("text", "")).strip()
        )
        for group in _line_groups(words)
        if group
    ).strip()


def _bbox_for_words(words: list[dict]) -> tuple[float, float, float, float]:
    return (
        min(_number(word, "x0") for word in words),
        min(_number(word, "top") for word in words),
        max(_number(word, "x1", _number(word, "x0")) for word in words),
        max(
            _number(word, "bottom", _number(word, "top"))
            for word in words
        ),
    )


def _segments(words: list[dict], page_width: float) -> list[_Segment]:
    segments: list[_Segment] = []
    for group in _line_groups(words):
        for piece in _line_segments(group, page_width):
            if not piece:
                continue
            segments.append(
                _Segment(
                    index=len(segments),
                    top=min(_number(word, "top") for word in piece),
                    bottom=max(
                        _number(word, "bottom", _number(word, "top"))
                        for word in piece
                    ),
                    x0=min(_number(word, "x0") for word in piece),
                    x1=max(
                        _number(word, "x1", _number(word, "x0"))
                        for word in piece
                    ),
                    raw_text=" ".join(
                        str(word.get("text", "")).strip()
                        for word in piece
                        if str(word.get("text", "")).strip()
                    ),
                    words=list(piece),
                )
            )
    return segments


def _candidate_box(candidate: Any) -> TableBox | None:
    """Normalise parser objects, comparison-harness dicts, or TableBox values."""

    if isinstance(candidate, TableBox):
        return candidate
    if isinstance(candidate, dict):
        bbox = candidate.get("bbox")
        if bbox is None:
            bbox = (
                candidate.get("x0"),
                candidate.get("top"),
                candidate.get("x1"),
                candidate.get("bottom"),
            )
        text = candidate.get("markdown") or candidate.get("text") or ""
    else:
        bbox = getattr(candidate, "bbox", None)
        if bbox is None:
            bbox = (
                getattr(candidate, "x0", None),
                getattr(candidate, "top", None),
                getattr(candidate, "x1", None),
                getattr(candidate, "bottom", None),
            )
        text = getattr(candidate, "markdown", None) or getattr(candidate, "text", "")
    try:
        left, top, right, bottom = (float(value) for value in bbox)
    except (TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return TableBox(left, top, right, bottom, str(text or ""), "ruled")


def _iou(first: TableBox, second: TableBox) -> float:
    left = max(first.left, second.left)
    top = max(first.top, second.top)
    right = min(first.right, second.right)
    bottom = min(first.bottom, second.bottom)
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    first_area = (first.right - first.left) * (first.bottom - first.top)
    second_area = (second.right - second.left) * (second.bottom - second.top)
    union = first_area + second_area - intersection
    return intersection / union if union > 0 else 0.0


def _unruled_boxes(segments: list[_Segment], page_width: float) -> list[TableBox]:
    """Turn existing anchor blocks into conservative word-tight boxes.

    Ruled lines remain the primary driver.  For an unruled block, only
    segments whose left edge sits on a reported table anchor define the box;
    this avoids swallowing prose in another column that happens to share the
    same visual rows.
    """

    if not segments:
        return []
    lines = visual_lines(segments)
    blocks = table_blocks(segments, page_width, lines)
    lookup = {segment.index: segment for segment in segments}
    tolerance = max(TABLE_ALIGN_FLOOR, page_width * TABLE_ALIGN_WIDTH_SHARE)
    boxes: list[TableBox] = []
    for block in blocks:
        members = [
            lookup[index]
            for line_index in block.lines
            for index in lines[line_index]
            if any(abs(lookup[index].x0 - anchor) <= tolerance for anchor in block.left_anchors)
        ]
        if not members:
            continue
        boxes.append(
            TableBox(
                min(member.x0 for member in members),
                min(member.top for member in members),
                max(member.x1 for member in members),
                max(member.bottom for member in members),
                "",
                "unruled_anchor",
            )
        )
    return boxes


def detect_table_boxes(
    words: list[dict],
    page_width: float,
    ruled_tables: Sequence[Any] | None = None,
    *,
    include_unruled: bool = True,
) -> list[TableBox]:
    """Detect table boundaries before any prose-region decomposition."""

    linear = _linear_words(_usable_words(words))
    ruled = [box for item in (ruled_tables or ()) if (box := _candidate_box(item))]
    unruled = _unruled_boxes(_segments(linear, page_width), page_width) if include_unruled else []

    # Stable priority: ruled/cell-aware boxes first, then larger boxes, then
    # page position.  Near-identical lower-priority detections are discarded.
    ordered = sorted(
        ruled + unruled,
        key=lambda box: (
            0 if box.source == "ruled" else 1,
            -((box.right - box.left) * (box.bottom - box.top)),
            box.top,
            box.left,
        ),
    )
    kept: list[TableBox] = []
    for box in ordered:
        if any(_iou(box, existing) >= BOX_DEDUP_IOU for existing in kept):
            continue
        if not any(_center_in(box, word) for word in linear):
            continue
        kept.append(box)
    return sorted(kept, key=lambda box: (box.top, box.left))


def _semantic_equal(first: str, second: str) -> bool:
    return _semantic_token_counter(first) == _semantic_token_counter(second)


def _gaps(units: list[TableRegionUnit], axis: str) -> list[tuple[float, float, float]]:
    intervals = sorted(
        (unit.left, unit.right) if axis == "x" else (unit.top, unit.bottom)
        for unit in units
    )
    merged: list[list[float]] = []
    for start, end in intervals:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [
        (merged[index][1], merged[index + 1][0], merged[index + 1][0] - merged[index][1])
        for index in range(len(merged) - 1)
    ]


def _order_units(
    units: list[TableRegionUnit],
    page_width: float,
    page_height: float,
    depth: int = 0,
) -> list[TableRegionUnit]:
    """Whitespace-cut atomic units; a table is never opened or subdivided."""

    if len(units) <= 1 or depth >= MAX_CUT_DEPTH:
        return units
    x_gap = max(_gaps(units, "x"), key=lambda item: item[2], default=None)
    y_gap = max(_gaps(units, "y"), key=lambda item: item[2], default=None)
    valid_x = bool(x_gap and x_gap[2] >= MIN_X_CUT)
    valid_y = bool(y_gap and y_gap[2] >= MIN_Y_CUT)
    if valid_y and (
        not valid_x
        or y_gap[2] / max(page_height, 1.0)
        >= Y_CUT_RELATIVE_PRIORITY * x_gap[2] / max(page_width, 1.0)
    ):
        cut = (y_gap[0] + y_gap[1]) / 2
        upper = [unit for unit in units if unit.bottom <= cut]
        lower = [unit for unit in units if unit.top >= cut]
        if upper and lower and len(upper) + len(lower) == len(units):
            return _order_units(upper, page_width, page_height, depth + 1) + _order_units(
                lower, page_width, page_height, depth + 1
            )
    if valid_x:
        cut = (x_gap[0] + x_gap[1]) / 2
        left = [unit for unit in units if unit.right <= cut]
        right = [unit for unit in units if unit.left >= cut]
        if left and right and len(left) + len(right) == len(units):
            return _order_units(left, page_width, page_height, depth + 1) + _order_units(
                right, page_width, page_height, depth + 1
            )
    return sorted(units, key=lambda unit: (unit.top, unit.left))


def _metrics(source_text: str, output_text: str) -> tuple[int, int, float, float]:
    source = _semantic_token_counter(source_text)
    output = _semantic_token_counter(output_text)
    source_count = sum(source.values())
    output_count = sum(output.values())
    preserved = sum((source & output).values())
    extra = sum((output - source).values())
    return (
        source_count,
        output_count,
        preserved / source_count if source_count else 1.0,
        extra / source_count if source_count else 0.0,
    )


def reconstruct_table_regions(
    words: list[dict],
    page_width: float,
    page_height: float,
    ruled_tables: Sequence[Any] | None = None,
    *,
    include_unruled: bool = True,
    fallback_text: str | None = None,
) -> TableRegionResult:
    """Build a table-first, region-second candidate without production writes."""

    usable = _usable_words(words)
    if page_width <= 0 or page_height <= 0 or not usable:
        return TableRegionResult("candidate_ready", "", "no_words_or_geometry", (), (), 0, 0, 1.0, 0.0)

    boxes = detect_table_boxes(
        usable, page_width, ruled_tables, include_unruled=include_unruled
    )
    if not boxes:
        baseline = reconstruct_by_regions(usable, page_width, page_height)
        baseline_text = baseline.text if fallback_text is None else fallback_text
        source_text = " ".join(str(word.get("text", "")) for word in usable)
        source_count, output_count, preservation, extra = _metrics(source_text, baseline_text)
        return TableRegionResult(
            baseline.status,
            baseline_text,
            "no_table_box_fell_back_to_reader_b",
            (),
            (),
            source_count,
            output_count,
            preservation,
            extra,
        )

    linear = _linear_words(usable)
    linear_ids = {id(word) for word in linear}
    rotated = [word for word in usable if id(word) not in linear_ids]

    # Assign a word to at most one table.  Ruled boxes have priority from
    # detect_table_boxes; position order then makes the result deterministic.
    by_box: list[list[dict]] = [[] for _ in boxes]
    table_word_ids: set[int] = set()
    for word in linear:
        for index, box in enumerate(boxes):
            if _center_in(box, word):
                by_box[index].append(word)
                table_word_ids.add(id(word))
                break

    units: list[TableRegionUnit] = []
    markdown_used = 0
    source_fallbacks = 0
    active_boxes: list[TableBox] = []
    for box, table_words in zip(boxes, by_box):
        if not table_words:
            continue
        source_text = _line_text(table_words)
        if box.text and _semantic_equal(source_text, box.text):
            table_text = box.text.strip()
            markdown_used += 1
        else:
            table_text = source_text
            source_fallbacks += 1
        units.append(
            TableRegionUnit(
                box.left,
                box.top,
                box.right,
                box.bottom,
                table_text,
                "table",
                len(table_words),
            )
        )
        active_boxes.append(box)

    prose_words = [word for word in linear if id(word) not in table_word_ids]
    prose_segments = _segments(prose_words, page_width)
    if prose_segments:
        lines = visual_lines(prose_segments)
        for region in persistent_regions(prose_segments, page_width, lines):
            region_words = [
                word
                for segment_index in region.members
                for word in prose_segments[segment_index].words
            ]
            if not region_words:
                continue
            result = reconstruct_by_regions(region_words, page_width, page_height)
            left, top, right, bottom = _bbox_for_words(region_words)
            units.append(
                TableRegionUnit(
                    left,
                    top,
                    right,
                    bottom,
                    result.text,
                    "prose",
                    len(region_words),
                )
            )

    ordered = _order_units(units, page_width, page_height)
    parts = [unit.text.strip() for unit in ordered if unit.text.strip()]
    if rotated:
        rotated_text = _line_text(rotated)
        if rotated_text:
            parts.append(rotated_text)
    text = "\n\n".join(parts).strip()

    source_text = " ".join(str(word.get("text", "")) for word in usable)
    source_count, output_count, preservation, extra = _metrics(source_text, text)
    ready = preservation == 1.0 and extra == 0.0 and output_count == source_count
    return TableRegionResult(
        "candidate_ready" if ready else "needs_review",
        text,
        (
            f"table_box_first: tables={len(active_boxes)}; units={len(units)}"
            if ready
            else (
                f"word_preservation_failed={preservation:.4f};"
                f"extra={extra:.4f}; source={source_count}; output={output_count}"
            )
        ),
        tuple(active_boxes),
        tuple(ordered),
        source_count,
        output_count,
        preservation,
        extra,
        markdown_used,
        source_fallbacks,
    )


__all__ = [
    "BOX_DEDUP_IOU",
    "TableBox",
    "TableRegionResult",
    "TableRegionUnit",
    "detect_table_boxes",
    "reconstruct_table_regions",
]
