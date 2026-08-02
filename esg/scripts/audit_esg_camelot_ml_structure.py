"""Compare Camelot ML table structure with pdfplumber and PyMuPDF.

This is a development-only experiment.  It reads only content pages from the
development split of the frozen AI-gold manifest.  It never changes parser
outputs, chunks, indexes, vectors, or production defaults.

The acceptance gate is deliberately strict:

* Camelot, pdfplumber, and PyMuPDF must propose the same table geometry;
* row and column grids must agree, including cell boxes;
* every source word must have exactly one cell owner in both word inventories;
* the ownership map must agree across the three proposals; and
* an explicit header approval is still required before embedding records are
  emitted.

Camelot 2.x stores cell boxes in a bottom-left coordinate system.  The
project validator uses pdfplumber/PyMuPDF top-left coordinates, so this file
converts Camelot boxes before validation.
"""

from __future__ import annotations

import argparse
import csv
import json
import platform
import sys
import warnings
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

try:
    import camelot
except ModuleNotFoundError:  # Optional dependency for this audit only.
    camelot = None
import fitz  # PyMuPDF
import pdfplumber


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "esg" / "src"))
from esg_table_structure import (  # noqa: E402
    CellBox,
    TableValidation,
    validate_table,
)


DEFAULT_GOLD = REPO_ROOT / "data" / "00_reference" / "esg_ai_gold_v1.jsonl"
DEFAULT_PARSE_INDEX = (
    REPO_ROOT
    / "outputs"
    / "esg_ai_gold_parser_20260731"
    / "parser_output"
    / "esg_parse_index.csv"
)
DEFAULT_OUT = REPO_ROOT / "reports" / "esg_camelot_ml_structure_audit_2026-08-01_v2"

# These thresholds are frozen before the development run.  They are not
# tuned from holdout pages or from the output of this experiment.
CAMELOT_FLAVOR = "ml"
MATCH_BBOX_IOU_MIN = 0.80
CELL_BBOX_IOU_MIN = 0.75
EDGE_TOLERANCE = 12.0
EDGE_CLUSTER_TOLERANCE = 1.5


def read_development_content_gold(path: Path) -> list[dict[str, Any]]:
    """Read only the development/content rows needed to select pages."""

    items: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            item = json.loads(line)
            if item.get("split") == "development" and item.get("reference_use") == "content":
                items.append(item)
    if any(item.get("split") != "development" for item in items):
        raise AssertionError("holdout item entered the development audit set")
    return items


def read_parse_index(path: Path) -> dict[str, dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return {row["pdf_file"]: row for row in csv.DictReader(handle)}


def resolve_source(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPO_ROOT / path


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bbox(value: Any) -> tuple[float, float, float, float] | None:
    try:
        values = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None
    if len(values) != 4:
        return None
    x0, y0, x1, y1 = values
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    return x0, y0, x1, y1


def pdfplumber_words(page) -> list[dict[str, Any]]:
    words = page.extract_words(
        use_text_flow=False,
        keep_blank_chars=False,
        extra_attrs=["size", "upright"],
    ) or []
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(words):
        word = dict(raw)
        if not str(word.get("text") or "").strip():
            continue
        word["idx"] = index
        result.append(word)
    return result


def pymupdf_words(page) -> list[dict[str, Any]]:
    """Use the same word schema as the project PyMuPDF parser."""

    result: list[dict[str, Any]] = []
    for index, item in enumerate(page.get_text("words", sort=False) or []):
        if len(item) < 5:
            continue
        x0, top, x1, bottom, text = item[:5]
        text = str(text or "").strip()
        if not text:
            continue
        width = max(0.0, float(x1) - float(x0))
        height = max(0.0, float(bottom) - float(top))
        vertical_edge_text = (
            float(x0) >= float(page.rect.width) * 0.94
            and height >= 15.0
            and height > width * 1.25
        )
        result.append(
            {
                "idx": index,
                "x0": float(x0),
                "top": float(top),
                "x1": float(x1),
                "bottom": float(bottom),
                "text": text,
                "upright": not vertical_edge_text,
                "size": height,
            }
        )
    return result


def _camelot_cell_box(cell: Any, page_height: float) -> CellBox | None:
    if cell is None:
        return None
    # Camelot uses x1/y1 at the lower-left and x2/y2 at the upper-right.
    x0 = _number(getattr(cell, "x1", None))
    bottom_y = _number(getattr(cell, "y1", None))
    x1 = _number(getattr(cell, "x2", None))
    top_y = _number(getattr(cell, "y2", None))
    converted = _bbox((x0, page_height - top_y, x1, page_height - bottom_y))
    if converted is None:
        return None
    return CellBox(0, 0, converted)


def camelot_proposal(table: Any, page_height: float, table_index: int) -> dict[str, Any]:
    raw_rows = list(getattr(table, "cells", []) or [])
    cells: list[list[CellBox | None]] = []
    for row_index, raw_row in enumerate(raw_rows):
        row: list[CellBox | None] = []
        for column_index, raw_cell in enumerate(raw_row or []):
            cell = _camelot_cell_box(raw_cell, page_height)
            if cell is not None:
                cell = CellBox(
                    row_index,
                    column_index,
                    cell.bbox,
                    row_span=1,
                    column_span=1,
                )
            row.append(cell)
        cells.append(row)

    table_bbox = getattr(table, "_bbox", None)
    if table_bbox is not None:
        x0, bottom_y, x1, top_y = map(float, table_bbox)
        table_bbox = (x0, page_height - top_y, x1, page_height - bottom_y)
    else:
        boxes = [cell.bbox for row in cells for cell in row if cell is not None]
        table_bbox = _union_bbox(boxes)

    try:
        extracted_rows = table.extract() or []
    except Exception:
        extracted_rows = []
    return {
        "source": "camelot_ml_table_transformer",
        "table_index": table_index,
        "bbox": _bbox(table_bbox),
        "cells": cells,
        "extracted_rows": extracted_rows,
        "model_report": getattr(table, "parsing_report", None),
        "confidence": getattr(table, "confidence", None),
        "flavor": getattr(table, "flavor", CAMELOT_FLAVOR),
    }


def native_proposal(table: Any, source: str, table_index: int) -> dict[str, Any]:
    cells: list[list[CellBox | None]] = []
    for row_index, raw_row in enumerate(getattr(table, "rows", []) or []):
        row: list[CellBox | None] = []
        for column_index, raw_cell in enumerate(getattr(raw_row, "cells", []) or []):
            raw_bbox = getattr(raw_cell, "bbox", raw_cell)
            cell_bbox = _bbox(raw_bbox) if raw_bbox is not None else None
            row.append(
                CellBox(row_index, column_index, cell_bbox)
                if cell_bbox is not None
                else None
            )
        cells.append(row)
    try:
        extracted_rows = table.extract() or []
    except Exception:
        extracted_rows = []
    return {
        "source": source,
        "table_index": table_index,
        "bbox": _bbox(getattr(table, "bbox", None)),
        "cells": cells,
        "extracted_rows": extracted_rows,
        "model_report": None,
        "confidence": None,
        "flavor": None,
    }


def _union_bbox(boxes: Iterable[tuple[float, float, float, float]]) -> tuple[float, float, float, float] | None:
    values = list(boxes)
    if not values:
        return None
    return (
        min(value[0] for value in values),
        min(value[1] for value in values),
        max(value[2] for value in values),
        max(value[3] for value in values),
    )


def proposal_shape(proposal: dict[str, Any]) -> tuple[int, int, int]:
    cells = proposal.get("cells", [])
    row_count = len(cells)
    column_count = max((len(row) for row in cells), default=0)
    cell_count = sum(cell is not None for row in cells for cell in row)
    return row_count, column_count, cell_count


def _cell_bbox(proposal: dict[str, Any], row_index: int, column_index: int):
    cells = proposal.get("cells", [])
    if row_index >= len(cells) or column_index >= len(cells[row_index]):
        return None
    cell = cells[row_index][column_index]
    return None if cell is None else cell.bbox


def bbox_iou(left: tuple[float, float, float, float] | None, right: tuple[float, float, float, float] | None) -> float:
    if left is None or right is None:
        return 0.0
    ix0 = max(left[0], right[0])
    iy0 = max(left[1], right[1])
    ix1 = min(left[2], right[2])
    iy1 = min(left[3], right[3])
    intersection = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    left_area = max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
    right_area = max(0.0, right[2] - right[0]) * max(0.0, right[3] - right[1])
    union = left_area + right_area - intersection
    return intersection / union if union else 0.0


def _cluster_edges(values: Iterable[float]) -> list[float]:
    edges: list[float] = []
    for value in sorted(float(item) for item in values):
        if not edges or abs(value - edges[-1]) > EDGE_CLUSTER_TOLERANCE:
            edges.append(value)
        else:
            edges[-1] = (edges[-1] + value) / 2.0
    return edges


def proposal_edges(proposal: dict[str, Any], axis: str) -> list[float]:
    values: list[float] = []
    for row in proposal.get("cells", []):
        for cell in row:
            if cell is None:
                continue
            if axis == "x":
                values.extend((cell.bbox[0], cell.bbox[2]))
            else:
                values.extend((cell.bbox[1], cell.bbox[3]))
    return _cluster_edges(values)


def edge_agreement(left: dict[str, Any], right: dict[str, Any], axis: str) -> dict[str, Any]:
    left_edges = proposal_edges(left, axis)
    right_edges = proposal_edges(right, axis)
    if not left_edges or not right_edges:
        return {
            "left_count": len(left_edges),
            "right_count": len(right_edges),
            "max_abs_delta": None,
            "pass": False,
        }
    deltas = []
    for left_edge, right_edge in zip(left_edges, right_edges):
        deltas.append(abs(left_edge - right_edge))
    max_delta = max(deltas) if len(left_edges) == len(right_edges) else None
    return {
        "left_count": len(left_edges),
        "right_count": len(right_edges),
        "max_abs_delta": max_delta,
        "pass": len(left_edges) == len(right_edges) and max_delta is not None and max_delta <= EDGE_TOLERANCE,
    }


def geometry_pair(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    left_shape = proposal_shape(left)
    right_shape = proposal_shape(right)
    pattern_equal = len(left["cells"]) == len(right["cells"]) and all(
        len(left["cells"][row_index]) == len(right["cells"][row_index])
        and all(
            (left["cells"][row_index][column_index] is None)
            == (right["cells"][row_index][column_index] is None)
            for column_index in range(len(left["cells"][row_index]))
        )
        for row_index in range(len(left["cells"]))
    )
    cell_ious: list[float] = []
    if pattern_equal:
        for row_index, row in enumerate(left["cells"]):
            for column_index, cell in enumerate(row):
                if cell is None:
                    continue
                cell_ious.append(
                    bbox_iou(cell.bbox, _cell_bbox(right, row_index, column_index))
                )
    min_cell_iou = min(cell_ious) if cell_ious else 0.0
    row_edges = edge_agreement(left, right, "y")
    column_edges = edge_agreement(left, right, "x")
    return {
        "bbox_iou": round(bbox_iou(left.get("bbox"), right.get("bbox")), 6),
        "left_shape": list(left_shape),
        "right_shape": list(right_shape),
        "shape_exact": left_shape[:2] == right_shape[:2],
        "cell_pattern_equal": pattern_equal,
        "cell_iou_min": round(min_cell_iou, 6),
        "cell_iou_mean": round(sum(cell_ious) / len(cell_ious), 6) if cell_ious else 0.0,
        "row_edges": row_edges,
        "column_edges": column_edges,
        "pass": (
            bbox_iou(left.get("bbox"), right.get("bbox")) >= MATCH_BBOX_IOU_MIN
            and left_shape[:2] == right_shape[:2]
            and pattern_equal
            and min_cell_iou >= CELL_BBOX_IOU_MIN
            and row_edges["pass"]
            and column_edges["pass"]
        ),
    }


def _validation_for(
    proposal: dict[str, Any],
    words: list[dict[str, Any]],
    word_source: str,
    page_width: float,
    page_height: float,
    header_rows: list[int] | None = None,
) -> TableValidation:
    return validate_table(
        words=words,
        cells=proposal["cells"],
        extracted_rows=proposal.get("extracted_rows") or [],
        table_bbox=proposal.get("bbox"),
        page_bbox=(0.0, 0.0, page_width, page_height),
        source=f"{proposal['source']}__{word_source}",
        header_rows=header_rows,
        infer_headers=header_rows is None,
    )


def ownership_map(validation: TableValidation) -> dict[Any, tuple[int, int]]:
    result: dict[Any, tuple[int, int]] = {}
    for cell in validation.cells:
        for word_id in cell.word_ids:
            result[word_id] = (cell.row_index, cell.column_index)
    return result


def exact_ownership(validation: TableValidation) -> bool:
    return bool(validation.source_word_count) and (
        validation.geometry_verified
        and validation.assigned_word_count == validation.source_word_count
        and not validation.unassigned_word_ids
        and not validation.ambiguous_word_ids
        and not validation.ignored_word_ids
        and len(ownership_map(validation)) == validation.assigned_word_count
    )


def ownership_agreement(left: TableValidation, right: TableValidation) -> dict[str, Any]:
    left_map = ownership_map(left)
    right_map = ownership_map(right)
    same_ids = set(left_map) == set(right_map)
    same_cells = same_ids and all(left_map[key] == right_map[key] for key in left_map)
    return {
        "left_word_count": len(left_map),
        "right_word_count": len(right_map),
        "same_word_ids": same_ids,
        "same_cell_assignments": same_cells,
        "pass": exact_ownership(left) and exact_ownership(right) and same_cells,
    }


def _validation_public(validation: TableValidation) -> dict[str, Any]:
    return validation.as_dict()


def _proposal_public(proposal: dict[str, Any] | None) -> dict[str, Any] | None:
    if proposal is None:
        return None
    rows, columns, cells = proposal_shape(proposal)
    return {
        "source": proposal["source"],
        "table_index": proposal["table_index"],
        "bbox": list(proposal["bbox"]) if proposal.get("bbox") else None,
        "row_count": rows,
        "column_count": columns,
        "cell_count": cells,
        "flavor": proposal.get("flavor"),
        "confidence": proposal.get("confidence"),
        "model_report": proposal.get("model_report"),
    }


def _best_match(proposal: dict[str, Any], candidates: list[dict[str, Any]], used: set[int]) -> tuple[int | None, float]:
    options = [
        (index, bbox_iou(proposal.get("bbox"), candidate.get("bbox")))
        for index, candidate in enumerate(candidates)
        if index not in used
    ]
    if not options:
        return None, 0.0
    return max(options, key=lambda item: item[1])


def load_header_approvals(path: Path | None) -> dict[str, list[int]]:
    if path is None:
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("header approvals must be a JSON object")
    result: dict[str, list[int]] = {}
    for key, value in data.items():
        if not isinstance(value, list):
            raise ValueError(f"header approval for {key!r} must be a list")
        result[str(key)] = sorted({int(row) for row in value})
    return result


def make_cluster(
    *,
    item: dict[str, Any],
    page_width: float,
    page_height: float,
    proposals: dict[str, dict[str, Any] | None],
    words_by_source: dict[str, list[dict[str, Any]]],
    header_approvals: dict[str, list[int]],
) -> dict[str, Any]:
    available = {key: value for key, value in proposals.items() if value is not None}
    pair_metrics: dict[str, Any] = {}
    for left_name, right_name in (("camelot", "pdfplumber"), ("camelot", "pymupdf"), ("pdfplumber", "pymupdf")):
        left = proposals.get(left_name)
        right = proposals.get(right_name)
        if left is not None and right is not None:
            pair_metrics[f"{left_name}_vs_{right_name}"] = geometry_pair(left, right)

    validations: dict[str, dict[str, dict[str, Any]]] = {}
    validation_objects: dict[str, dict[str, TableValidation]] = {}
    for proposal_name, proposal in available.items():
        validations[proposal_name] = {}
        validation_objects[proposal_name] = {}
        for word_source, words in words_by_source.items():
            validation = _validation_for(
                proposal,
                words,
                word_source,
                page_width,
                page_height,
            )
            validation_objects[proposal_name][word_source] = validation
            validations[proposal_name][word_source] = _validation_public(validation)

    ownership_pairs: dict[str, Any] = {}
    for word_source in words_by_source:
        for left_name, right_name in (("camelot", "pdfplumber"), ("camelot", "pymupdf"), ("pdfplumber", "pymupdf")):
            left = validation_objects.get(left_name, {}).get(word_source)
            right = validation_objects.get(right_name, {}).get(word_source)
            if left is not None and right is not None:
                ownership_pairs[f"{word_source}:{left_name}_vs_{right_name}"] = ownership_agreement(left, right)

    geometry_pass = (
        set(available) == {"camelot", "pdfplumber", "pymupdf"}
        and len(pair_metrics) == 3
        and all(metric["pass"] for metric in pair_metrics.values())
    )
    ownership_pass = (
        set(available) == {"camelot", "pdfplumber", "pymupdf"}
        and all(
            exact_ownership(validation_objects[name][word_source])
            for name in available
            for word_source in words_by_source
        )
        and len(ownership_pairs) == len(words_by_source) * 3
        and all(metric["pass"] for metric in ownership_pairs.values())
    )
    structure_pass = geometry_pass and ownership_pass

    camelot_index = proposals.get("camelot", {}).get("table_index") if proposals.get("camelot") else None
    approval_key = f"{item['item_id']}|{camelot_index}" if camelot_index is not None else ""
    approved_headers = header_approvals.get(approval_key)
    embedding_pass = False
    embedding_records: list[dict[str, Any]] = []
    if structure_pass and approved_headers is not None:
        canonical = proposals["camelot"]
        assert canonical is not None
        canonical_validation = _validation_for(
            canonical,
            words_by_source["pdfplumber"],
            "pdfplumber",
            page_width,
            page_height,
            header_rows=approved_headers,
        )
        embedding_pass = canonical_validation.embedding_eligible
        if embedding_pass:
            embedding_records = canonical_validation.to_embedding_records(
                document_id=item["pdf_file"],
                page_number=int(item["page"]),
                table_id=approval_key,
            )
            validations["camelot"]["pdfplumber_with_approved_headers"] = _validation_public(canonical_validation)

    reasons: list[str] = []
    if set(available) != {"camelot", "pdfplumber", "pymupdf"}:
        reasons.append("not_all_three_proposals_present")
    if not geometry_pass:
        reasons.append("independent_geometry_disagreement")
    if not ownership_pass:
        reasons.append("exact_word_ownership_failed")
    if structure_pass and approved_headers is None:
        reasons.append("explicit_header_approval_missing")
    if structure_pass and approved_headers is not None and not embedding_pass:
        reasons.append("approved_header_validation_failed")

    return {
        "item_id": item["item_id"],
        "ticker": item["ticker"],
        "pdf_file": item["pdf_file"],
        "page": int(item["page"]),
        "page_type": item.get("page_type", ""),
        "canonical_order": item.get("canonical_order", ""),
        "proposals": {key: _proposal_public(value) for key, value in proposals.items()},
        "pair_geometry": pair_metrics,
        "ownership": ownership_pairs,
        "validations": validations,
        "geometry_pass": geometry_pass,
        "ownership_pass": ownership_pass,
        "structure_pass": structure_pass,
        "embedding_pass": embedding_pass,
        "header_approval_key": approval_key,
        "approved_header_rows": approved_headers,
        "reason_codes": reasons,
        "embedding_records": embedding_records,
    }


def camelot_tables(source_pdf: Path, page_number: int) -> list[Any]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return list(
            camelot.read_pdf(
                str(source_pdf),
                pages=str(page_number),
                flavor=CAMELOT_FLAVOR,
                suppress_stdout=True,
            )
        )


def audit_item(
    item: dict[str, Any],
    parse_rows: dict[str, dict[str, str]],
    header_approvals: dict[str, list[int]],
) -> dict[str, Any]:
    parse_row = parse_rows.get(item["pdf_file"])
    if parse_row is None:
        raise FileNotFoundError(f"no parse-index row for {item['pdf_file']}")
    source_pdf = resolve_source(parse_row["source_pdf"])
    page_number = int(item["page"])
    with pdfplumber.open(source_pdf) as plumber_doc, fitz.open(source_pdf) as fitz_doc:
        plumber_page = plumber_doc.pages[page_number - 1]
        fitz_page = fitz_doc[page_number - 1]
        page_width = float(plumber_page.width)
        page_height = float(plumber_page.height)
        words_by_source = {
            "pdfplumber": pdfplumber_words(plumber_page),
            "pymupdf": pymupdf_words(fitz_page),
        }
        plumber_proposals = [
            native_proposal(table, "pdfplumber", index)
            for index, table in enumerate(plumber_page.find_tables() or [])
        ]
        try:
            fitz_finder = fitz_page.find_tables()
            fitz_tables = list(getattr(fitz_finder, "tables", []) or [])
        except Exception:
            fitz_tables = []
        pymupdf_proposals = [
            native_proposal(table, "pymupdf", index)
            for index, table in enumerate(fitz_tables)
        ]
    camelot_error = None
    try:
        raw_camelot = camelot_tables(source_pdf, page_number)
        camelot_proposals = [
            camelot_proposal(table, page_height, index)
            for index, table in enumerate(raw_camelot)
        ]
    except Exception as exc:
        camelot_proposals = []
        camelot_error = f"{type(exc).__name__}: {exc}"

    clusters: list[dict[str, Any]] = []
    used_plumber: set[int] = set()
    used_pymupdf: set[int] = set()
    for camelot_candidate in camelot_proposals:
        plumber_index, _ = _best_match(camelot_candidate, plumber_proposals, used_plumber)
        pymupdf_index, _ = _best_match(camelot_candidate, pymupdf_proposals, used_pymupdf)
        if plumber_index is not None:
            used_plumber.add(plumber_index)
        if pymupdf_index is not None:
            used_pymupdf.add(pymupdf_index)
        clusters.append(
            make_cluster(
                item=item,
                page_width=page_width,
                page_height=page_height,
                proposals={
                    "camelot": camelot_candidate,
                    "pdfplumber": plumber_proposals[plumber_index] if plumber_index is not None else None,
                    "pymupdf": pymupdf_proposals[pymupdf_index] if pymupdf_index is not None else None,
                },
                words_by_source=words_by_source,
                header_approvals=header_approvals,
            )
        )
    for plumber_index, plumber_candidate in enumerate(plumber_proposals):
        if plumber_index in used_plumber:
            continue
        pymupdf_index, _ = _best_match(plumber_candidate, pymupdf_proposals, used_pymupdf)
        if pymupdf_index is not None:
            used_pymupdf.add(pymupdf_index)
        clusters.append(
            make_cluster(
                item=item,
                page_width=page_width,
                page_height=page_height,
                proposals={
                    "camelot": None,
                    "pdfplumber": plumber_candidate,
                    "pymupdf": pymupdf_proposals[pymupdf_index] if pymupdf_index is not None else None,
                },
                words_by_source=words_by_source,
                header_approvals=header_approvals,
            )
        )
    for pymupdf_index, pymupdf_candidate in enumerate(pymupdf_proposals):
        if pymupdf_index in used_pymupdf:
            continue
        clusters.append(
            make_cluster(
                item=item,
                page_width=page_width,
                page_height=page_height,
                proposals={"camelot": None, "pdfplumber": None, "pymupdf": pymupdf_candidate},
                words_by_source=words_by_source,
                header_approvals=header_approvals,
            )
        )

    return {
        "item_id": item["item_id"],
        "ticker": item["ticker"],
        "pdf_file": item["pdf_file"],
        "page": page_number,
        "page_type": item.get("page_type", ""),
        "source_pdf": str(source_pdf),
        "page_width": page_width,
        "page_height": page_height,
        "word_counts": {key: len(value) for key, value in words_by_source.items()},
        "proposal_counts": {
            "camelot": len(camelot_proposals),
            "pdfplumber": len(plumber_proposals),
            "pymupdf": len(pymupdf_proposals),
        },
        "camelot_error": camelot_error,
        "clusters": clusters,
    }


def flatten_clusters(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [cluster for result in results for cluster in result["clusters"]]


def write_outputs(
    out_dir: Path,
    results: list[dict[str, Any]],
    *,
    gold_path: Path,
    parse_index_path: Path,
    header_approvals: dict[str, list[int]],
) -> None:
    if out_dir.exists():
        raise FileExistsError(f"refusing to overwrite existing audit output: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=False)
    clusters = flatten_clusters(results)
    embedding_records = [
        record
        for cluster in clusters
        if cluster.get("embedding_pass")
        for record in cluster.get("embedding_records", [])
    ]

    metadata = {
        "experiment": "esg_camelot_ml_structure_audit",
        "version": "2026-08-01_v2",
        "camelot_version": getattr(camelot, "__version__", "unknown"),
        "camelot_flavor": CAMELOT_FLAVOR,
        "pdfplumber_version": getattr(pdfplumber, "__version__", "unknown"),
        "pymupdf_version": getattr(fitz, "VersionBind", "unknown"),
        "python": sys.version,
        "platform": platform.platform(),
        "thresholds": {
            "match_bbox_iou_min": MATCH_BBOX_IOU_MIN,
            "cell_bbox_iou_min": CELL_BBOX_IOU_MIN,
            "edge_tolerance_points": EDGE_TOLERANCE,
        },
        "gold_manifest": str(gold_path),
        "parse_index": str(parse_index_path),
        "header_approval_count": len(header_approvals),
        "development_content_pages": len(results),
        "holdout_pages_read": 0,
        "production_outputs_changed": False,
        "embeddings_built": False,
        "embedding_ready_record_count": len(embedding_records),
    }
    (out_dir / "run_metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (out_dir / "comparison.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    fields = [
        "item_id", "ticker", "pdf_file", "page", "camelot_index", "pdfplumber_index", "pymupdf_index",
        "geometry_pass", "ownership_pass", "structure_pass", "embedding_pass", "reason_codes",
        "camelot_bbox", "pdfplumber_bbox", "pymupdf_bbox", "camelot_shape", "pdfplumber_shape", "pymupdf_shape",
        "camelot_vs_pdfplumber_iou", "camelot_vs_pymupdf_iou", "pdfplumber_vs_pymupdf_iou",
    ]
    with (out_dir / "comparison.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for cluster in clusters:
            proposals = cluster["proposals"]
            pair_geometry = cluster["pair_geometry"]
            writer.writerow({
                "item_id": cluster["item_id"],
                "ticker": cluster["ticker"],
                "pdf_file": cluster["pdf_file"],
                "page": cluster["page"],
                "camelot_index": (proposals.get("camelot") or {}).get("table_index"),
                "pdfplumber_index": (proposals.get("pdfplumber") or {}).get("table_index"),
                "pymupdf_index": (proposals.get("pymupdf") or {}).get("table_index"),
                "geometry_pass": cluster["geometry_pass"],
                "ownership_pass": cluster["ownership_pass"],
                "structure_pass": cluster["structure_pass"],
                "embedding_pass": cluster["embedding_pass"],
                "reason_codes": "|".join(cluster["reason_codes"]),
                "camelot_bbox": json.dumps((proposals.get("camelot") or {}).get("bbox")),
                "pdfplumber_bbox": json.dumps((proposals.get("pdfplumber") or {}).get("bbox")),
                "pymupdf_bbox": json.dumps((proposals.get("pymupdf") or {}).get("bbox")),
                "camelot_shape": json.dumps((proposals.get("camelot") or {}).get("row_count")),
                "pdfplumber_shape": json.dumps((proposals.get("pdfplumber") or {}).get("row_count")),
                "pymupdf_shape": json.dumps((proposals.get("pymupdf") or {}).get("row_count")),
                "camelot_vs_pdfplumber_iou": pair_geometry.get("camelot_vs_pdfplumber", {}).get("bbox_iou"),
                "camelot_vs_pymupdf_iou": pair_geometry.get("camelot_vs_pymupdf", {}).get("bbox_iou"),
                "pdfplumber_vs_pymupdf_iou": pair_geometry.get("pdfplumber_vs_pymupdf", {}).get("bbox_iou"),
            })

    with (out_dir / "embedding_ready_records.jsonl").open("w", encoding="utf-8") as handle:
        for record in embedding_records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    (out_dir / "embedding_ready_schema.json").write_text(
        json.dumps({
            "description": "Source-backed records emitted only after all three geometry and ownership gates plus explicit header approval.",
            "vectors_built": False,
            "record_count": len(embedding_records),
        }, indent=2) + "\n",
        encoding="utf-8",
    )

    status_counts = Counter(
        "pass" if cluster["structure_pass"] else "held"
        for cluster in clusters
    )
    reason_counts = Counter(
        reason
        for cluster in clusters
        for reason in cluster["reason_codes"]
    )
    pages_with_camelot = sum(bool(result["proposal_counts"]["camelot"]) for result in results)
    pages_with_structure_pass = sum(
        any(cluster["structure_pass"] for cluster in result["clusters"])
        for result in results
    )
    report = [
        "# Camelot ML table structure audit",
        "",
        "Development-only. The run opened and scored only development/content pages from the frozen AI-gold manifest. Holdout pages were not opened, scored, or rendered.",
        "Production parser output, chunks, indexes, vectors, and defaults were not changed. No embeddings were built.",
        "",
        "## Method",
        "",
        f"- Camelot {getattr(camelot, '__version__', 'unknown')} flavor `{CAMELOT_FLAVOR}` (Table Transformer)",
        "- Independent proposals: Camelot cell grid, pdfplumber `find_tables`, and PyMuPDF `find_tables`",
        "- Camelot boxes converted from bottom-left to the project top-left coordinate system",
        f"- Frozen table-box IoU gate: {MATCH_BBOX_IOU_MIN:.2f}; cell-box IoU gate: {CELL_BBOX_IOU_MIN:.2f}; edge tolerance: {EDGE_TOLERANCE:.1f} points",
        "- Exact ownership gate: every active pdfplumber and PyMuPDF source word has one owner, with no gaps, ambiguity, rotation exclusion, or duplicate owner; all proposal maps must agree",
        "- Embedding gate: structure pass plus an explicit header-approval JSON entry",
        "",
        "## Results",
        "",
        f"- Development content pages: {len(results)}",
        f"- Pages where Camelot returned a table: {pages_with_camelot}",
        f"- Table clusters: {len(clusters)}",
        f"- Structure passes: {status_counts.get('pass', 0)}",
        f"- Held: {status_counts.get('held', 0)}",
        f"- Pages with a structure pass: {pages_with_structure_pass}",
        f"- Embedding-ready records: {len(embedding_records)}",
        "- Embeddings built: 0",
        "",
        "## Hold reasons",
        "",
    ]
    report.extend(f"- `{reason}`: {count}" for reason, count in reason_counts.most_common())
    report.extend([
        "",
        "## Artifacts",
        "",
        "- `comparison.csv`: one row per matched or unmatched table cluster",
        "- `comparison.json`: full geometry, ownership, and source-word evidence",
        "- `embedding_ready_records.jsonl`: empty unless every gate and explicit header approval pass",
        "- `run_metadata.json`: versions, thresholds, and safety flags",
    ])
    (out_dir / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")


def run(
    gold_path: Path,
    parse_index_path: Path,
    out_dir: Path,
    header_approvals_path: Path | None,
) -> int:
    items = read_development_content_gold(gold_path)
    parse_rows = read_parse_index(parse_index_path)
    header_approvals = load_header_approvals(header_approvals_path)
    results = [audit_item(item, parse_rows, header_approvals) for item in items]
    write_outputs(
        out_dir,
        results,
        gold_path=gold_path,
        parse_index_path=parse_index_path,
        header_approvals=header_approvals,
    )
    print(f"development_content_pages={len(results)}")
    print("holdout_pages_read=0")
    print("production_outputs_changed=0")
    print("embeddings_built=0")
    print(f"output={out_dir}")
    return 0


def main() -> int:
    if camelot is None:
        raise SystemExit(
            "Camelot is required for this development audit. Install camelot-py first."
        )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--parse-index", type=Path, default=DEFAULT_PARSE_INDEX)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--header-approvals",
        type=Path,
        default=None,
        help="Optional JSON object keyed by '<item_id>|<camelot_table_index>' with approved header row indexes.",
    )
    args = parser.parse_args()
    return run(args.gold, args.parse_index, args.out, args.header_approvals)


if __name__ == "__main__":
    raise SystemExit(main())
