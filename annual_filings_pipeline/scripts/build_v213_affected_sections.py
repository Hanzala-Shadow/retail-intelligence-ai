#!/usr/bin/env python3
"""Select every section affected by v2.13 boundary/auditor corrections."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.chunker_v2 import (
    AUDITOR_PROCEDURE_RE,
    grammatical_continuation_end,
    grammatical_continuation_start,
)


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
    parser.add_argument("--progress-every", type=int, default=10000)
    args = parser.parse_args()

    if args.output_root.exists():
        raise FileExistsError(args.output_root)

    section_rows = list(jsonl(args.sections_root / "sections.jsonl"))
    sections = {
        str(row["section_id"]): row
        for row in section_rows
    }
    affected: dict[str, set[str]] = {}
    findings: list[dict[str, str]] = []
    current_section_id = ""
    current_section_text = ""

    def mark(row: dict[str, object], reason: str) -> None:
        section_id = str(row["section_id"])
        affected.setdefault(section_id, set()).add(reason)
        findings.append({
            "chunk_id": str(row["chunk_id"]),
            "section_id": section_id,
            "reason": reason,
        })

    def source_text(section_id: str) -> str:
        nonlocal current_section_id, current_section_text
        if section_id == current_section_id:
            return current_section_text
        section = sections[section_id]
        path = Path(str(section["output_file"]))
        if not path.is_file():
            path = args.sections_root / "10k" / path.name
        current_section_text = path.read_text(encoding="utf-8")
        current_section_id = section_id
        return current_section_text

    started = time.monotonic()
    scanned = 0
    for row in jsonl(args.chunks_root / "chunks.jsonl"):
        scanned += 1
        flags = set(row["quality_flags"])
        section_id = str(row["section_id"])

        if (
            bool(row["continuation_from_previous"])
            or bool(row["continues_to_next"])
        ):
            mark(row, "existing_continuation")

        # Re-run all sections containing an auditor report so v2.13 can
        # propagate the auditor/CAM state through chunks without repeated
        # auditor keywords.
        if "auditor_opinion" in flags:
            mark(row, "auditor_region")

        if (
            row["rag_action"] == "include"
            and str(row["canonical_section_code"])
            in {"Item_15", "Item_16", "Signatures"}
            and AUDITOR_PROCEDURE_RE.search(str(row["chunk_text"]))
        ):
            mark(row, "included_auditor_procedure")

        if (
            row["rag_action"] == "include"
            and row["chunk_type"] in {"narrative", "list", "mixed_approved"}
        ):
            source = source_text(section_id)
            start = int(row["section_start_char"])
            end = int(row["section_end_char"])
            if (
                grammatical_continuation_start(source, start)
                and not bool(row["continuation_from_previous"])
            ):
                mark(row, "missing_backward_continuation")
            if (
                grammatical_continuation_end(source, end)
                and not bool(row["continues_to_next"])
            ):
                mark(row, "missing_forward_continuation")

        if scanned % max(1, args.progress_every) == 0:
            elapsed = max(0.001, time.monotonic() - started)
            print(
                f"PROGRESS stage=v213-scan chunks={scanned} "
                f"affected_sections={len(affected)} "
                f"elapsed={elapsed:.1f}s",
                flush=True,
            )

    unknown = sorted(set(affected) - set(sections))
    if unknown:
        raise RuntimeError(f"unknown affected sections: {unknown[:10]}")

    selected = [
        row for row in section_rows
        if str(row["section_id"]) in affected
    ]
    args.output_root.mkdir(parents=True)
    output_10k = args.output_root / "10k"
    output_10k.mkdir()

    with (args.output_root / "sections.jsonl").open(
        "w", encoding="utf-8"
    ) as handle:
        for index, row in enumerate(selected, start=1):
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
            if index % 500 == 0 or index == len(selected):
                print(
                    f"PROGRESS stage=v213-copy "
                    f"{index}/{len(selected)}",
                    flush=True,
                )

    status_path = args.sections_root / "document_section_status.jsonl"
    if status_path.is_file():
        accessions = {str(row["accession_number"]) for row in selected}
        with (
            args.output_root / "document_section_status.jsonl"
        ).open("w", encoding="utf-8") as handle:
            for row in jsonl(status_path):
                if str(row["accession_number"]) in accessions:
                    handle.write(json.dumps(row, sort_keys=True) + "\n")

    unique_findings = {
        (row["chunk_id"], row["section_id"], row["reason"]): row
        for row in findings
    }
    with (args.output_root / "affected_findings.jsonl").open(
        "w", encoding="utf-8"
    ) as handle:
        for key in sorted(unique_findings):
            handle.write(
                json.dumps(unique_findings[key], sort_keys=True) + "\n"
            )

    summary = {
        "source_chunks": scanned,
        "affected_sections": len(selected),
        "affected_documents": len({
            str(row["accession_number"]) for row in selected
        }),
        "affected_files": len(list(output_10k.glob("*.txt"))),
        "finding_rows": len(unique_findings),
        "reasons": dict(Counter(
            row["reason"] for row in unique_findings.values()
        )),
    }
    (args.output_root / "affected_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
