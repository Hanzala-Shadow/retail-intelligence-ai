"""Merge per-PDF parse-index shards written by a parallel run_pdf_parser_by_year.py
into the real esg_parse_index.csv, then delete the shard directory.

Each shard is a single-PDF index file a parallel worker wrote with its own
--index path (to avoid concurrent writers racing on the shared CSV). This
performs the same upsert (keyed by ticker+pdf_file, last write wins) that
pdf_parser.py itself does, using its own read/write functions so the output
format is identical to a normal run.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)

import config
import pdf_parser  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--index", required=True, help="Real esg_parse_index.csv to merge into.")
    ap.add_argument("--shard-dir", required=True, help="Directory of per-job shard CSVs.")
    ap.add_argument("--keep-shards", action="store_true",
                    help="Don't delete the shard directory after a successful merge.")
    args = ap.parse_args()

    index_path = Path(args.index)
    shard_dir = Path(args.shard_dir)

    shard_files = sorted(shard_dir.glob("*.csv"))
    if not shard_files:
        print(f"No shard files found under {shard_dir}; nothing to merge.")
        return 0

    all_new_rows: list[dict] = []
    for shard in shard_files:
        all_new_rows.extend(pdf_parser.read_existing_index(shard))

    if not all_new_rows:
        print("Shard files were present but contained no rows (all jobs may have failed).")
        return 1

    pdf_parser.upsert_index_rows(index_path, all_new_rows, replace_all=False)
    print(f"Merged {len(all_new_rows)} row(s) from {len(shard_files)} shard(s) into {index_path}")

    if not args.keep_shards:
        for shard in shard_files:
            shard.unlink()
        try:
            shard_dir.rmdir()
        except OSError:
            pass
        print(f"Removed shard directory {shard_dir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
