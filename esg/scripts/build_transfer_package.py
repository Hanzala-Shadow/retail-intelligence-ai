r"""Build the auditable transfer archive for the Ubuntu server.

Follows the packaging guide's primary rule: package by an explicit allowlist,
never by archiving the repository and deleting afterwards. Nothing reaches
staging unless it is named below.

Written in Python rather than ported from the guide's PowerShell template so
the same script runs on the machine that builds the package and on the server
that re-verifies it -- the guide asks for cross-platform tooling elsewhere and
this is the one step that most needs it.

Deviations from the guide's file list, both deliberate:

  models/            The BGE tokenizer. esg_chunker refuses to run without it
                     ("Local BGE tokenizer directory. Required for
                     esg_chunk_v4"), so a package without it cannot rebuild a
                     single chunk. The guide's list omits it.
  extra reference    esg_file_catalog.csv, esg_ocr_approval.csv,
                     esg_drive_manifest.csv, companies.csv and the tracker.
                     Without the catalog the QA database loads with 140,027
                     foreign key violations, which is how this corpus reached
                     packaging once already.

Run
---
    venv/Scripts/python.exe esg/scripts/build_transfer_package.py
    venv/Scripts/python.exe esg/scripts/build_transfer_package.py --out-dir D:\ship
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)

import config  # noqa: E402

csv.field_size_limit(2**31 - 1)

PACKAGE_NAME = "esg_pipeline_v2"
DATASET_ID = "esg_docling_fusion_v2"
CHUNKER_VERSION = "esg_chunk_v4"

REQUIRED_TREES = [
    "common",
    "esg",
    "tests",
    "models",
    "data/02_interim/sustainability/03_pipeline_text",
    "data/03_sections/sustainability",
    "data/04_chunks/sustainability",
]

REQUIRED_FILES = [
    "data/00_reference/esg_parse_index.csv",
    "data/00_reference/esg_parse_index_v2.csv",
    "data/00_reference/esg_sections_index.csv",
    "data/00_reference/esg_chunks_index.csv",
    "data/00_reference/esg_file_catalog.csv",
    "data/00_reference/esg_ocr_approval.csv",
    "data/00_reference/esg_drive_manifest.csv",
    "data/00_reference/companies.csv",
    "data/00_reference/sustainability_report_tracker.csv",
    "data/00_reference/esg_accepted_company_manifest.csv",
    "requirements.txt",
    "requirements-docling.txt",
    "requirements-dev.txt",
    ".gitattributes",
]

# Never packaged, whatever the allowlist says. Checked again after staging is
# assembled, because an allowlist is only as good as the tree it copied.
FORBIDDEN_SUFFIXES = {".pdf", ".pem", ".key"}
FORBIDDEN_NAMES = {"credentials.json", "client_secret.json", "token.json", "token_rw.json"}
FORBIDDEN_PATTERNS = (".env", ".db-wal", ".db-shm", ".db-journal")

# The guide's acceptance table was written against a 682-document corpus. The
# current numbers are lower by one deleted document and higher by the Type3
# routing fix; recorded here so the server-side reviewer compares against the
# right values instead of failing four gates on a correct package.
GUIDE_BASELINE = {
    "documents": 682,
    "sections": 18707,
    "chunks": 50510,
    "eligible_chunks": 49734,
    "excluded_chunks": 776,
}

KNOWN_EXCEPTIONS = [
    {
        "id": "locally-minted-source-versions",
        "summary": (
            "Four identity IDs were minted on the packaging machine rather than issued "
            "upstream: source_version_id and extraction_artifact_id for "
            "BBY-BEST BUY CO INC-2016.pdf and COST-COSTCO WHOLESALE CORP-2020.pdf."
        ),
        "why": (
            "Drive replaced both files on 2026-08-07, after the catalog snapshot. The "
            "corpus holds the current bytes (confirmed against Drive by md5 on "
            "2026-08-12); the catalog described the superseded ones, so both documents "
            "carried no identity at all."
        ),
        "risk": (
            "If the upstream Drive system later issues its own IDs for the same bytes, "
            "the two will disagree and nothing will detect it. Resolve by replacing "
            "these four IDs with upstream's when available."
        ),
        "visible_in": "esg_file_catalog.csv review_reason on the affected rows",
    },
    {
        "id": "ges-duplicate-alias",
        "summary": (
            "The loader logs 'conflicting catalog rows' for logical source "
            "ls_199108d2ad0d75ec066ffbee and artifact ea_b8f079115d5fb4d3e26840cb."
        ),
        "why": (
            "GES-GUESS INC-2020-2021.pdf and GES-GUESS-2021-2020.pdf are one file "
            "stored in Drive under two names, identical sha256. One row is the "
            "canonical alias, the other carries duplicate_of_source_version_id."
        ),
        "risk": (
            "None. This is the file_alias layer modelling two names for one source "
            "version, which is what it exists for. Documented rather than fixed."
        ),
        "visible_in": "esg_file_catalog.csv canonical_alias / duplicate_of_source_version_id",
    },
]


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def git_commit(repo: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def git_is_dirty(repo: Path) -> bool | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return bool(out.stdout.strip()) if out.returncode == 0 else None


def ignore_junk(_dir: str, names: list[str]) -> set[str]:
    """Keep caches and compiled artifacts out of staging as the copy happens."""
    drop = set()
    for name in names:
        if name in {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git"}:
            drop.add(name)
        elif name.endswith((".pyc", ".pyo")):
            drop.add(name)
    return drop


def is_forbidden(path: Path) -> bool:
    name = path.name.lower()
    if path.suffix.lower() in FORBIDDEN_SUFFIXES:
        return True
    if name in FORBIDDEN_NAMES:
        return True
    return any(token in name for token in FORBIDDEN_PATTERNS)


def backup_database(source: Path, target: Path) -> dict:
    """Copy the database through SQLite's own backup API, then verify it.

    Never a file copy: a plain copy of a database with a writer attached is not
    a consistent snapshot, and the failure is silent until someone queries it.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    with sqlite3.connect(target) as conn:
        quick = conn.execute("PRAGMA quick_check").fetchone()[0]
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = len(list(conn.execute("PRAGMA foreign_key_check")))
    if quick != "ok" or integrity != "ok":
        raise SystemExit(f"staged database failed its pragmas: quick={quick} integrity={integrity}")
    return {
        "path": f"data/{target.name}",
        "role": "offline_qa_mirror",
        "sha256": sha256_file(target),
        "size_bytes": target.stat().st_size,
        "quick_check": quick,
        "integrity_check": integrity,
        "foreign_key_violations": foreign_keys,
    }


def count_rows(path: Path) -> int:
    with path.open(newline="", encoding="utf-8") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def gate_counts(stage: Path) -> dict:
    reference = stage / "data" / "00_reference"
    chunks = reference / "esg_chunks_index.csv"
    eligible = excluded = 0
    with chunks.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            action = (row.get("rag_action") or "").strip()
            if action == "index_as_esg":
                eligible += 1
            elif action == "exclude_from_esg_index":
                excluded += 1
    return {
        "documents": count_rows(reference / "esg_parse_index_v2.csv"),
        "sections": count_rows(reference / "esg_sections_index.csv"),
        "chunks": count_rows(chunks),
        "eligible_chunks": eligible,
        "excluded_chunks": excluded,
    }


def write_readme(stage: Path, counts: dict, commit: str | None) -> None:
    exceptions = "\n\n".join(
        f"### {item['id']}\n\n{item['summary']}\n\n"
        f"**Why:** {item['why']}\n\n**Risk:** {item['risk']}\n\n"
        f"**Visible in:** `{item['visible_in']}`"
        for item in KNOWN_EXCEPTIONS
    )
    deltas = "\n".join(
        f"| {name} | {GUIDE_BASELINE[name]:,} | {counts[name]:,} | {counts[name] - GUIDE_BASELINE[name]:+,} |"
        for name in GUIDE_BASELINE
    )
    (stage / "README.md").write_text(
        f"""# ESG pipeline transfer package

Dataset `{DATASET_ID}`, chunker `{CHUNKER_VERSION}`.
Source commit: `{commit or "not a git checkout"}`

Generated ESG corpus, the code that produced it, and an offline SQLite QA
mirror. **No source PDFs.** Embeddings, vector indexing, retrieval evaluation,
serving, API and UI are not included and are not in scope for this package.

## Canonical counts

| measure | value |
|---|---|
| documents | {counts['documents']:,} |
| sections | {counts['sections']:,} |
| chunks | {counts['chunks']:,} |
| eligible to index | {counts['eligible_chunks']:,} |
| excluded, retained for audit | {counts['excluded_chunks']:,} |

### Deviation from the acceptance guide

The guide's gate table was written against an earlier corpus. Compare against
the values above, not the guide's:

| measure | guide | this package | delta |
|---|---|---|---|
{deltas}

One document (`GROV-GROVE COLLABORATIVE HLDG INC-2018`) was withdrawn from
Drive and removed. Eight Type3-font documents that previously contributed no
indexable content now section correctly, which raises the chunk counts.

## Install

Ubuntu 24.04, Python 3.12 or 3.13. The code parses cleanly under 3.12; the
corpus was produced on 3.13.

```bash
python3 -m venv venv
./venv/bin/python -m pip install -r requirements.txt      # production
./venv/bin/python -m pip install -r requirements-dev.txt  # adds pytest
```

`requirements-docling.txt` is only needed to re-run stages 1-2, which require
a CUDA torch build. This package ships their output, so the server does not
need it.

## Run

```bash
esg/scripts/run_esg_pipeline.sh                  # stages 3-5 (Linux)
esg/scripts/run_esg_pipeline.sh --with-convert   # adds stages 1-2, needs docling
```

The PowerShell equivalent, `esg/scripts/run_docling_fusion_corpus.ps1`, is
retained for Windows.

## Validate, read-only

```bash
sha256sum -c PACKAGE_SHA256SUMS
./venv/bin/python esg/scripts/audit_esg_qa_db.py --out /tmp/audit.json
./venv/bin/python esg/scripts/summarise_fusion_run.py \\
    --parse-index data/00_reference/esg_parse_index_v2.csv \\
    --chunks-index data/00_reference/esg_chunks_index.csv
./venv/bin/python -m pytest -q
```

## Paths and configuration

`common/config.py` resolves every path from the repository root. Nothing
requires a drive letter, a named user profile or a Drive mount. Override with
the environment variables that file documents; do not hardcode absolute paths
into any index, database row or manifest.

## The SQLite database

`data/esg.db` is an **offline QA mirror** of the canonical CSV indexes. It is
not a production serving database and must not silently become one. Rebuild it
with `esg/scripts/build_esg_qa_db.py`, which refuses to overwrite an existing
file.

## Resume and cleanup

The pipeline caches by document and resumes: re-running skips completed work
unless `--force` is passed. `esg/scripts/prepare_clean_fusion_run.py` is
**destructive** -- it clears generated output. Read it before running it.

## Known exceptions

{exceptions}
""",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=config.REPO_ROOT)
    parser.add_argument("--out-dir", type=Path, default=None, help="where the .tar.gz lands")
    parser.add_argument("--stage-parent", type=Path, default=None, help="staging root (outside the repo)")
    parser.add_argument("--stamp", default=None, help="override the UTC timestamp in the archive name")
    args = parser.parse_args()

    repo = args.repo.resolve()
    now = datetime.now(timezone.utc)
    stamp = args.stamp or now.strftime("%Y%m%dT%H%M%SZ")
    out_dir = (args.out_dir or repo.parent).resolve()
    stage_parent = (args.stage_parent or Path(tempfile.gettempdir()) / f"esg-transfer-{stamp}").resolve()

    if stage_parent.is_relative_to(repo):
        raise SystemExit(f"staging must sit outside the repository: {stage_parent}")
    if stage_parent.exists():
        shutil.rmtree(stage_parent)
    stage = stage_parent / PACKAGE_NAME
    stage.mkdir(parents=True)

    commit = git_commit(repo)
    dirty = git_is_dirty(repo)
    print(f"staging  : {stage}")
    print(f"commit   : {commit or 'none'}{'  (WORKING TREE DIRTY)' if dirty else ''}")
    print()

    print("copying allowlisted trees")
    for relative in REQUIRED_TREES:
        source = repo / relative
        if not source.is_dir():
            raise SystemExit(f"required tree missing: {relative}")
        shutil.copytree(source, stage / relative, ignore=ignore_junk)
        print(f"    {relative}")

    print("copying allowlisted files")
    for relative in REQUIRED_FILES:
        source = repo / relative
        if not source.is_file():
            print(f"    SKIP (absent) {relative}")
            continue
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        print(f"    {relative}")

    print("\nbacking up the database through sqlite3.backup()")
    db_info = backup_database(config.ESG_DB, stage / "data" / "esg.db")
    print(f"    quick_check={db_info['quick_check']} integrity_check={db_info['integrity_check']} "
          f"fk_violations={db_info['foreign_key_violations']}")

    for evidence in ("TEST_RESULTS.txt", "DATABASE_AUDIT.json"):
        source = repo / "scratchpad" / evidence
        if source.is_file():
            shutil.copy2(source, stage / evidence)
            print(f"    evidence: {evidence}")
        else:
            print(f"    MISSING evidence: {evidence}", file=sys.stderr)

    counts = gate_counts(stage)
    write_readme(stage, counts, commit)

    metadata = {
        "package_id": f"esg-pipeline-v2-{stamp}",
        "dataset_id": DATASET_ID,
        "chunker_version": CHUNKER_VERSION,
        "created_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_commit": commit,
        "source_tree_dirty": dirty,
        "target_platform": "Ubuntu 24.04 / Python 3.12 or 3.13",
        "raw_pdfs_included": False,
        "counts": counts,
        "guide_baseline_counts": GUIDE_BASELINE,
        "count_deltas_vs_guide": {k: counts[k] - GUIDE_BASELINE[k] for k in GUIDE_BASELINE},
        "database": db_info,
        "known_exceptions": KNOWN_EXCEPTIONS,
    }
    (stage / "PACKAGE_METADATA.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    print("\nscanning staging for forbidden content")
    offenders = [p for p in stage.rglob("*") if p.is_file() and is_forbidden(p)]
    if offenders:
        for path in offenders[:20]:
            print(f"    FORBIDDEN {path.relative_to(stage)}", file=sys.stderr)
        raise SystemExit(f"{len(offenders)} forbidden file(s) in staging; nothing was archived")
    print("    clean: no PDFs, secrets, keys or live SQLite sidecars")

    print("hashing every packaged file")
    staged = sorted(p for p in stage.rglob("*") if p.is_file())
    rows = []
    sums = []
    for path in staged:
        relative = path.relative_to(stage).as_posix()
        if relative == "PACKAGE_SHA256SUMS":
            continue
        digest = sha256_file(path)
        rows.append({
            "relative_path": relative,
            "size_bytes": path.stat().st_size,
            "sha256": digest,
            "artifact_role": classify(relative),
            "required": "true",
        })
        sums.append(f"{digest}  {relative}")

    with (stage / "PACKAGE_MANIFEST.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["relative_path", "size_bytes", "sha256", "artifact_role", "required"])
        writer.writeheader()
        writer.writerows(rows)
    (stage / "PACKAGE_SHA256SUMS").write_text("\n".join(sorted(sums)) + "\n", encoding="utf-8")
    print(f"    {len(rows)} file(s) inventoried")

    archive = out_dir / f"{PACKAGE_NAME}_{stamp}.tar.gz"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"\nwriting {archive}")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(stage, arcname=PACKAGE_NAME)

    digest = sha256_file(archive)
    (archive.parent / f"{archive.name}.sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")

    print("verifying a clean extraction")
    with tempfile.TemporaryDirectory() as scratch:
        with tarfile.open(archive) as tar:
            roots = {Path(m.name).parts[0] for m in tar.getmembers()}
            if roots != {PACKAGE_NAME}:
                raise SystemExit(f"archive must have exactly one top-level directory, found {sorted(roots)}")
            tar.extractall(scratch, filter="data")
        extracted = Path(scratch) / PACKAGE_NAME
        bad = [line for line in (extracted / "PACKAGE_SHA256SUMS").read_text(encoding="utf-8").splitlines()
               if line and sha256_file(extracted / line.split("  ", 1)[1]) != line.split("  ", 1)[0]]
        if bad:
            raise SystemExit(f"{len(bad)} checksum mismatch(es) after extraction")
        strays = [p for p in extracted.rglob("*") if p.is_file() and is_forbidden(p)]
        if strays:
            raise SystemExit(f"{len(strays)} forbidden file(s) survived into the archive")

    print("    one top-level directory, every checksum verified, no forbidden files")
    print()
    print(f"archive  : {archive}")
    print(f"sha256   : {digest}")
    print(f"size     : {archive.stat().st_size / 1e6:.1f} MB")
    print(f"staging  : {stage}  (delete when the archive is accepted)")
    return 0


def classify(relative: str) -> str:
    if relative.startswith("data/00_reference/"):
        return "canonical_index"
    if relative.startswith("data/02_interim/"):
        return "bridge_text"
    if relative.startswith("data/03_sections/"):
        return "section_artifact"
    if relative.startswith("data/04_chunks/"):
        return "chunk_artifact"
    if relative == "data/esg.db":
        return "qa_database"
    if relative.startswith("models/"):
        return "tokenizer"
    if relative.startswith("tests/"):
        return "test"
    if relative.startswith(("common/", "esg/")):
        return "code"
    return "package_metadata"


if __name__ == "__main__":
    sys.exit(main())
