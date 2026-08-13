"""Emit DATABASE_AUDIT.json for the packaged SQLite QA mirror.

The server transfer guide requires the archive to carry evidence that the
database it ships is internally consistent and agrees with the canonical CSV
indexes, so the receiving side can audit before installing rather than after.
This writes that evidence: file digest, schema inventory, SQLite's own
integrity pragmas, the canonical counts, duplicate-ID checks, and index/file
agreement.

Read-only against the database. Safe to run on a staged copy, which is what
the guide asks for -- acceptance checks run against staging, not the source
repository.

Run
---
    venv/Scripts/python.exe esg/scripts/audit_esg_qa_db.py
    venv/Scripts/python.exe esg/scripts/audit_esg_qa_db.py --db <path> --out <path>
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)

import config  # noqa: E402

# Raised well above the csv default: embedding_text carries whole chunks, and
# the reader dies on the first oversized field rather than skipping it.
csv.field_size_limit(2**31 - 1)

DATASET_ID = "esg_docling_fusion_v2"
CHUNKER_VERSION = "esg_chunk_v4"


def relative_to_repo(path: Path) -> str:
    """Repo-relative POSIX path, so the audit does not name the build machine.

    This file ships inside the transfer package as DATABASE_AUDIT.json, and the
    guide's portability rule is explicit: do not store environment-specific
    absolute paths in CSV indexes, SQLite rows, JSON manifests or serialized
    objects. Writing ``str(args.db)`` broke that the moment the audit ran with
    an absolute ``--db`` -- the 2026-08-12 package carried
    ``C:\\Users\\<name>\\Desktop\\Final\\data\\esg.db``, which is both a drive
    letter and a named user profile, the two things the gate names.

    Outside the repo -- which is the normal case when auditing a staged package,
    since staging deliberately sits outside it -- falls back to the last two
    components. That yields "data/esg.db" for a staged copy, matching what
    PACKAGE_METADATA.json reports, and at worst discloses one directory name
    rather than a drive letter and user profile.
    """
    resolved = path.resolve()
    try:
        return resolved.relative_to(config.REPO_ROOT.resolve()).as_posix()
    except ValueError:
        parent = resolved.parent.name
        return f"{parent}/{resolved.name}" if parent else resolved.name


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


def git_commit(repo_root: Path) -> str | None:
    """Current commit, or None when this is not a git checkout.

    The guide accepts "no git commit exists" as an answer but not a silently
    wrong one, so a failure here reports absence rather than guessing.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def table_inventory(conn: sqlite3.Connection) -> dict[str, int]:
    names = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    inventory: dict[str, int] = {}
    for name in names:
        # Identifier is quoted rather than parameterised: SQLite does not bind
        # table names, and every name here came from sqlite_master.
        inventory[name] = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
    return inventory


def duplicate_count(conn: sqlite3.Connection, table: str, column: str) -> int | None:
    """Ids shared by more than one row. None when the table or column is absent.

    The column has to be confirmed against table_info first. SQLite resolves a
    double-quoted name that matches no column as a string literal instead of
    raising, so `SELECT "doc_id" FROM documents GROUP BY "doc_id"` on a table
    without that column groups every row under one constant and reports a
    duplicate. That misfeature made this audit's first run claim duplicate
    document ids in a database that had none.
    """
    try:
        columns = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
    except sqlite3.OperationalError:
        return None
    if column not in columns:
        return None
    row = conn.execute(
        f'SELECT COUNT(*) FROM (SELECT "{column}" FROM "{table}" '
        f'GROUP BY "{column}" HAVING COUNT(*) > 1)'
    ).fetchone()
    return row[0]


def csv_stats(path: Path, action_column: str = "rag_action") -> dict[str, int]:
    """Row count and retrieval-gate split straight from the canonical index.

    Counted from the CSV rather than the database on purpose: the point of the
    audit is to show the two agree, which a single source cannot demonstrate.
    """
    stats = {"rows": 0, "eligible": 0, "excluded": 0, "other_action": 0}
    if not path.exists():
        return stats
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            stats["rows"] += 1
            action = (row.get(action_column) or "").strip()
            if action == "index_as_esg":
                stats["eligible"] += 1
            elif action == "exclude_from_esg_index":
                stats["excluded"] += 1
            elif action:
                stats["other_action"] += 1
    return stats


def resolve(value: str, root: Path) -> Path:
    """Resolve an index path against `root`, tolerating absolute legacy values."""
    p = Path(value)
    return p if p.is_absolute() else root / p


def citation_checks(
    chunks_index: Path,
    parse_index: Path,
    root: Path,
    verify_slices: bool = True,
) -> dict:
    """Citation and hash mismatch counts, which the guide requires by name.

    The chunk index claims every chunk carries an exact source slice
    (citation_validation_status=verified_exact). This re-derives that claim
    instead of trusting the column: source_start_char/source_end_char index
    into the *parsed document text* under 03_pipeline_text -- not the section
    file, which was the obvious wrong guess and reproduces the chunk for
    roughly one row in five by coincidence -- so the check slices the parsed
    text and compares the digest to chunk_text_sha256.

    Comparing digests rather than reading each chunk file keeps this to one
    read per document (681) instead of one per chunk (50,530), and still
    catches a wrong offset, a truncated slice or a stale hash.

    verify_slices=False keeps the cheap column-level counts and skips the
    re-derivation; the flag is recorded in the output so a zero is never read
    as evidence of a check that did not run.
    """
    stats = {
        "chunks_checked": 0,
        "not_citation_ready": 0,
        "not_verified_exact": 0,
        "chunk_text_sha256_missing": 0,
        "embedding_text_sha256_missing": 0,
        "page_range_invalid": 0,
        "offset_invalid": 0,
        "slices_verified": 0,
        "parsed_text_missing": 0,
        "offset_overflow": 0,
        "slice_hash_mismatch": 0,
        "deep_verification": bool(verify_slices),
    }
    if not chunks_index.exists():
        return stats

    parsed_for: dict[str, str] = {}
    if parse_index.exists():
        with parse_index.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                stem = Path((row.get("pdf_file") or "").strip()).stem
                target = (row.get("parsed_text_file") or "").strip()
                if stem and target:
                    parsed_for[stem] = target

    by_doc: dict[str, list[tuple[int, int, str]]] = {}
    with chunks_index.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            stats["chunks_checked"] += 1
            if (row.get("citation_ready") or "").strip() != "true":
                stats["not_citation_ready"] += 1
            if (row.get("citation_validation_status") or "").strip() != "verified_exact":
                stats["not_verified_exact"] += 1
            chunk_sha = (row.get("chunk_text_sha256") or "").strip()
            if not chunk_sha:
                stats["chunk_text_sha256_missing"] += 1
            if not (row.get("embedding_text_sha256") or "").strip():
                stats["embedding_text_sha256_missing"] += 1

            try:
                page_start = int(row["page_start"])
                page_end = int(row["page_end"])
                if page_start < 1 or page_end < page_start:
                    stats["page_range_invalid"] += 1
            except (KeyError, TypeError, ValueError):
                stats["page_range_invalid"] += 1

            try:
                start = int(row["source_start_char"])
                end = int(row["source_end_char"])
            except (KeyError, TypeError, ValueError):
                stats["offset_invalid"] += 1
                continue
            if start < 0 or end <= start:
                stats["offset_invalid"] += 1
                continue
            if verify_slices and chunk_sha:
                by_doc.setdefault((row.get("pdf_stem") or "").strip(), []).append(
                    (start, end, chunk_sha)
                )

    if not verify_slices:
        return stats

    for stem, wanted in by_doc.items():
        target = parsed_for.get(stem)
        path = resolve(target, root) if target else None
        if path is None or not path.exists():
            stats["parsed_text_missing"] += len(wanted)
            continue
        text = path.read_text(encoding="utf-8")
        size = len(text)
        for start, end, chunk_sha in wanted:
            if end > size:
                stats["offset_overflow"] += 1
                continue
            digest = hashlib.sha256(text[start:end].encode("utf-8")).hexdigest()
            if digest == chunk_sha:
                stats["slices_verified"] += 1
            else:
                stats["slice_hash_mismatch"] += 1
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=config.ESG_DB)
    parser.add_argument("--out", type=Path, default=Path("DATABASE_AUDIT.json"))
    parser.add_argument("--sections-index", type=Path, default=config.ESG_SECTIONS_INDEX_CSV)
    parser.add_argument("--chunks-index", type=Path, default=config.ESG_CHUNKS_INDEX_CSV)
    parser.add_argument("--sections-dir", type=Path, default=config.ESG_SECTIONS_DIR)
    parser.add_argument("--chunks-dir", type=Path, default=config.ESG_CHUNKS_DIR)
    parser.add_argument("--parse-index", type=Path, default=config.ESG_PARSE_INDEX_V2_CSV)
    parser.add_argument(
        "--root", type=Path, default=config.REPO_ROOT,
        help="base for repo-relative paths inside the indexes; point at staging "
             "when auditing a staged package rather than the repository",
    )
    parser.add_argument(
        "--skip-citation-verify", action="store_true",
        help="keep the column-level citation counts but skip re-deriving each "
             "slice from the parsed text",
    )
    args = parser.parse_args()

    if not args.db.exists():
        print(f"no database at {args.db}", file=sys.stderr)
        return 2

    # A live WAL or SHM alongside the file means a writer may still be attached
    # and the copy is not a consistent snapshot. The guide fails closed here.
    sidecars = [
        p.name
        for p in (args.db.with_suffix(args.db.suffix + "-wal"), args.db.with_suffix(args.db.suffix + "-shm"))
        if p.exists()
    ]

    conn = sqlite3.connect(f"file:{args.db.as_posix()}?mode=ro", uri=True)
    try:
        quick = conn.execute("PRAGMA quick_check").fetchone()[0]
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = [dict(zip(("table", "rowid", "parent", "fkid"), r)) for r in conn.execute("PRAGMA foreign_key_check")]
        tables = table_inventory(conn)
        sqlite_version = conn.execute("SELECT sqlite_version()").fetchone()[0]
        duplicates = {
            "documents.doc_id": duplicate_count(conn, "documents", "doc_id"),
            "sections.section_id": duplicate_count(conn, "sections", "section_id"),
            "chunks.chunk_id": duplicate_count(conn, "chunks", "chunk_id"),
            "chunks.external_chunk_id": duplicate_count(conn, "chunks", "external_chunk_id"),
        }
        # An empty table that other tables have foreign keys into is the shape
        # this database actually failed in, and it reads as healthy in a plain
        # row-count inventory. Reported, but not a failure on its own: an empty
        # table whose referencing columns are all NULL is legitimate, which is
        # exactly the case for source_approvals in a corpus where no OCR
        # substitution was ever approved. foreign_key_check is what decides.
        empty_referenced = sorted(
            {
                r[2]
                for t in tables
                for r in conn.execute(f'PRAGMA foreign_key_list("{t}")')
                if tables.get(r[2]) == 0
            }
        )
    finally:
        conn.close()

    sections_csv = csv_stats(args.sections_index)
    chunks_csv = csv_stats(args.chunks_index)
    citation = citation_checks(
        args.chunks_index,
        args.parse_index,
        args.root,
        verify_slices=not args.skip_citation_verify,
    )
    section_files = sum(1 for _ in args.sections_dir.rglob("*.txt")) if args.sections_dir.exists() else 0
    chunk_files = sum(1 for p in args.chunks_dir.rglob("*") if p.is_file()) if args.chunks_dir.exists() else 0

    audit = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_commit": git_commit(config.REPO_ROOT),
        "dataset_id": DATASET_ID,
        "chunker_version": CHUNKER_VERSION,
        "database": {
            "path": relative_to_repo(args.db),
            "sha256": sha256_file(args.db),
            "size_bytes": args.db.stat().st_size,
            "sqlite_version": sqlite_version,
            "quick_check": quick,
            "integrity_check": integrity,
            "foreign_key_violations": len(foreign_keys),
            "empty_referenced_tables": empty_referenced,
            "live_sidecars": sidecars,
            "tables": tables,
        },
        "duplicate_ids": duplicates,
        "canonical_counts": {
            "documents_db": tables.get("documents", 0),
            "sections_db": tables.get("sections", 0),
            "chunks_db": tables.get("chunks", 0),
            "sections_index_rows": sections_csv["rows"],
            "chunks_index_rows": chunks_csv["rows"],
            "eligible_chunks": chunks_csv["eligible"],
            "excluded_chunks": chunks_csv["excluded"],
            "other_action_chunks": chunks_csv["other_action"],
            "section_files_on_disk": section_files,
            "chunk_files_on_disk": chunk_files,
        },
        "mismatches": {
            "sections_index_vs_files": sections_csv["rows"] - section_files,
            "chunks_index_vs_files": chunks_csv["rows"] - chunk_files,
            "sections_db_vs_index": tables.get("sections", 0) - sections_csv["rows"],
            "chunks_db_vs_index": tables.get("chunks", 0) - chunks_csv["rows"],
        },
        "citation": citation,
    }

    failures = []
    if quick != "ok":
        failures.append(f"quick_check={quick}")
    if integrity != "ok":
        failures.append(f"integrity_check={integrity}")
    if sidecars:
        failures.append(f"live WAL/SHM present: {', '.join(sidecars)}")
    if foreign_keys:
        failures.append(f"{len(foreign_keys)} foreign key violation(s)")
    if empty_referenced and foreign_keys:
        failures.append("empty tables other tables reference: " + ", ".join(empty_referenced))
    failures += [f"duplicate ids in {k}" for k, v in duplicates.items() if v]
    failures += [f"{k}={v}" for k, v in audit["mismatches"].items() if v]
    # Every citation counter is a defect count, so any non-zero fails. The two
    # non-counters are excluded by name rather than by type: deep_verification
    # is a bool, and chunks_checked/slices_verified are the denominators that
    # make a zero mean "checked and clean" instead of "never looked".
    failures += [
        f"citation.{k}={v}"
        for k, v in citation.items()
        if k not in {"chunks_checked", "slices_verified", "deep_verification"} and v
    ]
    if citation["deep_verification"] and citation["slices_verified"] != citation["chunks_checked"]:
        failures.append(
            f"citation slices verified {citation['slices_verified']} of "
            f"{citation['chunks_checked']} chunks"
        )
    if chunks_csv["other_action"]:
        failures.append(f"{chunks_csv['other_action']} chunk(s) outside the two-way retrieval gate")
    audit["acceptance"] = {"pass": not failures, "failures": failures}

    args.out.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n": this lands in the package as DATABASE_AUDIT.json and is
    # read on Ubuntu; Path.write_text would emit CRLF when auditing on Windows.
    args.out.write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8", newline="\n")

    print(f"wrote {args.out}")
    print(f"  quick_check      : {quick}")
    print(f"  integrity_check  : {integrity}")
    print(f"  tables           : {len(tables)}")
    for name, count in tables.items():
        print(f"      {name:26s} {count:8d}")
    print(f"  sections  index/files : {sections_csv['rows']} / {section_files}")
    print(f"  chunks    index/files : {chunks_csv['rows']} / {chunk_files}")
    print(f"  eligible / excluded   : {chunks_csv['eligible']} / {chunks_csv['excluded']}")
    if citation["deep_verification"]:
        print(f"  citation slices exact : {citation['slices_verified']} / {citation['chunks_checked']}")
    else:
        print(f"  citation slices       : not re-derived (--skip-citation-verify)")
    if failures:
        print("\n  ACCEPTANCE FAILURES:")
        for item in failures:
            print(f"      {item}")
        return 1
    print("\n  acceptance: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
