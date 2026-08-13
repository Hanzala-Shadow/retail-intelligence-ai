#!/usr/bin/env python3
"""Build the minimal section corpus touched by auditor-state correction."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from collections import Counter
from pathlib import Path


def jsonl(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sections-root", type=Path, required=True)
    parser.add_argument("--chunks-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()

    staging = args.output_root.with_name(args.output_root.name + ".staging")
    if args.output_root.exists() or staging.exists():
        raise FileExistsError(args.output_root)

    affected: dict[str, set[str]] = {}
    scanned = 0
    auditor_chunks = 0

    for row in jsonl(args.chunks_root / "chunks.jsonl"):
        scanned += 1
        flags = set(row.get("quality_flags", []))
        if "auditor_opinion" in flags:
            auditor_chunks += 1
            affected.setdefault(str(row["section_id"]), set()).add(
                "auditor_opinion"
            )
        if scanned % 10000 == 0:
            print(
                f"PROGRESS stage=affected-scan chunks={scanned} "
                f"auditor_chunks={auditor_chunks} "
                f"sections={len(affected)}",
                flush=True,
            )

    section_rows = list(
        jsonl(args.sections_root / "sections.jsonl")
    )
    selected = [
        row
        for row in section_rows
        if str(row["section_id"]) in affected
    ]
    selected_ids = {str(row["section_id"]) for row in selected}
    if selected_ids != set(affected):
        missing = sorted(set(affected) - selected_ids)
        raise RuntimeError(
            f"unknown affected sections: {missing[:10]}"
        )

    staging.mkdir(parents=True)
    output_10k = staging / "10k"
    output_10k.mkdir()

    with (staging / "sections.jsonl").open(
        "w", encoding="utf-8"
    ) as handle:
        for index, row in enumerate(selected, start=1):
            source = Path(str(row["output_file"]))
            if not source.is_file():
                source = args.sections_root / "10k" / source.name
            destination = output_10k / source.name
            if not destination.exists():
                try:
                    os.link(source, destination)
                except OSError:
                    shutil.copy2(source, destination)

            local_row = dict(row)
            local_row["output_file"] = str(
                args.output_root / "10k" / source.name
            )
            handle.write(
                json.dumps(local_row, sort_keys=True) + "\n"
            )
            if index % 100 == 0 or index == len(selected):
                print(
                    f"PROGRESS stage=affected-copy "
                    f"{index}/{len(selected)}",
                    flush=True,
                )

    accessions = {
        str(row["accession_number"]) for row in selected
    }
    status_path = (
        args.sections_root / "document_section_status.jsonl"
    )
    if status_path.is_file():
        with (staging / "document_section_status.jsonl").open(
            "w", encoding="utf-8"
        ) as handle:
            for row in jsonl(status_path):
                if str(row["accession_number"]) in accessions:
                    handle.write(
                        json.dumps(row, sort_keys=True) + "\n"
                    )

    summary = {
        "source_chunks_scanned": scanned,
        "auditor_flagged_chunks": auditor_chunks,
        "affected_sections": len(selected),
        "affected_documents": len(accessions),
        "affected_files": len(list(output_10k.glob("*.txt"))),
        "affected_section_codes": dict(
            Counter(
                str(row["canonical_section_code"])
                for row in selected
            )
        ),
    }
    (staging / "affected_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    staging.replace(args.output_root)
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
