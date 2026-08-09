#!/usr/bin/env python3
"""Merge regenerated v2.13 affected sections with unchanged base chunks."""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path


def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-chunks-root", type=Path, required=True)
    parser.add_argument("--affected-chunks-root", type=Path, required=True)
    parser.add_argument("--affected-sections-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()

    staging = args.output_root.with_name(args.output_root.name + ".staging")
    if args.output_root.exists() or staging.exists():
        raise FileExistsError(args.output_root)

    affected_sections = {
        str(row["section_id"])
        for row in read_jsonl(
            args.affected_sections_root / "sections.jsonl"
        )
    }
    print(
        f"PROGRESS stage=merge affected_sections={len(affected_sections)}",
        flush=True,
    )
    base = read_jsonl(args.base_chunks_root / "chunks.jsonl")
    print(f"PROGRESS stage=merge base_loaded={len(base)}", flush=True)
    regenerated = read_jsonl(
        args.affected_chunks_root / "chunks.jsonl"
    )
    print(
        f"PROGRESS stage=merge regenerated_loaded={len(regenerated)}",
        flush=True,
    )
    if not regenerated:
        raise RuntimeError("affected chunk output is empty")

    versions = {str(row["chunker_version"]) for row in regenerated}
    hashes = {str(row["chunker_config_sha256"]) for row in regenerated}
    if versions != {"fy2325-chunker-v2.13"}:
        raise RuntimeError(f"unexpected affected versions: {versions}")
    if len(hashes) != 1:
        raise RuntimeError("affected output has multiple configuration hashes")
    config_hash = next(iter(hashes))

    bad_regenerated = [
        str(row["section_id"])
        for row in regenerated
        if str(row["section_id"]) not in affected_sections
    ]
    if bad_regenerated:
        raise RuntimeError(
            f"unexpected regenerated sections: {bad_regenerated[:10]}"
        )

    unchanged = []
    started = time.monotonic()
    for index, row in enumerate(base, start=1):
        if str(row["section_id"]) not in affected_sections:
            row["chunker_version"] = "fy2325-chunker-v2.13"
            row["chunker_config_sha256"] = config_hash
            unchanged.append(row)
        if index % 10000 == 0 or index == len(base):
            print(
                f"PROGRESS stage=merge-filter {index}/{len(base)} "
                f"({100.0 * index / len(base):.1f}%) "
                f"elapsed={time.monotonic() - started:.1f}s",
                flush=True,
            )

    merged = [*unchanged, *regenerated]
    merged.sort(
        key=lambda row: (
            str(row["ticker"]),
            int(row["coverage_year"]),
            str(row["accession_number"]),
            int(row["source_start_char"]),
            int(row["source_end_char"]),
            str(row["chunk_id"]),
        )
    )
    ids = [str(row["chunk_id"]) for row in merged]
    if len(ids) != len(set(ids)):
        raise RuntimeError("duplicate chunk IDs after merge")

    staging.mkdir(parents=True)
    with (staging / "chunks.jsonl").open("w", encoding="utf-8") as handle:
        for index, row in enumerate(merged, start=1):
            handle.write(json.dumps(row, sort_keys=True) + "\n")
            if index % 10000 == 0 or index == len(merged):
                print(
                    f"PROGRESS stage=merge-write {index}/{len(merged)} "
                    f"({100.0 * index / len(merged):.1f}%)",
                    flush=True,
                )

    summary = {
        "base_chunks": len(base),
        "affected_sections": len(affected_sections),
        "removed_base_chunks": len(base) - len(unchanged),
        "regenerated_chunks": len(regenerated),
        "merged_chunks": len(merged),
        "chunker_version": "fy2325-chunker-v2.13",
        "chunker_config_sha256": config_hash,
        "rag_action_counts": dict(
            Counter(str(row["rag_action"]) for row in merged)
        ),
    }
    (staging / "incremental_merge_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    staging.replace(args.output_root)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
