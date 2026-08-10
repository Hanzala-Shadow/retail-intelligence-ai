"""Build a fixed-size handoff sample from a chunk index.

The full handout under ``data/handout`` mirrors the entire corpus.  This
script instead cuts a small, representative slice for review or for an
external embedding run: a chunk index CSV plus the chunk text files it
points at, laid out the same way the real corpus is.

Only rows the pipeline marked ``include_in_esg_index`` are eligible, so the
sample never hands out navigation traces or encoding-damaged chunks.  The
draw is stratified across companies and seeded, so the same arguments always
produce the same sample.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Chunk rows carry the full embedding text, which is far wider than the
# default field limit.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

TRUE_VALUES = {"1", "true", "yes", "y"}
DEFAULT_SAMPLE_SIZE = 2000
DEFAULT_SEED = 20260808

# Companion records a full-corpus handout carries beside the chunk index, so
# the receiver can trace a chunk back through sections, parsing and sourcing
# without the rest of the repo.  Backup subdirectories are deliberately left
# behind; only the current records travel.
COMPANION_REFERENCE_CSVS = (
    "companies.csv",
    "esg_accepted_company_manifest.csv",
    "esg_drive_manifest.csv",
    "esg_file_catalog.csv",
    "esg_ocr_approval.csv",
    "esg_parse_index.csv",
    "esg_parse_index_v2.csv",
    "esg_sections_index.csv",
    "sustainability_report_tracker.csv",
)


def repo_root_from_script() -> Path:
    return Path(__file__).resolve().parents[2]


def is_included(row: dict[str, str]) -> bool:
    return (row.get("include_in_esg_index") or "").strip().lower() in TRUE_VALUES


def content_type_of(row: dict[str, str]) -> str:
    """Read the content-type label back out of the embedding header."""
    for line in (row.get("embedding_text") or "").split("\n")[:10]:
        if line.startswith("Content type:"):
            return line.split(":", 1)[1].strip()
    return "unknown"


def stratified_sample(
    rows: list[dict[str, str]], size: int, seed: int
) -> list[dict[str, str]]:
    """Draw `size` rows, spreading them as evenly as possible over companies.

    Companies are visited in round-robin order, so every company is
    represented before any company contributes a second chunk.  Within a
    company the order is shuffled, keeping the draw reproducible without
    favouring whichever report happened to be chunked first.
    """
    by_ticker: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_ticker[row.get("canonical_ticker") or "UNKNOWN"].append(row)

    rng = random.Random(seed)
    for ticker_rows in by_ticker.values():
        rng.shuffle(ticker_rows)

    picked: list[dict[str, str]] = []
    tickers = sorted(by_ticker)
    depth = 0
    while len(picked) < size:
        progressed = False
        for ticker in tickers:
            ticker_rows = by_ticker[ticker]
            if depth >= len(ticker_rows):
                continue
            picked.append(ticker_rows[depth])
            progressed = True
            if len(picked) >= size:
                break
        if not progressed:
            break
        depth += 1
    return picked


def copy_chunk_files(
    rows: list[dict[str, str]], repo_root: Path, out_root: Path
) -> tuple[int, list[str]]:
    copied = 0
    missing: list[str] = []
    for row in rows:
        rel = (row.get("chunk_file") or "").strip()
        if not rel:
            missing.append(row.get("chunk_id", "<no chunk_id>"))
            continue
        source = repo_root / rel
        if not source.is_file():
            missing.append(rel)
            continue
        destination = out_root / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied += 1
    return copied, missing


def copy_reference_csvs(
    reference_dir: Path, out_root: Path
) -> tuple[list[str], list[str]]:
    """Copy the companion records that sit beside the chunk index."""
    copied: list[str] = []
    missing: list[str] = []
    destination_dir = out_root / "00_reference"
    destination_dir.mkdir(parents=True, exist_ok=True)
    for name in COMPANION_REFERENCE_CSVS:
        source = reference_dir / name
        if not source.is_file():
            missing.append(name)
            continue
        shutil.copy2(source, destination_dir / name)
        copied.append(name)
    return copied, missing


def write_index(rows: list[dict[str, str]], fieldnames: list[str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_manifest(
    rows: list[dict[str, str]],
    args: argparse.Namespace,
    eligible_total: int,
    index_total: int,
    missing: list[str],
) -> dict:
    return {
        "mode": "full_corpus" if args.all else "stratified_sample",
        "sample_size": len(rows),
        "requested_size": None if args.all else args.size,
        "seed": None if args.all else args.seed,
        "includes_excluded_rows": bool(args.include_excluded),
        "index_only": bool(args.index_only),
        "rows_included_in_index": sum(1 for r in rows if is_included(r)),
        "rows_excluded_from_index": sum(1 for r in rows if not is_included(r)),
        "source_index": str(args.index),
        "index_rows_total": index_total,
        "eligible_rows_total": eligible_total,
        "dataset_ids": sorted({r.get("dataset_id", "") for r in rows}),
        "companies": len({r.get("canonical_ticker") for r in rows}),
        "source_documents": len({r.get("source_id") for r in rows}),
        "content_types": dict(Counter(content_type_of(r) for r in rows).most_common()),
        "chunk_types": dict(Counter(r.get("chunk_type", "") for r in rows).most_common()),
        "missing_chunk_files": missing,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    repo_root = repo_root_from_script()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--index",
        type=Path,
        required=True,
        help="Chunk index CSV to sample from.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Handoff root. The corpus layout is recreated underneath it.",
    )
    parser.add_argument(
        "--size",
        type=int,
        default=DEFAULT_SAMPLE_SIZE,
        help=f"Number of chunks to hand off (default {DEFAULT_SAMPLE_SIZE}).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Hand off the whole corpus instead of a sample. Ignores --size.",
    )
    parser.add_argument(
        "--include-excluded",
        action="store_true",
        help=(
            "Keep rows the pipeline excluded from retrieval, flags intact, so "
            "the receiver can audit what was dropped and why."
        ),
    )
    parser.add_argument(
        "--with-reference",
        action="store_true",
        help="Copy the companion reference CSVs beside the chunk index.",
    )
    parser.add_argument(
        "--index-only",
        action="store_true",
        help=(
            "Write just the chunk index and skip the chunk text files. The "
            "index still carries embedding_text, which is what gets vectorised."
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=f"Sampling seed (default {DEFAULT_SEED}).",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=repo_root,
        help="Root the index's relative chunk_file paths resolve against.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace the output directory if it already exists.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not args.index.is_file():
        print(f"ERROR: index not found: {args.index}", file=sys.stderr)
        return 2

    if args.out.exists():
        if not args.force:
            print(
                f"ERROR: {args.out} already exists. Pass --force to replace it.",
                file=sys.stderr,
            )
            return 2
        shutil.rmtree(args.out)

    with args.index.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        all_rows = list(reader)

    if args.include_excluded:
        eligible = all_rows
    else:
        eligible = [row for row in all_rows if is_included(row)]
    if not eligible:
        print("ERROR: no rows left to hand off.", file=sys.stderr)
        return 1

    if args.all:
        picked = list(eligible)
    else:
        if len(eligible) < args.size:
            print(
                f"WARNING: only {len(eligible)} eligible rows for a "
                f"{args.size}-chunk request.",
                file=sys.stderr,
            )
        picked = stratified_sample(eligible, args.size, args.seed)
    picked.sort(key=lambda row: row.get("chunk_id", ""))

    if args.index_only:
        copied, missing = 0, []
    else:
        copied, missing = copy_chunk_files(picked, args.repo_root, args.out)
    write_index(picked, fieldnames, args.out / "00_reference" / "esg_chunks_index.csv")

    manifest = build_manifest(picked, args, len(eligible), len(all_rows), missing)
    manifest["chunk_files_copied"] = copied

    if args.with_reference:
        reference_copied, reference_missing = copy_reference_csvs(
            args.index.parent, args.out
        )
        manifest["reference_csvs"] = reference_copied
        manifest["reference_csvs_missing"] = reference_missing

    manifest_path = args.out / "handoff_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"Handoff written to {args.out}")
    print(f"  mode:             {manifest['mode']}")
    print(f"  chunks:           {len(picked)}")
    print(
        f"    retrievable:    {manifest['rows_included_in_index']}"
        f"  excluded: {manifest['rows_excluded_from_index']}"
    )
    print(f"  companies:        {manifest['companies']}")
    print(f"  source documents: {manifest['source_documents']}")
    print(f"  chunk files:      {'skipped (--index-only)' if args.index_only else copied}")
    print(f"  content types:    {manifest['content_types']}")
    if args.with_reference:
        print(f"  reference CSVs:   {len(manifest.get('reference_csvs', []))}")
    if missing:
        print(f"  WARNING: {len(missing)} chunk files missing", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
