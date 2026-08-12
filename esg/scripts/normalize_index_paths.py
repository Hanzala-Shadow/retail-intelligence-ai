r"""Rewrite machine-specific absolute paths in the reference indexes.

The server transfer guide forbids persisting environment-specific absolute
paths in CSV indexes and SQLite rows, because the package is extracted on a
machine where those directories do not exist. The corpus violates this in two
ways at once: it arrived carrying paths rooted at a former developer's
``C:/Users/Aziz/Documents/ChatGPT Codex/Retail-Document-Intelligence`` profile,
and the bridge was itself writing absolute paths against whichever checkout
produced the run.

The bridge now stores repository-relative paths (see ``store_path`` there), so
this script exists for the indexes already on disk. It rewrites any value that
starts with a drive letter or a POSIX home prefix down to its ``data/...``
suffix. Nothing else in the row is touched -- the hashes, sizes and identity
IDs stay exactly as they were, because only the locator was ever wrong.

Fails closed: if a path cannot be reduced to a repository-relative form the
file is left alone and the offenders are printed, rather than writing a file
that is half-normalised and passes a shallow grep.

Run
---
    venv/Scripts/python.exe esg/scripts/normalize_index_paths.py --check
    venv/Scripts/python.exe esg/scripts/normalize_index_paths.py --apply
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from pathlib import Path

import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)

import config  # noqa: E402

# embedding_text carries whole chunks; the default limit stops the reader dead.
csv.field_size_limit(2**31 - 1)

# A drive letter, a UNC share, or a POSIX home directory. Anything matching is
# machine-specific by construction.
ABSOLUTE_RE = re.compile(r"^([A-Za-z]:[\\/]|\\\\|/home/|/Users/)")

# The repository-relative anchor every stored path shares. Splitting on it is
# what makes a foreign path recoverable without needing the foreign machine:
# the tail below data/ is identical across checkouts.
ANCHOR = "data/"

DEFAULT_INDEXES = [
    "esg_parse_index.csv",
    "esg_parse_index_v2.csv",
    "esg_sections_index.csv",
    "esg_chunks_index.csv",
]


def normalize(value: str) -> str | None:
    """Repository-relative form of one cell, or None when it cannot be reduced.

    Returns the value unchanged when it is already relative or empty, so the
    script is idempotent and safe to re-run after a partial fix.
    """
    text = (value or "").strip()
    if not text or not ABSOLUTE_RE.match(text):
        return value
    posix = text.replace("\\", "/")
    marker = posix.rfind("/" + ANCHOR)
    if marker == -1:
        return None
    return posix[marker + 1 :]


def scan_file(path: Path) -> tuple[list[str], dict[str, int], list[tuple[str, str]], list[dict]]:
    """Return (fieldnames, per-column rewrite counts, unrecoverable, rows)."""
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    counts: dict[str, int] = {}
    unrecoverable: list[tuple[str, str]] = []
    for row in rows:
        for column, value in row.items():
            text = str(value or "")
            if not ABSOLUTE_RE.match(text.strip()):
                continue
            replacement = normalize(text)
            if replacement is None:
                unrecoverable.append((column, text))
                continue
            row[column] = replacement
            counts[column] = counts.get(column, 0) + 1
    return fieldnames, counts, unrecoverable, rows


def write_atomic(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-dir", type=Path, default=config.REFERENCE_DIR)
    parser.add_argument("--indexes", nargs="*", default=DEFAULT_INDEXES)
    parser.add_argument("--apply", action="store_true", help="write the files (default is a dry run)")
    args = parser.parse_args()

    total = 0
    blocked = 0
    for name in args.indexes:
        path = args.reference_dir / name
        if not path.exists():
            print(f"{name}: absent, skipped")
            continue

        fieldnames, counts, unrecoverable, rows = scan_file(path)
        found = sum(counts.values())
        total += found

        if not found and not unrecoverable:
            print(f"{name}: clean ({len(rows)} rows)")
            continue

        print(f"{name}: {len(rows)} rows, {found} absolute path(s)")
        for column, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"    {column:24s} {count}")

        if unrecoverable:
            blocked += len(unrecoverable)
            print(f"    {len(unrecoverable)} value(s) have no '{ANCHOR}' segment and were NOT rewritten:")
            for column, value in unrecoverable[:5]:
                print(f"      {column}: {value[:90]}")
            print("    file left unchanged")
            continue

        if args.apply:
            write_atomic(path, fieldnames, rows)
            print("    rewritten")
        else:
            print("    dry run; pass --apply to write")

    print()
    if blocked:
        print(f"{blocked} value(s) could not be normalised; nothing written for those files")
        return 1
    if not args.apply and total:
        print(f"{total} path(s) would be rewritten. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
