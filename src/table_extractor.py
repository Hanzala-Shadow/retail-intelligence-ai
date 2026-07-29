from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

import config

TABLE_FILENAME_RE = re.compile(r"^(?P<doc_id>.+)__(?P<table_id>table_\d+)\.csv$")


TINY_ROWS = 2   
TINY_COLS = 2
RAGGED_TOLERANCE = 0  


def read_table_csv(path: Path) -> tuple[list[list[str]], str]:
    """Read a table CSV and return (rows, extraction_status)."""
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = [row for row in csv.reader(f)]
    except Exception:
        return [], "error"

    if not rows:
        return [], "empty"

    return rows, "ok"


def is_ragged(rows: list[list[str]]) -> bool:
    lengths = {len(r) for r in rows}
    return (max(lengths) - min(lengths)) > RAGGED_TOLERANCE


def evaluate_table(path: Path) -> dict:
    rows, status = read_table_csv(path)

    n_rows = len(rows)
    n_cols = max((len(r) for r in rows), default=0)

    if status == "ok":
        if n_rows == 0 or n_cols == 0:
            status = "empty"
        elif is_ragged(rows):
            status = "ragged"

    needs_manual_review = (
        status != "ok"
        or (n_rows <= TINY_ROWS and n_cols < TINY_COLS)
    )

    return {
        "n_rows": n_rows,
        "n_cols": n_cols,
        "extraction_status": status,
        "needs_manual_review": needs_manual_review,
    }


def discover_table_csvs(dirs: list[Path]) -> list[Path]:
    found = []
    for d in dirs:
        if not d.exists():
            print(f"Warning: table directory not found, skipping: {d}")
            continue
        found.extend(sorted(d.rglob("*.csv")))
    return found


def build_index(dirs: list[Path]) -> list[dict]:
    index = []
    unmatched = 0

    for csv_path in discover_table_csvs(dirs):
        m = TABLE_FILENAME_RE.match(csv_path.name)
        if not m:
            unmatched += 1
            doc_id = csv_path.stem
            table_id = "unknown"
        else:
            doc_id = m.group("doc_id")
            table_id = m.group("table_id")

        stats = evaluate_table(csv_path)

        index.append({
            "doc_id": doc_id,
            "table_id": table_id,
            "rows": stats["n_rows"],
            "cols": stats["n_cols"],
            "extraction_status": stats["extraction_status"],
            "needs_manual_review": stats["needs_manual_review"],
            "source_csv": str(csv_path),
        })

    if unmatched:
        print(f"Warning: {unmatched} CSV file(s) didn't match the expected "
              f"'{{doc_id}}__table_N.csv' naming pattern; doc_id/table_id may be unreliable for those.")

    def sort_key(row):
        m = re.search(r"(\d+)$", row["table_id"])
        table_num = int(m.group(1)) if m else 0
        return (row["doc_id"], table_num)

    index.sort(key=sort_key)
    return index


def write_index(index: list[dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["doc_id", "table_id", "rows", "cols", "extraction_status", "needs_manual_review", "source_csv"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(index)


def main():
    ap = argparse.ArgumentParser(
        description="Build a consolidated tables_index.csv from the table CSVs "
                     "produced by html_parser.py and pdf_parser.py."
    )
    ap.add_argument(
        "--tables-dir",
        dest="tables_dirs",
        action="append",
        default=None,
        help="Directory containing table CSVs. Repeatable. "
             "Default: data/tables/html_table and data/tables/pdf_table",
    )
    ap.add_argument("--out", default=str(config.TABLES_INDEX_CSV))
    args = ap.parse_args()

    dirs = (
        [Path(d) for d in args.tables_dirs]
        if args.tables_dirs
        else [config.HTML_TABLE_DIR, config.PDF_TABLE_DIR]
    )

    print(f"Scanning: {', '.join(str(d) for d in dirs)}")

    index = build_index(dirs)

    if not index:
        print("No table CSVs found. Nothing to index.")
        sys.exit(0)

    out_path = Path(args.out)
    write_index(index, out_path)

    n_total = len(index)
    n_flagged = sum(1 for r in index if r["needs_manual_review"])
    status_counts: dict[str, int] = {}
    for r in index:
        status_counts[r["extraction_status"]] = status_counts.get(r["extraction_status"], 0) + 1

    print(f"\nIndexed {n_total} tables across {len(set(r['doc_id'] for r in index))} document(s).")
    print("Status breakdown: " + ", ".join(f"{k}={v}" for k, v in sorted(status_counts.items())))
    print(f"Flagged for manual review: {n_flagged} ({n_flagged / n_total:.1%})")
    print(f"\nWrote index to: {out_path.resolve()}")


if __name__ == "__main__":
    main()