"""Audit PDF table candidates with cell-level word ownership.

The default run is intentionally limited to the development content split of
the AI gold set.  It never opens a holdout page.  The output is a new report
directory containing JSON evidence, a CSV summary, and a short Markdown
report.  It does not change parser output, indexes, chunks, or embeddings.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
import sys
from typing import Any

import pdfplumber


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "esg" / "src"))
from esg_table_structure import validate_pdfplumber_table  # noqa: E402


DEFAULT_GOLD = REPO_ROOT / "data" / "00_reference" / "esg_ai_gold_v1.jsonl"
DEFAULT_PARSE_INDEX = (
    REPO_ROOT
    / "outputs"
    / "esg_ai_gold_parser_20260731"
    / "parser_output"
    / "esg_parse_index.csv"
)
DEFAULT_OUT = REPO_ROOT / "reports" / "esg_table_structure_audit_2026-08-01"


def read_development_content_gold(path: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            item = json.loads(line)
            if item.get("split") != "development":
                continue
            if item.get("reference_use") != "content":
                continue
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


def audit_item(item: dict[str, Any], parse_rows: dict[str, dict[str, str]]) -> dict[str, Any]:
    parse_row = parse_rows.get(item["pdf_file"])
    if parse_row is None:
        raise FileNotFoundError(f"no parse-index row for {item['pdf_file']}")
    source_pdf = resolve_source(parse_row["source_pdf"])
    table_rows: list[dict[str, Any]] = []
    with pdfplumber.open(source_pdf) as pdf:
        page_number = int(item["page"])
        page = pdf.pages[page_number - 1]
        words = page.extract_words(
            use_text_flow=False,
            keep_blank_chars=False,
            extra_attrs=["size", "upright"],
        ) or []
        for table_index, table in enumerate(page.find_tables() or []):
            validation = validate_pdfplumber_table(
                table,
                words,
                page_bbox=(0, 0, page.width, page.height),
                source="pdfplumber_find_tables",
                infer_headers=True,
            )
            table_rows.append(
                {
                    "item_id": item["item_id"],
                    "ticker": item["ticker"],
                    "pdf_file": item["pdf_file"],
                    "page": page_number,
                    "table_index": table_index,
                    "page_type": item.get("page_type", ""),
                    "canonical_order": item.get("canonical_order", ""),
                    "reference_chars": len(item.get("reference_markdown", "")),
                    "source_pdf": str(source_pdf),
                    **validation.as_dict(),
                }
            )
    return {
        "item_id": item["item_id"],
        "ticker": item["ticker"],
        "pdf_file": item["pdf_file"],
        "page": int(item["page"]),
        "page_type": item.get("page_type", ""),
        "canonical_order": item.get("canonical_order", ""),
        "table_count": len(table_rows),
        "tables": table_rows,
    }


def write_outputs(out_dir: Path, results: list[dict[str, Any]]) -> None:
    out_dir.mkdir(parents=True, exist_ok=False)
    table_rows = [table for result in results for table in result["tables"]]
    (out_dir / "table_audit.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    fields = [
        "item_id",
        "ticker",
        "pdf_file",
        "page",
        "table_index",
        "page_type",
        "canonical_order",
        "reference_chars",
        "source_pdf",
        "status",
        "source",
        "table_bbox",
        "row_count",
        "column_count",
        "cell_count",
        "nonempty_cell_count",
        "source_word_count",
        "assigned_word_count",
        "word_recall",
        "extracted_matches",
        "geometry_verified",
        "embedding_eligible",
        "header_status",
        "header_rows",
        "unassigned_word_ids",
        "ambiguous_word_ids",
        "ignored_word_ids",
        "reason_codes",
    ]
    with (out_dir / "table_audit.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in table_rows:
            writer.writerow(
                {
                    **row,
                    "table_bbox": json.dumps(row.get("table_bbox"), ensure_ascii=False),
                    "unassigned_word_ids": json.dumps(row.get("unassigned_word_ids", [])),
                    "ambiguous_word_ids": json.dumps(row.get("ambiguous_word_ids", [])),
                    "ignored_word_ids": json.dumps(row.get("ignored_word_ids", [])),
                    "header_rows": json.dumps(row.get("header_rows", [])),
                    "reason_codes": "|".join(row.get("reason_codes", [])),
                }
            )

    status_counts = Counter(row["status"] for row in table_rows)
    reason_counts = Counter(
        reason
        for row in table_rows
        for reason in row.get("reason_codes", [])
    )
    geometry_verified_count = sum(
        bool(table["geometry_verified"]) for table in table_rows
    )
    embedding_eligible_count = sum(
        bool(table["embedding_eligible"]) for table in table_rows
    )
    header_review_count = sum(
        bool(table["geometry_verified"]) and not bool(table["embedding_eligible"])
        for table in table_rows
    )
    eligible_pages = sum(
        any(table["embedding_eligible"] for table in result["tables"])
        for result in results
    )
    report = [
        "# ESG table structure audit",
        "",
        "This is a development-split audit only. Holdout pages were not opened, scored, or rendered.",
        "The run is read-only for parser outputs, indexes, chunks, and embeddings.",
        "",
        f"- Development content pages: {len(results)}",
        f"- Raw table candidates: {len(table_rows)}",
        f"- Candidates with verified geometry: {geometry_verified_count}",
        f"- Candidates held for header approval: {header_review_count}",
        f"- Embedding-eligible candidates: {embedding_eligible_count}",
        f"- Pages with an embedding-eligible table: {eligible_pages}",
        "- Status counts: " + ", ".join(f"{key}={value}" for key, value in sorted(status_counts.items())),
        "",
        "## Most common hold reasons",
        "",
    ]
    if reason_counts:
        report.extend(f"- `{key}`: {value}" for key, value in reason_counts.most_common())
    else:
        report.append("- None")
    report.extend(
        [
            "",
            "## Meaning of the statuses",
            "",
            "- `verified`: extracted cell text agrees with source word boxes.",
            "- `reconstructable`: extractor text disagrees, but every source word has one geometry owner; output must be rebuilt from boxes.",
            "- `review`: a word has no owner, more than one possible owner, bad geometry, or no explicitly approved header.",
            "",
            "A table is embedding eligible only when its source geometry is verified and its header rows are explicitly declared. Inferred headers are review suggestions only. A table with token recall alone is not eligible.",
        ]
    )
    (out_dir / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")


def run(gold_path: Path, parse_index_path: Path, out_dir: Path) -> int:
    if out_dir.exists():
        raise FileExistsError(f"refusing to overwrite existing audit output: {out_dir}")
    items = read_development_content_gold(gold_path)
    parse_rows = read_parse_index(parse_index_path)
    results = [audit_item(item, parse_rows) for item in items]
    write_outputs(out_dir, results)
    print(f"development_content_pages={len(results)}")
    print("holdout_pages_read=0")
    print(f"output={out_dir}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--parse-index", type=Path, default=DEFAULT_PARSE_INDEX)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    return run(args.gold, args.parse_index, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
