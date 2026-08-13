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
import platform
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
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
    # The test harness, not optional: the package ships tests/ and esg/tests/
    # and a README that tells the server to run pytest, and the guide gates on
    # the suite exiting zero. conftest.py is what puts common, esg/scripts,
    # esg/src and esg on sys.path, so without it collection aborts outright --
    # test_check_drive_csv_sync.py raises ModuleNotFoundError on
    # apply_drive_truth_sync, which is present in the package but unreachable,
    # and pytest stops before running anything.
    "conftest.py",
    "pytest.ini",
]

# Never packaged, whatever the allowlist says. Checked again after staging is
# assembled, because an allowlist is only as good as the tree it copied.
FORBIDDEN_SUFFIXES = {".pdf", ".pem", ".key"}
FORBIDDEN_NAMES = {"credentials.json", "client_secret.json", "token.json", "token_rw.json"}
FORBIDDEN_PATTERNS = (".env", ".db-wal", ".db-shm", ".db-journal")

# Mirrors the acceptance values stated in sections 2 and 10 of the transfer
# guide. Those figures describe the corpus as of 12 August 2026 and were
# deliberately left as written; the guide instead carries a CORPUS COUNT NOTE in
# its first block giving the real figures and why they moved -- GROV 2018
# reclassified out of scope (-1 document, -8 chunks) and eight Type3-font
# documents that now section correctly (+3 sections, +20 chunks, all eligible).
#
# So keep these numbers equal to the guide's, not to the corpus. The delta table
# they feed is that note in machine-checkable form: a reviewer reads the same
# comparison from the document and from the package, and neither can quietly
# drift from the other. Changing these to match the corpus would zero the deltas
# and hide exactly what the note exists to state.
#
# If the guide's own tables are ever corrected in place, update these to match
# them in the same change.
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


def write_lf(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write text with LF endings regardless of the packaging platform.

    ``Path.write_text`` translates ``\\n`` to ``os.linesep``, so building on
    Windows produced a CRLF ``PACKAGE_SHA256SUMS`` -- and ``sha256sum -c``, the
    first command this package's README tells the server to run, then appends
    the stray ``\\r`` to every filename and reports all 71,368 files as
    "FAILED open or read". The same defect hit the archive's own .sha256
    sidecar, which is the very first command in the guide's server-side
    verification. The hashes were right; only the line endings were wrong,
    which is the worst version of this bug because it looks like total
    corruption.

    The build's own extraction check missed it: it reads the sums file with
    ``read_text().splitlines()``, and ``splitlines`` strips ``\\r``. Python
    semantics passed where coreutils semantics fail, so nothing flagged it.

    PACKAGE_MANIFEST.csv is deliberately not routed through here -- RFC 4180
    specifies CRLF and every CSV reader handles it.
    """
    path.write_text(text, encoding=encoding, newline="\n")


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


def run_tests(repo: Path, stage: Path, commit: str | None, dirty: bool | None) -> tuple[int, str]:
    """Run the suite now and write TEST_RESULTS.txt, rather than copying one.

    The build used to copy scratchpad/TEST_RESULTS.txt, whatever it happened to
    contain. In the 2026-08-12 package that file recorded commit fc157611 with
    12 uncommitted paths -- four commits behind the tree being packaged, and
    predating both the .PDF casing fix and the portability test it claimed to
    have run. Evidence copied from elsewhere is evidence about somewhere else.

    Running it here makes the results and the package the same tree by
    construction, so they cannot drift apart again.
    """
    command = [sys.executable, "-m", "pytest", "-q"]
    started = datetime.now(timezone.utc)
    proc = subprocess.run(command, cwd=repo, capture_output=True, text=True)
    output = (proc.stdout or "") + (proc.stderr or "")
    summary = "\n".join(line for line in output.splitlines() if line.strip())[-4000:]

    installed = []
    for name in ("pytest", "numpy", "SQLAlchemy", "tiktoken", "transformers", "python-dotenv"):
        try:
            installed.append(f"{name}=={version(name)}")
        except PackageNotFoundError:
            installed.append(f"{name}==<not installed>")

    write_lf(stage / "TEST_RESULTS.txt", f"""ESG pipeline test run

command      : {" ".join(command)}
interpreter  : {sys.version}
platform     : {platform.platform()}
dependencies : requirements-dev.txt (requirements.txt + pytest)
timestamp_utc: {started.strftime("%Y-%m-%dT%H:%M:%SZ")}
git commit   : {commit or "not a git checkout"}
git dirty    : {"yes" if dirty else "no" if dirty is not None else "unknown"}

--- installed ---
{chr(10).join(installed)}

--- output ---
{summary}

exit status  : {proc.returncode}
""")
    return proc.returncode, summary.splitlines()[-1] if summary else ""


def run_database_audit(repo: Path, stage: Path) -> None:
    """Audit the staged database, into staging, as part of the build.

    Same defect as the test results and the same fix. The 2026-08-12 package
    copied a DATABASE_AUDIT.json generated ten minutes before the build, and
    the database was rebuilt in between: the audit recorded sha256 fa4f5322...
    while the database it shipped alongside hashes e5f029f8..., so the one
    artifact whose job is to vouch for that file described a different one.

    Everything is pointed at staging, which is also what the guide asks for --
    acceptance checks run against the staged package, not the repository.
    """
    data = stage / "data"
    command = [
        sys.executable, str(repo / "esg" / "scripts" / "audit_esg_qa_db.py"),
        "--db", str(data / "esg.db"),
        "--out", str(stage / "DATABASE_AUDIT.json"),
        "--sections-index", str(data / "00_reference" / "esg_sections_index.csv"),
        "--chunks-index", str(data / "00_reference" / "esg_chunks_index.csv"),
        "--parse-index", str(data / "00_reference" / "esg_parse_index_v2.csv"),
        "--sections-dir", str(data / "03_sections" / "sustainability"),
        "--chunks-dir", str(data / "04_chunks" / "sustainability"),
        "--root", str(stage),
    ]
    proc = subprocess.run(command, cwd=repo, capture_output=True, text=True)
    for line in (proc.stdout or "").splitlines():
        print(f"    {line}")
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr or "")
        raise SystemExit(
            "the staged database failed its audit; nothing was archived. "
            "Read DATABASE_AUDIT.json in staging for the acceptance failures."
        )


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
    write_lf(
        stage / "README.md",
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

One document (`GROV-GROVE COLLABORATIVE HLDG INC-2018`) was dropped from these
indexes. It was **not** deleted from Drive: on 2026-08-11 it was moved out of
the corpus folder `_ESG Reports/GROV/` into `Other ESG Sustainability Related
Reports/`, the holding area for out-of-scope documents, and a second copy
remains under `Archive/Sustainability Reports New/`. Both match the local file
by md5 (`97acf3b6e7bf6379678f69d74c57afbd`), verified against live Drive on
2026-08-12. The document was reclassified as out of scope, so its 8 chunks were
removed from the corpus; the bytes and their provenance are still in Drive.

Eight Type3-font documents that previously contributed no indexable content now
section correctly, which raises the chunk counts.

## What is in here

| path | holds |
|---|---|
| `common/`, `esg/config.py` | path and environment resolution, shared by every stage |
| `esg/src/` | the pipeline modules: `section_splitter_esg.py`, `esg_chunker.py`, and the TOC and year helpers they use |
| `esg/scripts/` | stage runners, the Ubuntu and Windows orchestrators, and the audit, sync and packaging tools |
| `esg/docs/` | stage-by-stage reference, data layout and runbooks |
| `tests/`, `esg/tests/` | the suite; `conftest.py` and `pytest.ini` are what make it importable |
| `models/bge-base-en-v1.5-tokenizer/` | the exact tokenizer chunk token counts depend on -- the chunker refuses to run without it |
| `data/00_reference/` | the canonical indexes and the reference CSVs they join to |
| `data/02_interim/sustainability/03_pipeline_text/` | bridge output: one text file, page map and heading list per document |
| `data/03_sections/sustainability/` | one file per topic section |
| `data/04_chunks/sustainability/` | one file per chunk |
| `data/esg.db` | the offline SQLite QA mirror |

The canonical data paths, which every stage defaults to:

```
data/00_reference/esg_parse_index.csv        source identity and lineage (input)
data/00_reference/esg_parse_index_v2.csv     per-document parse record (stage 3)
data/00_reference/esg_sections_index.csv     section index (stage 4)
data/00_reference/esg_chunks_index.csv       chunk index (stage 5) -- the deliverable
```

`esg_chunks_index.csv` is the artifact everything else exists to produce. Each
row carries its own `embedding_text`, so downstream retrieval needs that file
alone; the chunk and section files are what make a citation auditable back to
the source document.

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

**On line endings, before you report it as a defect.**
`PACKAGE_MANIFEST.csv` is CRLF; every other generated artifact here --
`PACKAGE_SHA256SUMS`, the archive's `.sha256` sidecar, this README,
`PACKAGE_METADATA.json`, `DATABASE_AUDIT.json`, `TEST_RESULTS.txt` -- is LF.
That difference is deliberate and both halves matter.

The manifest is a CSV, and RFC 4180 specifies CRLF as the line terminator, so
every CSV reader expects and strips it. The checksum files are parsed by
`sha256sum`, which takes everything after the two spaces as a filename and has
no notion of the CSV spec: a CRLF there makes it look for `README.md\\r`, so
every entry reports "FAILED open or read" while the hashes underneath are
perfectly correct. That failure is indistinguishable from a corrupt archive,
which is why the two file types are written differently on purpose.

Two reviewers have flagged the manifest as an inconsistency before reading
this paragraph. It is not one. See `write_lf` in
`esg/scripts/build_transfer_package.py` for the same explanation in the code.

## Paths and configuration

`common/config.py` resolves every path from the package root, which it takes
from its own location on disk. Nothing requires a drive letter, a named user
profile or a Drive mount, so this runs wherever it is unpacked.

**There is no environment variable that relocates the project or the data
directory.** `data/` is always `<package root>/data`; to put the corpus
somewhere else, move the whole package. The transfer guide suggests
`RETAIL_INTELLIGENCE_ROOT` and `ESG_DATA_ROOT` overrides and this build does
not implement them -- stated here rather than left to be discovered, so nobody
sets them and expects an effect.

The variables `common/config.py` does read are all optional, and **none are
needed for stages 3-5**:

| variable | used by |
|---|---|
| `DB_URL` | SQLAlchemy connection string, legacy loader only |
| `DRIVE_ROOT_FOLDER_ID` | Google Drive folder id for the source audit |
| `GOOGLE_DRIVE_CLIENT_SECRET` | OAuth client secret path |
| `GOOGLE_DRIVE_CREDENTIALS_PATH` | OAuth token path |
| `SEC_USER_AGENT` | SEC request header, 10-K path only |

Drive access is an acquisition and audit step, never an import-time
requirement: the pipeline runs with none of these set.

On the command line, the two stages that write the corpus take explicit paths,
all defaulting to the locations above:

```
section_splitter_esg.py  --input --out --index --ticker --force
esg_chunker.py           --input --out --index --sections-index
                         --parse-index --ticker
```

Pass `--help` to either for the current list. Do not hardcode absolute paths
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
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=config.REPO_ROOT)
    parser.add_argument("--out-dir", type=Path, default=None, help="where the .tar.gz lands")
    parser.add_argument("--stage-parent", type=Path, default=None, help="staging root (outside the repo)")
    parser.add_argument("--stamp", default=None, help="override the UTC timestamp in the archive name")
    parser.add_argument(
        "--allow-test-failures", action="store_true",
        help="package even though the suite failed; the failure is recorded in "
             "TEST_RESULTS.txt either way and must be declared at sign-off",
    )
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

    print("\nauditing the staged database")
    run_database_audit(repo, stage)

    print("\nrunning the test suite against the tree being packaged")
    code, last = run_tests(repo, stage, commit, dirty)
    print(f"    {last}")
    if code != 0 and not args.allow_test_failures:
        raise SystemExit(
            f"pytest exited {code}; nothing was archived. The guide gates on the "
            f"current suite exiting zero. Read TEST_RESULTS.txt in staging, or "
            f"pass --allow-test-failures to package anyway and declare it."
        )
    if code != 0:
        print("    WARNING: packaging a tree with failing tests (--allow-test-failures)",
              file=sys.stderr)

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
    write_lf(stage / "PACKAGE_METADATA.json", json.dumps(metadata, indent=2) + "\n")

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

    manifest = stage / "PACKAGE_MANIFEST.csv"
    with manifest.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["relative_path", "size_bytes", "sha256", "artifact_role", "required"])
        writer.writeheader()
        writer.writerows(rows)

    # The guide allows exactly one file to be absent from PACKAGE_SHA256SUMS:
    # PACKAGE_SHA256SUMS itself. The manifest was a second, only because it is
    # written after the hashing pass and so did not exist when staging was
    # walked -- it cannot list its own digest, but it can be listed. Hash it
    # here, once written, and fold it in before the sums file is sorted.
    sums.append(f"{sha256_file(manifest)}  {manifest.name}")
    write_lf(stage / "PACKAGE_SHA256SUMS", "\n".join(sorted(sums)) + "\n")
    print(f"    {len(rows)} file(s) inventoried")

    archive = out_dir / f"{PACKAGE_NAME}_{stamp}.tar.gz"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"\nwriting {archive}")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(stage, arcname=PACKAGE_NAME)

    digest = sha256_file(archive)
    write_lf(archive.parent / f"{archive.name}.sha256", f"{digest}  {archive.name}\n", encoding="ascii")

    print("verifying a clean extraction")
    with tempfile.TemporaryDirectory() as scratch:
        with tarfile.open(archive) as tar:
            roots = {Path(m.name).parts[0] for m in tar.getmembers()}
            if roots != {PACKAGE_NAME}:
                raise SystemExit(f"archive must have exactly one top-level directory, found {sorted(roots)}")
            tar.extractall(scratch, filter="data")
        extracted = Path(scratch) / PACKAGE_NAME
        # Read as bytes and split on b"\n" rather than read_text().splitlines():
        # splitlines() strips a trailing \r, which is precisely how a CRLF sums
        # file passed this check and then failed every line under sha256sum -c
        # on the server. Verify the way the server will.
        raw = (extracted / "PACKAGE_SHA256SUMS").read_bytes()
        if b"\r" in raw:
            raise SystemExit(
                "PACKAGE_SHA256SUMS contains CR bytes; sha256sum -c would append "
                "the stray \\r to every filename and fail to open all of them"
            )
        entries = [line.decode("utf-8") for line in raw.split(b"\n") if line]
        bad = [line for line in entries
               if sha256_file(extracted / line.split("  ", 1)[1]) != line.split("  ", 1)[0]]
        listed = {line.split("  ", 1)[1] for line in entries}
        present = {p.relative_to(extracted).as_posix() for p in extracted.rglob("*") if p.is_file()}
        unlisted = present - listed - {"PACKAGE_SHA256SUMS"}
        if unlisted:
            raise SystemExit(
                f"{len(unlisted)} extracted file(s) absent from PACKAGE_SHA256SUMS, "
                f"e.g. {sorted(unlisted)[:3]}"
            )
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
