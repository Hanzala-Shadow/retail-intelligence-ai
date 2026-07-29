"""
config.py — Centralized path and environment configuration for the
Retail Intelligence Pipeline. No script should hardcode a path or call
os.getenv() directly for these values; import from here instead.

All path constants are absolute ``Path`` objects anchored on ``REPO_ROOT``,
so they resolve identically no matter which directory a script is invoked
from. Call sites that need a string (argparse defaults, pandas readers that
were passed strings before) should wrap with ``str(...)``.

Layout mirrors the pipeline stages:

    data/00_reference   reference + index CSVs shared across stages
    data/01_raw         downloaded source documents
    data/02_interim     parsed text
    data/03_sections    sectioned text
    data/04_chunks      chunked text
    data/04_vlm         VLM stage artifacts
    data/05_db          database migrations
    data/05_embedding   embedding-ready payloads

Constants are grouped as SHARED / 10-K / ESG. That grouping is deliberate:
the two pipelines are slated to move into separate top-level directories,
and the ``ESG_``-prefixed names are the ones that travel with the ESG code.
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
# Stage directories
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
# SHARED reference data
# ---------------------------------------------------------------------------
COMPANIES_CSV = REFERENCE_DIR / "companies.csv"

# ---------------------------------------------------------------------------
# 10-K pipeline
# ---------------------------------------------------------------------------
RAW_10K_DIR = RAW_DIR / "10k"
HTML_TEXT_DIR = INTERIM_DIR / "html_text"
SECTIONS_10K_DIR = SECTIONS_DIR / "10k"
CHUNKS_10K_DIR = CHUNKS_DIR / "10k"
HTML_TABLE_DIR = TABLES_DIR / "html_table"
PDF_TABLE_DIR = TABLES_DIR / "pdf_table"

FILINGS_CSV = REFERENCE_DIR / "filings.csv"
FILING_STATE_CSV = REFERENCE_DIR / "filing_state.csv"
SECTIONS_INDEX_CSV = REFERENCE_DIR / "sections_index.csv"
CHUNKS_INDEX_CSV = REFERENCE_DIR / "chunks_index.csv"
DOCUMENT_SCAN_CSV = REFERENCE_DIR / "document_scan.csv"
DOWNLOAD_STATUS_REPORT_CSV = REFERENCE_DIR / "download_status_report.csv"
CHUNK_QA_REPORT_CSV = REFERENCE_DIR / "chunk_qa_report.csv"
CHUNK_QA_COMPANY_SUMMARY_CSV = REFERENCE_DIR / "chunk_qa_company_summary.csv"
STATS_REPORT_CSV = REFERENCE_DIR / "stats_report.csv"
TABLES_INDEX_CSV = TABLES_DIR / "tables_index.csv"

CHUNKABLE_10K_SECTIONS_FINAL_TXT = REPORTS_DIR / "chunkable_10k_sections_final.txt"
CHUNKABLE_10K_SECTIONS_TXT = REPORTS_DIR / "chunkable_10k_sections.txt"
FALLBACK_10K_SECTIONS_TXT = REPORTS_DIR / "fallback_10k_sections.txt"
FALLBACK_10K_FILES_TXT = REPORTS_DIR / "fallback_10k_files.txt"

# ---------------------------------------------------------------------------
# ESG pipeline — directories
# ---------------------------------------------------------------------------
RAW_SUSTAINABILITY_DIR = RAW_DIR / "sustainability"
# "Other Sustainability Related Reports" -- a separate source class that the
# Python intake deliberately does NOT ingest (see esg_intake_catalog.py and
# commit 3dbe613f8). Defined here only so the one runner that still scans it
# stops spelling the path out; that runner's scope is a separate question.
RAW_SUSTAINABILITY_OTHER_DIR = RAW_DIR / "sustainability_other"
ESG_TEXT_DIR = INTERIM_DIR / "esg_text"
OCR_STAGING_DIR = INTERIM_DIR / "ocr_staging"
ESG_SECTIONS_DIR = SECTIONS_DIR / "esg"
ESG_CHUNKS_DIR = CHUNKS_DIR / "esg"
ESG_EMBEDDING_DIR = EMBEDDING_DIR / "esg"
ESG_EMBEDDING_CTX_DIR = EMBEDDING_DIR / "esg_ctx"

# ---------------------------------------------------------------------------
# ESG pipeline — reference/index CSVs
# ---------------------------------------------------------------------------
SUSTAINABILITY_TRACKER_CSV = REFERENCE_DIR / "sustainability_report_tracker.csv"
ESG_DRIVE_MANIFEST_CSV = REFERENCE_DIR / "esg_drive_manifest.csv"
ESG_FILE_CATALOG_CSV = REFERENCE_DIR / "esg_file_catalog.csv"
ESG_PARSE_INDEX_CSV = REFERENCE_DIR / "esg_parse_index.csv"
ESG_SECTIONS_INDEX_CSV = REFERENCE_DIR / "esg_sections_index.csv"
ESG_CHUNKS_INDEX_CSV = REFERENCE_DIR / "esg_chunks_index.csv"
ESG_CHUNKS_INDEX_ENRICHED_CSV = REFERENCE_DIR / "esg_chunks_index_enriched.csv"
ESG_SOURCE_REGISTRY_CSV = REFERENCE_DIR / "esg_source_registry.csv"
ESG_PAGE_LAYOUT_QA_CSV = REFERENCE_DIR / "esg_page_layout_qa.csv"
ESG_PIPELINE_QA_CSV = REFERENCE_DIR / "esg_pipeline_qa.csv"
ESG_OCR_APPROVAL_CSV = REFERENCE_DIR / "esg_ocr_approval.csv"
ESG_PARSER_OVERRIDES_CSV = REFERENCE_DIR / "esg_parser_overrides.csv"
ESG_PAGE_OCR_OVERRIDES_CSV = REFERENCE_DIR / "esg_page_ocr_overrides.csv"
ESG_CHUNK_HISTORY_CSV = REFERENCE_DIR / "esg_chunk_history.csv"
ESG_ACCEPTED_COMPANY_MANIFEST_CSV = REFERENCE_DIR / "esg_accepted_company_manifest.csv"
ESG_CHUNK_EMBEDDING_CONTEXT_CSV = REFERENCE_DIR / "esg_chunk_embedding_context.csv"
ESG_LINES_AREA_SIGNALS_CSV = REFERENCE_DIR / "esg_lines_area_signals.csv"
BANNED_COMPANIES_CSV = REFERENCE_DIR / "banned_companies.csv"
VECTOR_INDEX_MANIFEST_CSV = REFERENCE_DIR / "vector_index_manifest.csv"
# Consumed by the sampling runners. The file is not currently generated.
ESG_SAMPLE_DOCS_CSV = REFERENCE_DIR / "esg_sample_docs.csv"

# ---------------------------------------------------------------------------
# ESG pipeline — local SQLite packages and reports
# ---------------------------------------------------------------------------
ESG_DB = DATA_DIR / "esg.db"
ESG_BROWSE_DB = DATA_DIR / "esg_browse.db"

ESG_STEM_REMAP_AUDIT_CSV = REPORTS_DIR / "esg_stem_remap_audit.csv"
ESG_P1_ENRICHMENT_QA_CSV = REPORTS_DIR / "esg_p1_enrichment_qa.csv"
ESG_P1_ENRICHMENT_SUMMARY_MD = REPORTS_DIR / "esg_p1_enrichment_summary.md"
ESG_EMBEDDING_CONTEXT_SUMMARY_MD = REPORTS_DIR / "esg_embedding_context_summary.md"
ESG_CONTRACT_CONFORMANCE_MD = REPORTS_DIR / "esg_contract_conformance.md"
ESG_DRIVE_YEAR_COVERAGE_DIR = REPORTS_DIR / "esg_drive_year_coverage"

# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------
DOWNLOAD_ERRORS_LOG = LOGS_DIR / "download_errors.log"

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
# Ensure the stage directories exist (idempotent, safe to call repeatedly).
# Only the stable stage roots are created here; individual scripts still
# create their own per-run output subdirectories.
# ---------------------------------------------------------------------------
for _d in [REFERENCE_DIR, RAW_10K_DIR, RAW_SUSTAINABILITY_DIR, INTERIM_DIR,
           SECTIONS_DIR, CHUNKS_DIR, DB_OUTPUT_DIR, LOGS_DIR]:
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

def path_constants() -> dict[str, dict[str, str]]:
    """Every path constant, absolute and repo-relative.

    This is the bridge for the PowerShell/bash runners, which cannot import
    this module. They read `python src/config.py --json` instead of spelling
    the layout out a second time. Relative forms use forward slashes and are
    what the runners pass to the Python stages, matching how those paths were
    written before this became the single source of truth.
    """
    absolute, relative = {}, {}
    for name, value in sorted(globals().items()):
        if name.startswith("_") or not isinstance(value, Path):
            continue
        absolute[name] = str(value)
        relative[name] = as_repo_relative(value).as_posix()
    return {"absolute": absolute, "relative": relative}


if __name__ == "__main__":
    # --json intentionally skips validate_config(): a runner asking where a
    # directory lives should not need SEC/Drive credentials to get an answer.
    if "--json" in sys.argv:
        print(json.dumps(path_constants(), indent=2))
        raise SystemExit(0)

    validate_config()
    print("config OK")
    print(f"REPO_ROOT: {REPO_ROOT}")
    print(f"DATA_DIR:  {DATA_DIR}")
    for k, v in _REQUIRED.items():
        print(f"{k}: {'set' if v else 'MISSING'}")
