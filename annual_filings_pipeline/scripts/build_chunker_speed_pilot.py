#!/usr/bin/env python3
"""Build a deterministic, stress-weighted chunker speed pilot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sections-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--sample-size", type=int, default=100)
    args = parser.parse_args()

    if args.output_root.exists():
        raise FileExistsError(args.output_root)
    rows = [
        json.loads(line)
        for line in (args.sections_root / "sections.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    count = min(max(1, args.sample_size), len(rows))
    largest_count = max(1, count // 4)

    def section_path(row: dict[str, object]) -> Path:
        path = Path(str(row["output_file"]))
        if path.is_file():
            return path
        return args.sections_root / "10k" / path.name

    largest = sorted(
        range(len(rows)),
        key=lambda index: section_path(rows[index]).stat().st_size,
        reverse=True,
    )[:largest_count]
    selected = set(largest)
    remaining = count - len(selected)
    if remaining:
        denominator = max(1, remaining - 1)
        for position in range(remaining):
            index = round(position * (len(rows) - 1) / denominator)
            selected.add(index)
        for index in range(len(rows)):
            if len(selected) >= count:
                break
            selected.add(index)

    ordered = [rows[index] for index in sorted(selected)[:count]]
    args.output_root.mkdir(parents=True)
    section_files = args.output_root / "10k"
    section_files.mkdir()
    for row in ordered:
        source = section_path(row).resolve()
        target = section_files / source.name
        target.symlink_to(source)
    with (args.output_root / "sections.jsonl").open(
        "w", encoding="utf-8"
    ) as handle:
        for row in ordered:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    summary = {
        "source_sections": len(rows),
        "pilot_sections": len(ordered),
        "largest_sections": largest_count,
        "selection": "25_percent_largest_plus_evenly_spaced",
    }
    (args.output_root / "pilot_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
