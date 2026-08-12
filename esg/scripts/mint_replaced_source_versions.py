r"""Mint catalog identity for documents whose bytes were replaced in Drive.

Two documents in the corpus carry no identity IDs: BBY-BEST BUY CO INC-2016
and COST-COSTCO WHOLESALE CORP-2020. Their filenames are in the catalog, but
the sha256 recorded there describes files of 12 MB and 35 MB, while the copies
on disk -- and the copies in Drive as of the 2026-08-12 reconciliation -- are
176 MB and 186 MB. Drive replaced them on 2026-08-07, after the catalog
snapshot was taken.

apply_drive_truth_sync handles this shape by retiring the old row and emitting
a replacement with blank IDs marked ``drive_replaced_needs_lineage_rebuild``.
It does not fill those IDs back in, and nothing else in this repository does
either: identity has always been minted upstream, which is why the parse index
rows for these two documents have been empty rather than wrong.

This mints them locally, and the caveat is worth stating plainly: if the
upstream system later issues its own IDs for these same bytes, the two will
disagree and nothing will flag it. Recorded in ``review_reason`` so the
provenance of these four IDs is visible in the data rather than only here.

What is preserved and what is new
---------------------------------
``logical_source_id`` and ``file_alias_id`` carry over -- it is the same report
under the same filename, so the old and new byte versions stay joined to one
logical source, which is what makes the replacement legible as history rather
than as an unrelated document. ``source_version_id`` and
``extraction_artifact_id`` are new, because they name the bytes.

IDs are derived from the content hash rather than drawn at random, so a second
run against the same file produces the same ID instead of a duplicate.

Run
---
    venv/Scripts/python.exe esg/scripts/mint_replaced_source_versions.py
    venv/Scripts/python.exe esg/scripts/mint_replaced_source_versions.py --apply
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)

import config  # noqa: E402

csv.field_size_limit(2**31 - 1)

# drive_to_db.IDENTITY_ID_RE accepts exactly this shape.
ID_HEX_LEN = 24


def local_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def mint(prefix: str, *parts: str) -> str:
    """A stable identifier for these bytes in this role.

    Derived rather than random so re-running is idempotent: the same file
    yields the same ID instead of a second row claiming the same content. The
    role is part of the input so a source version and its extraction artifact
    do not collide on one hash.
    """
    seed = "|".join(parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(seed).hexdigest()[:ID_HEX_LEN]}"


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
    parser.add_argument("--catalog", type=Path, default=config.ESG_FILE_CATALOG_CSV)
    parser.add_argument("--raw-root", type=Path, default=config.RAW_SUSTAINABILITY_DIR)
    parser.add_argument(
        "--parse-indexes",
        nargs="*",
        type=Path,
        default=[config.ESG_PARSE_INDEX_CSV, config.ESG_PARSE_INDEX_V2_CSV],
    )
    parser.add_argument("--apply", action="store_true", help="write the files (default is a dry run)")
    args = parser.parse_args()

    now = datetime.now(timezone.utc).isoformat()

    with args.catalog.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        catalog_fields = reader.fieldnames or []
        catalog_rows = list(reader)

    active_sha = {
        (row.get("sha256") or "").strip().lower()
        for row in catalog_rows
        if (row.get("active") or "").strip().lower() == "true"
    }

    # A document needs minting when the bytes on disk are not the bytes any
    # active catalog row describes.
    pending: list[tuple[dict, str, int]] = []
    for pdf in sorted(args.raw_root.rglob("*.pdf")):
        digest = local_sha256(pdf)
        if digest in active_sha:
            continue
        row = next((r for r in catalog_rows if (r.get("pdf_file") or "").strip() == pdf.name), None)
        if row is None:
            print(f"  {pdf.name}: no catalog row at all; not a byte replacement, skipped", file=sys.stderr)
            continue
        pending.append((row, digest, pdf.stat().st_size))

    if not pending:
        print("nothing to mint: every local file matches an active catalog row")
        return 0

    print(f"{len(pending)} document(s) need a new source version:\n")
    new_rows: list[dict] = []
    id_by_file: dict[str, dict[str, str]] = {}

    for old_row, digest, size in pending:
        filename = (old_row.get("pdf_file") or "").strip()
        logical = (old_row.get("logical_source_id") or "").strip()
        alias = (old_row.get("file_alias_id") or "").strip()
        source_version = mint("sv", "source_version", digest)
        artifact = mint("ea", "extraction_artifact", digest)

        fresh = dict(old_row)
        fresh.update(
            {
                "source_version_id": source_version,
                "extraction_artifact_id": artifact,
                "sha256": digest,
                "size_bytes": str(size),
                "active": "true",
                "processing_state": "eligible_candidate",
                "review_reason": (
                    "Bytes replaced in Drive after the catalog snapshot; source version "
                    "and extraction artifact minted locally, not issued upstream"
                ),
                "duplicate_of_source_version_id": "",
                "cataloged_at": now,
            }
        )
        new_rows.append(fresh)
        id_by_file[filename] = {
            "logical_source_id": logical,
            "source_version_id": source_version,
            "file_alias_id": alias,
            "extraction_artifact_id": artifact,
        }

        # The superseded row stays, deactivated, so the replacement reads as
        # history instead of the old bytes vanishing from the record.
        old_row["active"] = "false"
        old_row["processing_state"] = "drive_replaced_history"
        old_row["review_reason"] = (
            "Superseded by a different byte version present in Drive and on disk"
        )

        print(f"  {filename}")
        print(f"     logical_source_id      {logical}   (carried over)")
        print(f"     file_alias_id          {alias}   (carried over)")
        print(f"     source_version_id      {source_version}   (new)")
        print(f"     extraction_artifact_id {artifact}   (new)")
        print(f"     sha256 {digest[:16]}...  size {size:,}")

    updated_index_rows = 0
    index_payloads: list[tuple[Path, list[str], list[dict]]] = []
    for index_path in args.parse_indexes:
        if not index_path.exists():
            continue
        with index_path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            fields = reader.fieldnames or []
            rows = list(reader)
        for row in rows:
            ids = id_by_file.get((row.get("pdf_file") or "").strip())
            if not ids:
                continue
            for column, value in ids.items():
                if column in row:
                    row[column] = value
            updated_index_rows += 1
        index_payloads.append((index_path, fields, rows))

    print(f"\ncatalog: {len(pending)} row(s) retired, {len(new_rows)} row(s) added "
          f"({len(catalog_rows)} -> {len(catalog_rows) + len(new_rows)})")
    print(f"parse indexes: {updated_index_rows} row(s) backfilled")

    if not args.apply:
        print("\ndry run; pass --apply to write")
        return 0

    write_atomic(args.catalog, catalog_fields, catalog_rows + new_rows)
    for index_path, fields, rows in index_payloads:
        write_atomic(index_path, fields, rows)
    print("\nwritten")
    return 0


if __name__ == "__main__":
    sys.exit(main())
