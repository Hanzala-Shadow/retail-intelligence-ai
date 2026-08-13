"""Export the minimal input an external GPU run needs to embed the corpus.

The chunk index is wide and large, and an embedding run needs almost none of
it: a stable key, the exact text to encode, and a hash to prove the text
survived the round trip.  This writes those three columns, gzipped, so the
payload that leaves the machine is small and verifiable.

Row order is fixed by ``chunk_id``.  The embedding run must preserve it, and
``verify_embeddings`` re-checks the hashes rather than trusting the order.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import sys
from pathlib import Path

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

TRUE_VALUES = {"1", "true", "yes", "y"}
EXPORT_COLUMNS = ("chunk_id", "embedding_text_sha256", "embedding_text")


def repo_root_from_script() -> Path:
    return Path(__file__).resolve().parents[2]


def is_eligible(row: dict[str, str]) -> bool:
    return (row.get("include_in_esg_index") or "").strip().lower() in TRUE_VALUES


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, required=True, help="Chunk index CSV.")
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Gzipped CSV to write. A .manifest.json is written beside it.",
    )
    parser.add_argument(
        "--include-excluded",
        action="store_true",
        help=(
            "Also export rows gated out of retrieval. Off by default: paying "
            "GPU time to embed content the index will never serve is waste."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the output if it already exists.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not args.index.is_file():
        print(f"ERROR: index not found: {args.index}", file=sys.stderr)
        return 2
    if args.out.exists() and not args.force:
        print(f"ERROR: {args.out} exists. Pass --force.", file=sys.stderr)
        return 2

    with args.index.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [
            row
            for row in csv.DictReader(handle)
            if args.include_excluded or is_eligible(row)
        ]

    if not rows:
        print("ERROR: nothing to export.", file=sys.stderr)
        return 1

    rows.sort(key=lambda row: row["chunk_id"])

    # A row whose stored hash disagrees with its text would silently poison the
    # round trip, because the return path is checked against that same hash.
    mismatched = [
        row["chunk_id"]
        for row in rows
        if sha256_text(row["embedding_text"]) != row["embedding_text_sha256"]
    ]
    if mismatched:
        print(
            f"ERROR: {len(mismatched)} rows disagree with their stored "
            f"embedding_text_sha256, first: {mismatched[0]}",
            file=sys.stderr,
        )
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.out, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(EXPORT_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row[column] for column in EXPORT_COLUMNS})

    # Hash the payload itself so the Colab side can prove it received the file
    # intact before spending GPU minutes on it.
    payload_sha = hashlib.sha256(args.out.read_bytes()).hexdigest()
    manifest = {
        "source_index": str(args.index),
        "dataset_ids": sorted({row.get("dataset_id", "") for row in rows}),
        "rows": len(rows),
        "includes_excluded_rows": bool(args.include_excluded),
        "columns": list(EXPORT_COLUMNS),
        "row_order": "sorted by chunk_id",
        "payload_bytes": args.out.stat().st_size,
        "payload_sha256": payload_sha,
        "first_chunk_id": rows[0]["chunk_id"],
        "last_chunk_id": rows[-1]["chunk_id"],
    }
    manifest_path = args.out.with_suffix(args.out.suffix + ".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"Wrote {args.out}")
    print(f"  rows:          {manifest['rows']}")
    print(f"  size:          {manifest['payload_bytes'] / 1048576:.1f} MB")
    print(f"  dataset:       {', '.join(manifest['dataset_ids'])}")
    print(f"  payload sha256:{payload_sha}")
    print(f"  manifest:      {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
