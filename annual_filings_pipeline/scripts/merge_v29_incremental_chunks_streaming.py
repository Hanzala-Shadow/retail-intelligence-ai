#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import Counter
from pathlib import Path

VERSION = "fy2325-chunker-v2.16"


def jsonl(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


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
        for row in jsonl(args.affected_sections_root / "sections.jsonl")
    }
    print(
        f"PROGRESS stage=scan affected_sections={len(affected_sections)}",
        flush=True,
    )

    affected_path = args.affected_chunks_root / "chunks.jsonl"
    versions = set()
    hashes = set()
    regenerated_count = 0

    for row in jsonl(affected_path):
        regenerated_count += 1
        versions.add(str(row["chunker_version"]))
        hashes.add(str(row["chunker_config_sha256"]))

        if str(row["section_id"]) not in affected_sections:
            raise RuntimeError(
                f"unexpected regenerated section: {row['section_id']}"
            )

        if regenerated_count % 10000 == 0:
            print(
                f"PROGRESS stage=scan-regenerated "
                f"chunks={regenerated_count}",
                flush=True,
            )

    if regenerated_count == 0:
        raise RuntimeError("affected chunk output is empty")
    if versions != {VERSION}:
        raise RuntimeError(f"unexpected affected versions: {versions}")
    if len(hashes) != 1:
        raise RuntimeError("affected output has multiple configuration hashes")

    config_hash = next(iter(hashes))
    staging.mkdir(parents=True)

    database_path = staging / "merge.sqlite3"
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute("PRAGMA temp_store=FILE")
    connection.execute("PRAGMA cache_size=-65536")

    connection.execute("""
        CREATE TABLE chunks (
            chunk_id TEXT PRIMARY KEY,
            section_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            coverage_year INTEGER NOT NULL,
            accession_number TEXT NOT NULL,
            source_start_char INTEGER NOT NULL,
            source_end_char INTEGER NOT NULL,
            rag_action TEXT NOT NULL,
            payload TEXT NOT NULL
        )
    """)

    insert_sql = """
        INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    base_count = 0
    removed_count = 0
    retained_count = 0
    started = time.monotonic()

    for row in jsonl(args.base_chunks_root / "chunks.jsonl"):
        base_count += 1

        if str(row["section_id"]) in affected_sections:
            removed_count += 1
        else:
            row["chunker_version"] = VERSION
            row["chunker_config_sha256"] = config_hash

            connection.execute(insert_sql, (
                str(row["chunk_id"]),
                str(row["section_id"]),
                str(row["ticker"]),
                int(row["coverage_year"]),
                str(row["accession_number"]),
                int(row["source_start_char"]),
                int(row["source_end_char"]),
                str(row["rag_action"]),
                json.dumps(row, sort_keys=True),
            ))
            retained_count += 1

        if base_count % 10000 == 0:
            connection.commit()
            print(
                f"PROGRESS stage=base chunks={base_count} "
                f"retained={retained_count} removed={removed_count} "
                f"elapsed={time.monotonic() - started:.1f}s",
                flush=True,
            )

    connection.commit()

    inserted_regenerated = 0
    for row in jsonl(affected_path):
        connection.execute(insert_sql, (
            str(row["chunk_id"]),
            str(row["section_id"]),
            str(row["ticker"]),
            int(row["coverage_year"]),
            str(row["accession_number"]),
            int(row["source_start_char"]),
            int(row["source_end_char"]),
            str(row["rag_action"]),
            json.dumps(row, sort_keys=True),
        ))
        inserted_regenerated += 1

        if inserted_regenerated % 10000 == 0:
            connection.commit()
            print(
                f"PROGRESS stage=regenerated "
                f"{inserted_regenerated}/{regenerated_count}",
                flush=True,
            )

    connection.commit()

    merged_count = connection.execute(
        "SELECT COUNT(*) FROM chunks"
    ).fetchone()[0]

    actions = Counter(dict(connection.execute(
        "SELECT rag_action, COUNT(*) FROM chunks GROUP BY rag_action"
    )))

    output_path = staging / "chunks.jsonl"
    written = 0

    query = """
        SELECT payload
        FROM chunks
        ORDER BY
            ticker,
            coverage_year,
            accession_number,
            source_start_char,
            source_end_char,
            chunk_id
    """

    with output_path.open("w", encoding="utf-8") as handle:
        for (payload,) in connection.execute(query):
            handle.write(payload + "\n")
            written += 1

            if written % 10000 == 0 or written == merged_count:
                print(
                    f"PROGRESS stage=write "
                    f"{written}/{merged_count} "
                    f"({100 * written / merged_count:.1f}%)",
                    flush=True,
                )

    summary = {
        "base_chunks": base_count,
        "affected_sections": len(affected_sections),
        "removed_base_chunks": removed_count,
        "regenerated_chunks": inserted_regenerated,
        "merged_chunks": merged_count,
        "chunker_version": VERSION,
        "chunker_config_sha256": config_hash,
        "rag_action_counts": dict(actions),
        "merge_method": "sqlite_streaming_v1",
    }

    (staging / "incremental_merge_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    connection.close()
    database_path.unlink()
    staging.replace(args.output_root)

    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
