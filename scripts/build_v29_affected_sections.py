#!/usr/bin/env python3
"""Build the minimal section corpus affected by chunker v2.9 policy changes."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from collections import Counter
from pathlib import Path

LATE = {"Item_15", "Item_16", "Signatures"}
START_RE = re.compile(
    r"(?i)^\s*(?:and|or|but|with|of|to|from|which|that|including|"
    r"excluding|through|under|over|as)\b"
)
END_RE = re.compile(
    r"(?i)(?:,|\b(?:and|or|but|with|of|to|from|which|that|including|"
    r"excluding|through|under|over|as))\s*$"
)


def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sections-root", type=Path, required=True)
    parser.add_argument("--chunks-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()

    if args.output_root.exists():
        raise FileExistsError(args.output_root)

    section_rows = read_jsonl(args.sections_root / "sections.jsonl")
    chunk_rows = read_jsonl(args.chunks_root / "chunks.jsonl")
    affected: dict[str, set[str]] = {}

    def mark(section_id: str, reason: str) -> None:
        affected.setdefault(section_id, set()).add(reason)

    for row in section_rows:
        if row["canonical_section_code"] in LATE:
            mark(str(row["section_id"]), "late_container")

    for row in chunk_rows:
        section_id = str(row["section_id"])
        if row["continuation_from_previous"] or row["continues_to_next"]:
            mark(section_id, "existing_continuation")
        if (
            row["rag_action"] == "include"
            and row["chunk_type"] in {"narrative", "list", "mixed_approved"}
        ):
            value = str(row["chunk_text"])
            stripped = value.strip()
            if START_RE.match(stripped):
                mark(section_id, "grammatical_start")
            without_page = re.sub(
                r"(?:\s|\u200e)*\d+\s*$",
                "",
                stripped,
            ).rstrip()
            if END_RE.search(without_page):
                mark(section_id, "grammatical_end")

    selected = [
        row for row in section_rows
        if str(row["section_id"]) in affected
    ]
    selected_ids = {str(row["section_id"]) for row in selected}
    if selected_ids != set(affected):
        missing = sorted(set(affected) - selected_ids)
        raise RuntimeError(f"unknown affected sections: {missing[:10]}")

    args.output_root.mkdir(parents=True)
    output_10k = args.output_root / "10k"
    output_10k.mkdir()

    with (args.output_root / "sections.jsonl").open(
        "w", encoding="utf-8"
    ) as handle:
        for row in selected:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
            source = Path(str(row["output_file"]))
            if not source.is_file():
                source = args.sections_root / "10k" / source.name
            destination = output_10k / source.name
            if not destination.exists():
                try:
                    os.link(source, destination)
                except OSError:
                    shutil.copy2(source, destination)

    status_path = args.sections_root / "document_section_status.jsonl"
    if status_path.is_file():
        accessions = {str(row["accession_number"]) for row in selected}
        statuses = [
            row
            for row in read_jsonl(status_path)
            if str(row["accession_number"]) in accessions
        ]
        with (args.output_root / "document_section_status.jsonl").open(
            "w", encoding="utf-8"
        ) as handle:
            for row in statuses:
                handle.write(json.dumps(row, sort_keys=True) + "\n")

    summary = {
        "affected_sections": len(selected),
        "affected_documents": len(
            {str(row["accession_number"]) for row in selected}
        ),
        "affected_files": len(list(output_10k.glob("*.txt"))),
        "reasons": dict(
            Counter(reason for values in affected.values() for reason in values)
        ),
    }
    (args.output_root / "affected_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
