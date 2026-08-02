#!/usr/bin/env python3
"""Verify an ESG flattened baseline without using earlier candidates."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-root", required=True, type=Path)
    args = parser.parse_args()

    root = args.baseline_root.resolve()
    repo = Path.cwd().resolve()
    info = json.loads((root / "build_info.json").read_text(encoding="utf-8"))
    sections = rows(root / "esg_sections_index.csv")
    chunks = rows(root / "esg_chunks_index.csv")
    holds = rows(root / "esg_section_hold.csv")
    manifest = rows(root / "content_manifest.csv")
    errors: list[str] = []

    for item in manifest:
        path = root / item["relative_path"]
        if not path.is_file():
            errors.append(f"missing manifest file: {item['relative_path']}")
            continue
        if path.stat().st_size != int(item["size_bytes"]):
            errors.append(f"size mismatch: {item['relative_path']}")
        elif sha256(path) != item["sha256"]:
            errors.append(f"hash mismatch: {item['relative_path']}")

    expected_prefix = root.relative_to(repo).as_posix() + "/"
    for row in sections:
        path_text = row["section_file"]
        if not path_text.startswith(expected_prefix) or not (repo / path_text).is_file():
            errors.append(f"bad section path: {path_text}")
    for row in chunks:
        for field in ("chunk_file", "source_section_file"):
            path_text = row[field]
            if not path_text.startswith(expected_prefix) or not (repo / path_text).is_file():
                errors.append(f"bad {field}: {path_text}")

    hold_keys = {
        (row["ticker"], row["pdf_stem"], row["section_instance_id"])
        for row in holds
    }
    held_chunks = [
        row
        for row in chunks
        if (row["ticker"], row["pdf_stem"], row["section_instance_id"])
        in hold_keys
    ]
    held_eligible = [
        row
        for row in held_chunks
        if row["include_in_esg_index"].strip().lower() in {"1", "true", "yes"}
    ]
    counts = info["counts"]
    actual = {
        "sections": len(sections),
        "chunks": len(chunks),
        "eligible_chunks": sum(
            row["include_in_esg_index"].strip().lower() in {"1", "true", "yes"}
            for row in chunks
        ),
        "held_sections": len(holds),
        "held_chunks": len(held_chunks),
        "held_eligible_chunks": len(held_eligible),
        "manifest_files": len(manifest),
        "max_tokens": max(int(row["token_count"]) for row in chunks),
    }
    for key, value in actual.items():
        if counts[key] != value:
            errors.append(f"count mismatch for {key}: {value} != {counts[key]}")

    result = {
        "status": "FAIL" if errors else "PASS",
        "baseline_root": root.relative_to(repo).as_posix(),
        "counts": actual,
        "holds": sorted(row["ticker"] for row in holds),
        "errors": errors[:100],
    }
    print(json.dumps(result, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
