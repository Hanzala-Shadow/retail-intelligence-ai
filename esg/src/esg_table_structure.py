"""Cell-level table provenance and fail-closed validation.

The PDF parser used to treat a table as a block of Markdown.  That is not
enough to prove that a number belongs to the right label.  This module keeps
the page word boxes as the source of truth and validates every word-to-cell
assignment before a table can be used for embeddings.

The module is deliberately independent of a PDF library.  A small adapter at
the bottom accepts the objects returned by pdfplumber's ``find_tables``.  This
makes the rules testable with synthetic pages and lets another extractor be
compared against the same contract.

Important design rule: extracted cell strings are evidence, not authority.
When a table extractor omits or swaps cell text but the cell geometry is clear,
the table is marked ``reconstructable`` and its output is rebuilt from the
source word boxes.  When geometry is unclear, the result is not eligible for
embedding.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import re
import unicodedata
from typing import Any, Mapping, Sequence


Y_TOLERANCE = 3.0
CELL_EDGE_TOLERANCE = 1.5
MIN_WORD_OVERLAP = 0.50
AMBIGUOUS_OVERLAP = 0.20
AMBIGUITY_SCORE_MARGIN = 0.15
ROTATED_WORD_MIN_HEIGHT = 15.0

TOKEN_RE = re.compile(
    r"[A-Za-z0-9]+(?:[./:&'+\-][A-Za-z0-9]+)*|[%$\u20AC\u00A3]|[^\W_]",
    re.IGNORECASE,
)
NUMBER_RE = re.compile(
    r"^[+\-~<>]?[$\u20AC\u00A3]?\(?\d[\d,]*(?:\.\d+)?[KMBkmb]?\+?%?\)?\.?$",
    re.IGNORECASE,
)
TEMPORAL_RE = re.compile(
    r"^(?:(?:FY|CY)?(?:19|20)\d{2}|(?:FY|CY)\d{2}|Q[1-4])$",
    re.IGNORECASE,
)


class TableNotEmbeddingEligible(ValueError):
    """Raised when a caller tries to serialize an unverified table."""


@dataclass(frozen=True)
class CellBox:
    """One physical cell rectangle.

    ``row_span`` and ``column_span`` are explicit because merged headers must
    never be silently treated as ordinary one-column cells.
    """

    row_index: int
    column_index: int
    bbox: tuple[float, float, float, float]
    row_span: int = 1
    column_span: int = 1
    inferred: bool = False


@dataclass(frozen=True)
class ValidatedCell:
    row_index: int
    column_index: int
    bbox: tuple[float, float, float, float]
    text: str
    word_ids: tuple[Any, ...]
    extracted_text: str = ""
    row_span: int = 1
    column_span: int = 1
    inferred_bbox: bool = False


@dataclass(frozen=True)
class TableValidation:
    """Audit result for one table candidate."""

    status: str
    source: str
    table_bbox: tuple[float, float, float, float] | None
    row_count: int
    column_count: int
    cell_count: int
    nonempty_cell_count: int
    source_word_count: int
    assigned_word_count: int
    unassigned_word_ids: tuple[Any, ...]
    ambiguous_word_ids: tuple[Any, ...]
    ignored_word_ids: tuple[Any, ...]
    extracted_matches: bool
    geometry_verified: bool
    embedding_eligible: bool
    header_status: str
    header_rows: tuple[int, ...]
    reason_codes: tuple[str, ...]
    cells: tuple[ValidatedCell, ...]

    @property
    def word_recall(self) -> float:
        return self.assigned_word_count / max(self.source_word_count, 1)

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-friendly evidence for an audit artifact."""

        return {
            "status": self.status,
            "source": self.source,
            "table_bbox": list(self.table_bbox) if self.table_bbox else None,
            "row_count": self.row_count,
            "column_count": self.column_count,
            "cell_count": self.cell_count,
            "nonempty_cell_count": self.nonempty_cell_count,
            "source_word_count": self.source_word_count,
            "assigned_word_count": self.assigned_word_count,
            "word_recall": round(self.word_recall, 6),
            "unassigned_word_ids": list(self.unassigned_word_ids),
            "ambiguous_word_ids": list(self.ambiguous_word_ids),
            "ignored_word_ids": list(self.ignored_word_ids),
            "extracted_matches": self.extracted_matches,
            "geometry_verified": self.geometry_verified,
            "embedding_eligible": self.embedding_eligible,
            "header_status": self.header_status,
            "header_rows": list(self.header_rows),
            "reason_codes": list(self.reason_codes),
            "cells": [
                {
                    "row_index": cell.row_index,
                    "column_index": cell.column_index,
                    "bbox": list(cell.bbox),
                    "text": cell.text,
                    "word_ids": list(cell.word_ids),
                    "extracted_text": cell.extracted_text,
                    "row_span": cell.row_span,
                    "column_span": cell.column_span,
                    "inferred_bbox": cell.inferred_bbox,
                }
                for cell in self.cells
            ],
        }

    def to_markdown(self) -> str:
        """Serialize only a verified/reconstructable table.

        Synthetic ``column_N`` names are explicit.  They are never presented
        as if they came from the PDF.
        """

        _require_embedding_eligible(self)
        by_row: dict[int, list[ValidatedCell]] = defaultdict(list)
        for cell in self.cells:
            by_row[cell.row_index].append(cell)
        width = self.column_count
        headers = _header_paths(self)
        if not headers:
            headers = [f"column_{index + 1}" for index in range(width)]
        rendered = ["| " + " | ".join(_escape_markdown(value) for value in headers) + " |"]
        rendered.append("| " + " | ".join("---" for _ in range(width)) + " |")
        for row_index in range(self.row_count):
            if row_index in self.header_rows:
                continue
            cells = {cell.column_index: cell for cell in by_row.get(row_index, [])}
            values = [cells.get(index).text if index in cells else "" for index in range(width)]
            rendered.append("| " + " | ".join(_escape_markdown(value) for value in values) + " |")
        return "\n".join(rendered)

    def to_embedding_records(
        self,
        *,
        document_id: str = "",
        page_number: int | None = None,
        table_id: str = "",
        table_title: str = "",
    ) -> list[dict[str, Any]]:
        """Create row-level, provenance-backed embedding records.

        Each record repeats the column header path and row label.  A vector
        built from one cell therefore does not depend on a neighboring chunk to
        explain what the value means.
        """

        _require_embedding_eligible(self)
        header_paths = _header_paths(self)
        by_row: dict[int, list[ValidatedCell]] = defaultdict(list)
        for cell in self.cells:
            by_row[cell.row_index].append(cell)

        records: list[dict[str, Any]] = []
        for row_index in range(self.row_count):
            if row_index in self.header_rows:
                continue
            row_cells = sorted(by_row.get(row_index, []), key=lambda cell: cell.column_index)
            if not row_cells or not any(cell.text for cell in row_cells):
                continue
            row_label = next((cell.text for cell in row_cells if cell.text), "")
            rendered_cells = []
            row_word_ids: list[Any] = []
            for cell in row_cells:
                if not cell.text:
                    continue
                header = (
                    header_paths[cell.column_index]
                    if cell.column_index < len(header_paths)
                    else f"column_{cell.column_index + 1}"
                )
                rendered_cells.append(f"{header}: {cell.text}")
                row_word_ids.extend(cell.word_ids)
            text_parts = ["Table row."]
            if table_title.strip():
                text_parts.append(f"Table: {table_title.strip()}.")
            if row_label:
                text_parts.append(f"Row label: {row_label}.")
            text_parts.append("; ".join(rendered_cells))
            records.append(
                {
                    "document_id": document_id,
                    "page": page_number,
                    "table_id": table_id,
                    "table_title": table_title,
                    "row_index": row_index,
                    "row_label": row_label,
                    "header_status": self.header_status,
                    "text": " ".join(text_parts),
                    "source_word_ids": row_word_ids,
                    "source_bbox": list(_row_bbox(row_cells)),
                    "table_status": self.status,
                }
            )
        return records


def validate_table(
    *,
    words: Sequence[Mapping[str, Any]],
    cells: Sequence[Sequence[CellBox | Sequence[float] | None]],
    extracted_rows: Sequence[Sequence[Any]] | None = None,
    table_bbox: Sequence[float] | None = None,
    page_bbox: Sequence[float] | None = None,
    source: str = "geometry",
    header_rows: Sequence[int] | None = None,
    infer_headers: bool = False,
) -> TableValidation:
    """Validate cell ownership and build a source-backed table structure.

    The function fails closed on any readable word inside the table box that
    has no unique cell.  It also checks the extractor's strings cell by cell.
    Extractor mismatches are repairable only when geometry still gives every
    word a unique owner; repaired output comes from the boxes, never from the
    mismatched strings.
    """

    reason_codes: list[str] = []
    cell_boxes: list[CellBox] = []
    row_count = len(cells)
    column_count = 0
    for row_index, row in enumerate(cells):
        for column_index, raw in enumerate(row):
            column_count = max(column_count, column_index + 1)
            cell = _coerce_cell(raw, row_index, column_index)
            if cell is not None:
                cell_boxes.append(cell)
                column_count = max(column_count, cell.column_index + cell.column_span)

    normalized_table_bbox = _normalize_bbox(table_bbox) if table_bbox is not None else None
    if normalized_table_bbox is None and cell_boxes:
        normalized_table_bbox = _union_bbox(cell.bbox for cell in cell_boxes)
    if not cell_boxes:
        reason_codes.append("no_cell_geometry")

    valid_cells = [cell for cell in cell_boxes if _valid_bbox(cell.bbox)]
    if len(valid_cells) != len(cell_boxes):
        reason_codes.append("invalid_cell_geometry")
    if normalized_table_bbox is None or not _valid_bbox(normalized_table_bbox):
        reason_codes.append("invalid_table_bbox")

    reason_codes.extend(
        _append_candidate_reasons(
            normalized_table_bbox,
            _normalize_bbox(page_bbox) if page_bbox is not None else None,
            row_count,
            column_count,
            len(cell_boxes),
        )
    )

    active_words: list[tuple[Any, Mapping[str, Any]]] = []
    ignored_word_ids: list[Any] = []
    if normalized_table_bbox is not None and _valid_bbox(normalized_table_bbox):
        for fallback_id, word in enumerate(words):
            word_id = word.get("idx", fallback_id)
            text = str(word.get("text") or "").strip()
            if not text:
                continue
            if _is_rotated(word):
                ignored_word_ids.append(word_id)
                continue
            if _center_in(normalized_table_bbox, word):
                active_words.append((word_id, word))

    assignments: dict[Any, int] = {}
    unassigned: list[Any] = []
    ambiguous: list[Any] = []
    for word_id, word in active_words:
        candidates = _cell_candidates(word, valid_cells)
        if not candidates:
            unassigned.append(word_id)
            continue
        if len(candidates) > 1:
            best = candidates[0][1]
            second = candidates[1][1]
            if second >= AMBIGUOUS_OVERLAP or best - second < AMBIGUITY_SCORE_MARGIN:
                ambiguous.append(word_id)
                continue
        assignments[word_id] = valid_cells.index(candidates[0][0])

    words_by_cell: dict[int, list[tuple[Any, Mapping[str, Any]]]] = defaultdict(list)
    for word_id, word in active_words:
        cell_index = assignments.get(word_id)
        if cell_index is not None:
            words_by_cell[cell_index].append((word_id, word))

    extracted_matches = True
    validated_cells: list[ValidatedCell] = []
    extracted_rows = extracted_rows or []
    for cell_index, cell in enumerate(valid_cells):
        assigned = sorted(
            words_by_cell.get(cell_index, []),
            key=lambda pair: (_number(pair[1], "top"), _number(pair[1], "x0")),
        )
        text = _words_to_text([word for _, word in assigned])
        extracted_text = _extracted_cell_text(extracted_rows, cell.row_index, cell.column_index)
        if _token_counter(text) != _token_counter(extracted_text):
            extracted_matches = False
        validated_cells.append(
            ValidatedCell(
                row_index=cell.row_index,
                column_index=cell.column_index,
                bbox=cell.bbox,
                text=text,
                word_ids=tuple(word_id for word_id, _ in assigned),
                extracted_text=extracted_text,
                row_span=cell.row_span,
                column_span=cell.column_span,
                inferred_bbox=cell.inferred,
            )
        )

    if not extracted_matches:
        reason_codes.append("extractor_cell_text_mismatch")
    if unassigned:
        reason_codes.append("readable_words_without_cell_owner")
    if ambiguous:
        reason_codes.append("word_has_multiple_possible_cell_owners")
    if ignored_word_ids:
        reason_codes.append("rotated_words_excluded")

    if not active_words:
        reason_codes.append("no_readable_words_in_table_bbox")
    if len(assignments) != len(set(assignments)):
        reason_codes.append("duplicate_word_assignment")

    _append_geometry_reasons(valid_cells, row_count, reason_codes)

    selected_header_rows: tuple[int, ...]
    if header_rows is not None:
        selected_header_rows = tuple(sorted({int(row) for row in header_rows if 0 <= int(row) < row_count}))
        header_status = "declared" if selected_header_rows else "declared_empty"
    elif infer_headers:
        selected_header_rows, header_status = _infer_header_rows(validated_cells, row_count)
    else:
        selected_header_rows, header_status = (), "not_declared"

    if header_status == "ambiguous":
        reason_codes.append("header_rows_ambiguous")
    if header_status != "declared":
        # Inference is useful for review hints, but it is not proof.  A caller
        # must declare header rows before a header path can enter an embedding.
        reason_codes.append("header_rows_not_declared")

    nonempty_cell_count = sum(bool(cell.text) for cell in validated_cells)
    geometry_failures = {
        "no_cell_geometry",
        "invalid_cell_geometry",
        "invalid_table_bbox",
        "readable_words_without_cell_owner",
        "word_has_multiple_possible_cell_owners",
        "invalid_row_geometry",
        "invalid_column_geometry",
        "overlapping_cells_without_span",
        "no_readable_words_in_table_bbox",
        "insufficient_table_shape",
        "table_too_large",
        "table_out_of_page_bounds",
        "table_too_small",
    }
    geometry_verified = not any(code in geometry_failures for code in reason_codes)
    embedding_eligible = (
        geometry_verified
        and row_count >= 1
        and column_count >= 1
        and nonempty_cell_count > 0
        and header_status == "declared"
    )
    if header_status != "declared":
        reason_codes.append("header_rows_required_for_embedding")
        embedding_eligible = False

    if not embedding_eligible:
        status = "review"
    elif extracted_matches:
        status = "verified"
    else:
        status = "reconstructable"

    return TableValidation(
        status=status,
        source=source,
        table_bbox=normalized_table_bbox,
        row_count=row_count,
        column_count=column_count,
        cell_count=len(cell_boxes),
        nonempty_cell_count=nonempty_cell_count,
        source_word_count=len(active_words),
        assigned_word_count=len(assignments),
        unassigned_word_ids=tuple(unassigned),
        ambiguous_word_ids=tuple(ambiguous),
        ignored_word_ids=tuple(ignored_word_ids),
        extracted_matches=extracted_matches,
        geometry_verified=geometry_verified,
        embedding_eligible=embedding_eligible,
        header_status=header_status,
        header_rows=selected_header_rows,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        cells=tuple(validated_cells),
    )


def validate_pdfplumber_table(
    table: Any,
    words: Sequence[Mapping[str, Any]],
    *,
    page_bbox: Sequence[float] | None = None,
    source: str = "pdfplumber",
    header_rows: Sequence[int] | None = None,
    infer_headers: bool = False,
) -> TableValidation:
    """Adapt a pdfplumber ``Table`` to :func:`validate_table`."""

    table_rows = list(getattr(table, "rows", []) or [])
    cells = [
        [
            _coerce_cell(raw, row_index, column_index)
            for column_index, raw in enumerate(getattr(row, "cells", []) or [])
        ]
        for row_index, row in enumerate(table_rows)
    ]
    try:
        extracted = table.extract() or []
    except Exception:
        extracted = []
    return validate_table(
        words=words,
        cells=cells,
        extracted_rows=extracted,
        table_bbox=getattr(table, "bbox", None),
        page_bbox=page_bbox,
        source=source,
        header_rows=header_rows,
        infer_headers=infer_headers,
    )


def _coerce_cell(
    raw: CellBox | Sequence[float] | None,
    row_index: int,
    column_index: int,
) -> CellBox | None:
    if raw is None:
        return None
    if isinstance(raw, CellBox):
        return raw
    bbox = getattr(raw, "bbox", raw)
    normalized = _normalize_bbox(bbox)
    if normalized is None:
        return CellBox(row_index, column_index, (0.0, 0.0, 0.0, 0.0))
    return CellBox(row_index, column_index, normalized)


def _normalize_bbox(value: Sequence[float] | None) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    try:
        values = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None
    if len(values) != 4:
        return None
    return values  # type: ignore[return-value]


def _valid_bbox(bbox: tuple[float, float, float, float]) -> bool:
    return bbox[2] > bbox[0] and bbox[3] > bbox[1]


def _union_bbox(boxes: Sequence[tuple[float, float, float, float]] | Any) -> tuple[float, float, float, float] | None:
    values = list(boxes)
    if not values:
        return None
    return (
        min(box[0] for box in values),
        min(box[1] for box in values),
        max(box[2] for box in values),
        max(box[3] for box in values),
    )


def _number(word: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    try:
        return float(word.get(key, default))
    except (TypeError, ValueError):
        return default


def _is_rotated(word: Mapping[str, Any]) -> bool:
    if word.get("upright", True) is not False:
        return False
    return _number(word, "bottom") - _number(word, "top") > ROTATED_WORD_MIN_HEIGHT


def _center_in(bbox: tuple[float, float, float, float], word: Mapping[str, Any]) -> bool:
    center_x = (_number(word, "x0") + _number(word, "x1", _number(word, "x0"))) / 2
    center_y = (_number(word, "top") + _number(word, "bottom", _number(word, "top"))) / 2
    return bbox[0] - CELL_EDGE_TOLERANCE <= center_x <= bbox[2] + CELL_EDGE_TOLERANCE and bbox[1] - CELL_EDGE_TOLERANCE <= center_y <= bbox[3] + CELL_EDGE_TOLERANCE


def _intersection_area(bbox: tuple[float, float, float, float], word: Mapping[str, Any]) -> float:
    x0 = max(bbox[0], _number(word, "x0"))
    x1 = min(bbox[2], _number(word, "x1", _number(word, "x0")))
    top = max(bbox[1], _number(word, "top"))
    bottom = min(bbox[3], _number(word, "bottom", _number(word, "top")))
    return max(0.0, x1 - x0) * max(0.0, bottom - top)


def _cell_candidates(
    word: Mapping[str, Any], cells: Sequence[CellBox]
) -> list[tuple[CellBox, float]]:
    word_area = max(
        (_number(word, "x1", _number(word, "x0")) - _number(word, "x0"))
        * (_number(word, "bottom", _number(word, "top")) - _number(word, "top")),
        1e-9,
    )
    candidates: list[tuple[CellBox, float]] = []
    for cell in cells:
        intersection_share = _intersection_area(cell.bbox, word) / word_area
        if _center_in(cell.bbox, word) or intersection_share >= MIN_WORD_OVERLAP:
            candidates.append((cell, intersection_share))
    return sorted(candidates, key=lambda item: item[1], reverse=True)


def _words_to_text(words: Sequence[Mapping[str, Any]]) -> str:
    if not words:
        return ""
    groups: list[list[Mapping[str, Any]]] = []
    group_tops: list[float] = []
    for word in sorted(words, key=lambda item: (_number(item, "top"), _number(item, "x0"))):
        top = _number(word, "top")
        if not groups or abs(top - group_tops[-1]) > Y_TOLERANCE:
            groups.append([word])
            group_tops.append(top)
        else:
            groups[-1].append(word)
    return "\n".join(
        " ".join(str(word.get("text") or "").strip() for word in group).strip()
        for group in groups
        if group
    ).strip()


def _extracted_cell_text(rows: Sequence[Sequence[Any]], row_index: int, column_index: int) -> str:
    if row_index >= len(rows) or column_index >= len(rows[row_index]):
        return ""
    value = rows[row_index][column_index]
    return "" if value is None else str(value).strip()


def _semantic_tokens(text: str) -> list[str]:
    value = unicodedata.normalize("NFKC", text or "")
    return [token.casefold() for token in TOKEN_RE.findall(value)]


def _token_counter(text: str) -> Counter[str]:
    return Counter(_semantic_tokens(text))


def _append_candidate_reasons(
    table_bbox: tuple[float, float, float, float] | None,
    page_bbox: tuple[float, float, float, float] | None,
    row_count: int,
    column_count: int,
    cell_count: int,
) -> list[str]:
    """Apply cheap table-candidacy filters before semantic serialization.

    A PDF table finder often returns page borders, footer bands, and one-row
    header fragments.  Cell ownership can be perfect for those objects while
    the object is still not a table.  The caller adds the returned codes to the
    shared reason list; the function is kept separate so the thresholds are
    visible and testable.
    """

    reason_codes: list[str] = []
    if row_count < 2 or column_count < 2 or cell_count < 2:
        reason_codes.append("insufficient_table_shape")
    if table_bbox is not None and _valid_bbox(table_bbox):
        width = table_bbox[2] - table_bbox[0]
        height = table_bbox[3] - table_bbox[1]
        if width < 30.0 or height < 12.0:
            reason_codes.append("table_too_small")
        if page_bbox is not None and _valid_bbox(page_bbox):
            page_width = page_bbox[2] - page_bbox[0]
            page_height = page_bbox[3] - page_bbox[1]
            area_ratio = (width * height) / max(page_width * page_height, 1.0)
            if area_ratio > 0.80:
                reason_codes.append("table_too_large")
            if (
                table_bbox[0] < page_bbox[0] - 1.0
                or table_bbox[1] < page_bbox[1] - 1.0
                or table_bbox[2] > page_bbox[2] + 1.0
                or table_bbox[3] > page_bbox[3] + 1.0
            ):
                reason_codes.append("table_out_of_page_bounds")
    return reason_codes


def _append_geometry_reasons(
    cells: Sequence[CellBox], row_count: int, reason_codes: list[str]
) -> None:
    if not cells:
        return
    row_tops: list[float] = []
    for row_index in range(row_count):
        row_cells = [cell for cell in cells if cell.row_index == row_index]
        if row_cells:
            row_tops.append(min(cell.bbox[1] for cell in row_cells))
    if any(next_top < top - CELL_EDGE_TOLERANCE for top, next_top in zip(row_tops, row_tops[1:])):
        reason_codes.append("invalid_row_geometry")

    column_lefts: list[float] = []
    max_column = max(cell.column_index + cell.column_span for cell in cells)
    for column_index in range(max_column):
        column_cells = [cell for cell in cells if cell.column_index == column_index]
        if column_cells:
            column_lefts.append(min(cell.bbox[0] for cell in column_cells))
    if any(next_left < left - CELL_EDGE_TOLERANCE for left, next_left in zip(column_lefts, column_lefts[1:])):
        reason_codes.append("invalid_column_geometry")

    for index, first in enumerate(cells):
        for second in cells[index + 1 :]:
            if first.row_index != second.row_index:
                continue
            if first.column_span > 1 or second.column_span > 1:
                continue
            overlap = min(first.bbox[2], second.bbox[2]) - max(first.bbox[0], second.bbox[0])
            if overlap > CELL_EDGE_TOLERANCE:
                reason_codes.append("overlapping_cells_without_span")
                return


def _infer_header_rows(
    cells: Sequence[ValidatedCell], row_count: int
) -> tuple[tuple[int, ...], str]:
    by_row: dict[int, list[ValidatedCell]] = defaultdict(list)
    for cell in cells:
        by_row[cell.row_index].append(cell)
    candidates: list[int] = []
    for row_index in range(min(row_count, 4)):
        row_cells = [cell for cell in by_row.get(row_index, []) if cell.text]
        if len(row_cells) < 2:
            continue
        label_like = sum(
            any(char.isalpha() for char in cell.text) or any(
                TEMPORAL_RE.fullmatch(token) for token in _semantic_tokens(cell.text)
            )
            for cell in row_cells
        )
        data_like = sum(
            1 for cell in row_cells if all(NUMBER_RE.fullmatch(token) for token in _semantic_tokens(cell.text))
        )
        if label_like / len(row_cells) >= 0.50 and data_like / len(row_cells) <= 0.50:
            candidates.append(row_index)
    if not candidates:
        return (), "absent_or_unknown"
    if len(candidates) == 1:
        return (candidates[0],), "inferred"
    if candidates == list(range(candidates[0], candidates[-1] + 1)):
        return tuple(candidates), "inferred_multirow"
    return (), "ambiguous"


def _header_paths(validation: TableValidation) -> list[str]:
    if not validation.header_rows:
        return []
    by_key = {(cell.row_index, cell.column_index): cell for cell in validation.cells}
    headers = []
    for column_index in range(validation.column_count):
        values = [
            by_key[(row_index, column_index)].text
            for row_index in validation.header_rows
            if (row_index, column_index) in by_key and by_key[(row_index, column_index)].text
        ]
        headers.append(" / ".join(values) if values else f"column_{column_index + 1}")
    return headers


def _row_bbox(row_cells: Sequence[ValidatedCell]) -> tuple[float, float, float, float]:
    return (
        min(cell.bbox[0] for cell in row_cells),
        min(cell.bbox[1] for cell in row_cells),
        max(cell.bbox[2] for cell in row_cells),
        max(cell.bbox[3] for cell in row_cells),
    )


def _escape_markdown(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().replace("|", r"\|")


def _require_embedding_eligible(validation: TableValidation) -> None:
    if not validation.embedding_eligible:
        reasons = ", ".join(validation.reason_codes) or "unknown_validation_failure"
        raise TableNotEmbeddingEligible(
            f"table is not embedding eligible: status={validation.status}; reasons={reasons}"
        )
