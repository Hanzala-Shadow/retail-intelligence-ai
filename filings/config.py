"""filings/config.py — paths owned by the 10-K / SEC filings pipeline.

Imported as bare ``config`` by everything under ``filings/``, exactly as
``src/config.py`` was before the pipelines were split. It re-exports
``common.config`` wholesale, so a module that writes ``import config`` still
sees ``REPO_ROOT``, ``REFERENCE_DIR``, ``COMPANIES_CSV`` and the rest
alongside the 10-K constants below.

The ``data/`` layout did not change with the split: these are the same
directories and CSVs, under the same names, that the 10-K stages already read
and write. Only the file defining them moved.
"""

import os
import sys
from pathlib import Path

# `common` is a package at the repo root; the pipeline configs are bare
# modules named `config`. Reaching common by package name is what keeps those
# two from colliding on sys.path.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from common.config import *  # noqa: F401,F403  (re-export the shared layout)
from common.config import (  # noqa: F401  (names star-import would still give, pinned for readers)
    CHUNKS_DIR,
    INTERIM_DIR,
    LOGS_DIR,
    RAW_DIR,
    REPORTS_DIR,
    SECTIONS_DIR,
    TABLES_DIR,
    as_repo_relative,
    main as _main,
)

# ---------------------------------------------------------------------------
# Directories
# ---------------------------------------------------------------------------
RAW_10K_DIR = RAW_DIR / "10k"
HTML_TEXT_DIR = INTERIM_DIR / "html_text"
SECTIONS_10K_DIR = SECTIONS_DIR / "10k"
CHUNKS_10K_DIR = CHUNKS_DIR / "10k"
HTML_TABLE_DIR = TABLES_DIR / "html_table"
PDF_TABLE_DIR = TABLES_DIR / "pdf_table"

# ---------------------------------------------------------------------------
# Reference/index CSVs
# ---------------------------------------------------------------------------
FILINGS_CSV = REFERENCE_DIR / "filings.csv"  # noqa: F405
FILING_STATE_CSV = REFERENCE_DIR / "filing_state.csv"  # noqa: F405
SECTIONS_INDEX_CSV = REFERENCE_DIR / "sections_index.csv"  # noqa: F405
CHUNKS_INDEX_CSV = REFERENCE_DIR / "chunks_index.csv"  # noqa: F405
DOCUMENT_SCAN_CSV = REFERENCE_DIR / "document_scan.csv"  # noqa: F405
DOWNLOAD_STATUS_REPORT_CSV = REFERENCE_DIR / "download_status_report.csv"  # noqa: F405
CHUNK_QA_REPORT_CSV = REFERENCE_DIR / "chunk_qa_report.csv"  # noqa: F405
CHUNK_QA_COMPANY_SUMMARY_CSV = REFERENCE_DIR / "chunk_qa_company_summary.csv"  # noqa: F405
STATS_REPORT_CSV = REFERENCE_DIR / "stats_report.csv"  # noqa: F405
TABLES_INDEX_CSV = TABLES_DIR / "tables_index.csv"

# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
CHUNKABLE_10K_SECTIONS_FINAL_TXT = REPORTS_DIR / "chunkable_10k_sections_final.txt"
CHUNKABLE_10K_SECTIONS_TXT = REPORTS_DIR / "chunkable_10k_sections.txt"
FALLBACK_10K_SECTIONS_TXT = REPORTS_DIR / "fallback_10k_sections.txt"
FALLBACK_10K_FILES_TXT = REPORTS_DIR / "fallback_10k_files.txt"

# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------
DOWNLOAD_ERRORS_LOG = LOGS_DIR / "download_errors.log"

# ---------------------------------------------------------------------------
# This pipeline's own raw root (the shared stage roots are created by
# common/config.py).
# ---------------------------------------------------------------------------
os.makedirs(RAW_10K_DIR, exist_ok=True)


if __name__ == "__main__":
    # Shared + 10-K. The runners read the merged table from common/config.py;
    # this is the scoped view, for asking what the filings pipeline alone owns.
    _main(globals())
