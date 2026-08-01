"""common/config.py — layout and environment shared by both pipelines.

This is the shared half of what used to be one ``src/config.py``. It owns
everything neither pipeline can claim: the repo root, the ``data/`` stage
directories both pipelines write into, the company list, the database and
Drive environment variables, and the machinery the per-pipeline configs and
the shell runners are built on.

The pipeline-specific constants live beside the code that uses them:

    esg/config.py       ESG_* — sustainability PDFs, sections, chunks, QA
    filings/config.py   10-K — SEC filings, HTML text, tables, chunk index

Both re-export everything here, so a module inside either pipeline writes
``import config`` and gets the shared constants and its own.

    data/00_reference   reference + index CSVs shared across stages
    data/01_raw         downloaded source documents
    data/02_interim     parsed text
    data/03_sections    sectioned text
    data/04_chunks      chunked text
    data/04_vlm         VLM stage artifacts
    data/05_db          database migrations
    data/05_embedding   embedding-ready payloads

``data/`` itself is unchanged by the pipeline split: both pipelines keep
writing into the same stage directories, and the database stays single.
"""

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Load .env once, here, for the whole pipeline
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

# ---------------------------------------------------------------------------
# Top-level roots
# ---------------------------------------------------------------------------
DATA_DIR = REPO_ROOT / "data"
LOGS_DIR = REPO_ROOT / "logs"
REPORTS_DIR = REPO_ROOT / "reports"

# ---------------------------------------------------------------------------
# Stage directories — shared scaffolding. Both pipelines derive their own
# subdirectories from these, so they belong to neither.
# ---------------------------------------------------------------------------
REFERENCE_DIR = DATA_DIR / "00_reference"
RAW_DIR = DATA_DIR / "01_raw"
INTERIM_DIR = DATA_DIR / "02_interim"
SECTIONS_DIR = DATA_DIR / "03_sections"
CHUNKS_DIR = DATA_DIR / "04_chunks"
VLM_DIR = DATA_DIR / "04_vlm"
DB_OUTPUT_DIR = DATA_DIR / "05_db"
EMBEDDING_DIR = DATA_DIR / "05_embedding"
TABLES_DIR = DATA_DIR / "tables"

MIGRATIONS_DIR = DB_OUTPUT_DIR / "migrations"

# ---------------------------------------------------------------------------
# Shared reference data
# ---------------------------------------------------------------------------
COMPANIES_CSV = REFERENCE_DIR / "companies.csv"

# ---------------------------------------------------------------------------
# Repo-relative forms
#
# Values persisted to the database (e.g. documents.filepath) must stay
# relative to the repo root so a row stays meaningful on another machine.
# Derive them here rather than re-hardcoding the literal at the call site.
# ---------------------------------------------------------------------------
RAW_DIR_REL = RAW_DIR.relative_to(REPO_ROOT)


def as_repo_relative(path: str | os.PathLike[str]) -> Path:
    """Return ``path`` relative to REPO_ROOT, for values stored in the DB.

    Falls back to the path unchanged when it is already relative or points
    outside the repository.
    """
    candidate = Path(path)
    if not candidate.is_absolute():
        return candidate
    try:
        return candidate.relative_to(REPO_ROOT)
    except ValueError:
        return candidate


# ---------------------------------------------------------------------------
# Ensure the shared stage directories exist (idempotent, safe to call
# repeatedly). Each pipeline config creates its own raw/output roots.
# ---------------------------------------------------------------------------
for _d in [REFERENCE_DIR, INTERIM_DIR, SECTIONS_DIR, CHUNKS_DIR,
           DB_OUTPUT_DIR, LOGS_DIR]:
    os.makedirs(_d, exist_ok=True)

# ---------------------------------------------------------------------------
# Environment variables (single source of truth — no other script should
# call os.getenv() for these; import the constant from here instead)
# ---------------------------------------------------------------------------
DB_URL = os.getenv("DB_URL")
GOOGLE_DRIVE_CREDENTIALS_PATH = os.getenv("GOOGLE_DRIVE_CREDENTIALS_PATH")
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT")
DRIVE_ROOT_FOLDER_ID = os.getenv("DRIVE_ROOT_FOLDER_ID")
GOOGLE_DRIVE_CLIENT_SECRET = os.getenv("GOOGLE_DRIVE_CLIENT_SECRET")

# ---------------------------------------------------------------------------
# Fail loudly and early if critical vars are missing, rather than letting
# a downstream script crash confusingly mid-pipeline
# ---------------------------------------------------------------------------
_REQUIRED = {
    "DB_URL": DB_URL,
    "SEC_USER_AGENT": SEC_USER_AGENT,
    "GOOGLE_DRIVE_CREDENTIALS_PATH": GOOGLE_DRIVE_CREDENTIALS_PATH,
}


def validate_config():
    missing = [k for k, v in _REQUIRED.items() if not v]
    if missing:
        raise EnvironmentError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            f"Check .env at {REPO_ROOT / '.env'}"
        )


# ---------------------------------------------------------------------------
# The JSON bridge for the shell and PowerShell runners
#
# Splitting config into three files put this at risk: path_constants() walks a
# module namespace, so a runner reading only one of the three gets a table
# missing the other two thirds — and a missing key is an empty argument, which
# fails deep inside a corpus run rather than at startup. Two things prevent
# that. merged_path_constants() unions all three namespaces, and it is what
# `python common/config.py --json` prints, so the runners see exactly the
# table they saw before the split. tests/test_config_single_source_of_truth.py
# pins that key set and checks every runner lookup against it.
# ---------------------------------------------------------------------------
PIPELINE_CONFIGS = ("esg", "filings")


def path_constants(namespace: dict | None = None) -> dict[str, dict[str, str]]:
    """Every path constant in ``namespace``, absolute and repo-relative.

    Defaults to this module's own namespace — the shared constants only. A
    pipeline config passes its own ``globals()``, which holds the shared
    constants too because it re-exports them. Relative forms use forward
    slashes and are what the runners pass to the Python stages.
    """
    ns = globals() if namespace is None else namespace
    absolute, relative = {}, {}
    for name, value in sorted(ns.items()):
        if name.startswith("_") or not isinstance(value, Path):
            continue
        absolute[name] = str(value)
        relative[name] = as_repo_relative(value).as_posix()
    return {"absolute": absolute, "relative": relative}


def _load_pipeline_config(name: str):
    """Import ``<name>/config.py`` by file path, under a private module name.

    By path rather than by import so this module never needs the pipeline
    directories on ``sys.path``, and under a private name so it cannot clobber
    whichever ``config`` the calling process already imported.
    """
    import importlib.util

    path = REPO_ROOT / name / "config.py"
    if not path.is_file():
        raise FileNotFoundError(
            f"Pipeline config missing: {path}. The runners read the merged "
            f"path table from here, so a missing config silently shrinks it."
        )
    spec = importlib.util.spec_from_file_location(f"_{name}_config", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def merged_path_constants() -> dict[str, dict[str, str]]:
    """The union of shared + every pipeline's path constants.

    This is what the runners read. They span both pipelines — the ESG
    PowerShell runners want ESG paths, ``scripts_project_snapshot.sh`` counts
    files under both — and none of them should have to know which config owns
    a given name.
    """
    merged = path_constants()
    for name in PIPELINE_CONFIGS:
        pipeline = path_constants(vars(_load_pipeline_config(name)))
        merged["absolute"].update(pipeline["absolute"])
        merged["relative"].update(pipeline["relative"])
    return {
        "absolute": dict(sorted(merged["absolute"].items())),
        "relative": dict(sorted(merged["relative"].items())),
    }


def main(namespace: dict, merged: bool = False) -> None:
    """Shared ``__main__`` body for all three config modules."""
    if "--json" in sys.argv:
        # --json intentionally skips validate_config(): a runner asking where
        # a directory lives should not need SEC/Drive credentials to answer.
        payload = merged_path_constants() if merged else path_constants(namespace)
        print(json.dumps(payload, indent=2))
        raise SystemExit(0)

    validate_config()
    print("config OK")
    print(f"REPO_ROOT: {REPO_ROOT}")
    print(f"DATA_DIR:  {DATA_DIR}")
    for k, v in _REQUIRED.items():
        print(f"{k}: {'set' if v else 'MISSING'}")


if __name__ == "__main__":
    # The merged table by default: this file is the one the runners point at.
    main(globals(), merged=True)
