from __future__ import annotations

import argparse
import csv
from pathlib import Path


DEFAULT_INDEX = Path("data/00_reference/esg_parse_index.csv")


def display_path(path: str | Path) -> str:
    path = Path(path)
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def read_rows(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), list(reader)


def write_rows(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows([{field: row.get(field, "") for field in fieldnames} for row in rows])


def run(index_path: str | Path = DEFAULT_INDEX) -> dict[str, int]:
    index_path = Path(index_path)
    fieldnames, rows = read_rows(index_path)
    if "page_map_file" not in fieldnames:
        insert_at = fieldnames.index("parsed_text_file") + 1 if "parsed_text_file" in fieldnames else len(fieldnames)
        fieldnames.insert(insert_at, "page_map_file")

    updated = 0
    missing = 0
    for row in rows:
        parsed_text_file = row.get("parsed_text_file") or ""
        if not parsed_text_file:
            row["page_map_file"] = ""
            continue
        text_path = Path(parsed_text_file)
        page_map_path = text_path.with_suffix(".pages.csv")
        if page_map_path.exists():
            new_value = display_path(page_map_path)
            if row.get("page_map_file") != new_value:
                updated += 1
            row["page_map_file"] = new_value
        else:
            missing += 1
            row["page_map_file"] = ""

    write_rows(index_path, fieldnames, rows)
    return {"rows": len(rows), "updated": updated, "missing_page_maps": missing}


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill ESG parse index page_map_file values.")
    parser.add_argument("--index", default=str(DEFAULT_INDEX))
    args = parser.parse_args()
    stats = run(args.index)
    print(
        "Backfilled page maps: "
        f"rows={stats['rows']} updated={stats['updated']} missing_page_maps={stats['missing_page_maps']}"
    )


if __name__ == "__main__":
    main()
