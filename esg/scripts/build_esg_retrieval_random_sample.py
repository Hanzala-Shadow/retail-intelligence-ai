#!/usr/bin/env python3
"""Build a deterministic random sample of retrieval-eligible ESG chunks."""

from __future__ import annotations

import argparse
import csv
import random
from pathlib import Path


def resolve(path_text: str) -> Path:
    path = Path(path_text)
    return path if path.is_absolute() else Path.cwd() / path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chunks-index", type=Path, required=True)
    parser.add_argument("--size", type=int, default=40)
    parser.add_argument("--seed", type=int, default=20260802)
    parser.add_argument("--csv-out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    args = parser.parse_args()

    with args.chunks_index.open(encoding="utf-8-sig", newline="") as handle:
        eligible = [
            row
            for row in csv.DictReader(handle)
            if row.get("include_in_esg_index") == "true"
        ]
    if args.size < 1 or args.size > len(eligible):
        raise ValueError("sample size must be between 1 and the eligible chunk count")
    selected = random.Random(args.seed).sample(eligible, args.size)
    rows = []
    lines = [f"# Random retrieval sample (seed {args.seed})", ""]
    for sample_id, row in enumerate(selected, start=1):
        text = resolve(row["chunk_file"]).read_text(encoding="utf-8")
        rows.append({"sample_id": sample_id, **row})
        lines.extend(
            [
                f"## {sample_id}. {row['ticker']} — {row['chunk_id']}",
                "",
                f"Title: {row.get('physical_section_title', '')}",
                f"Subsection: {row.get('subsection_context', '')}",
                f"Table context: {row.get('table_context', '')}",
                "",
                "```text",
                text,
                "```",
                "",
            ]
        )

    args.csv_out.parent.mkdir(parents=True, exist_ok=True)
    with args.csv_out.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    args.markdown_out.write_text("\n".join(lines), encoding="utf-8")
    print(f"eligible={len(eligible)} sampled={len(rows)} seed={args.seed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
