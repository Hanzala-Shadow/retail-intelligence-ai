"""esg/config.py — paths owned by the ESG / sustainability pipeline.

Imported as bare ``config`` by everything under ``esg/``, exactly as
``src/config.py`` was before the pipelines were split. It re-exports
``common.config`` wholesale, so a module that writes ``import config`` still
sees ``REPO_ROOT``, ``REFERENCE_DIR``, ``COMPANIES_CSV`` and the rest
alongside the ESG constants below.

The ``data/`` layout did not change with the split: these are the same
directories and CSVs, under the same names, that the ESG stages already read
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
    DATA_DIR,
    EMBEDDING_DIR,
    INTERIM_DIR,
    RAW_DIR,
    REPORTS_DIR,
    SECTIONS_DIR,
    CHUNKS_DIR,
    as_repo_relative,
    main as _main,
)

# ---------------------------------------------------------------------------
# Directories
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
# Reference/index CSVs
# ---------------------------------------------------------------------------
SUSTAINABILITY_TRACKER_CSV = REFERENCE_DIR / "sustainability_report_tracker.csv"  # noqa: F405
ESG_DRIVE_MANIFEST_CSV = REFERENCE_DIR / "esg_drive_manifest.csv"  # noqa: F405
ESG_FILE_CATALOG_CSV = REFERENCE_DIR / "esg_file_catalog.csv"  # noqa: F405
ESG_PARSE_INDEX_CSV = REFERENCE_DIR / "esg_parse_index.csv"  # noqa: F405
ESG_SECTIONS_INDEX_CSV = REFERENCE_DIR / "esg_sections_index.csv"  # noqa: F405
ESG_CHUNKS_INDEX_CSV = REFERENCE_DIR / "esg_chunks_index.csv"  # noqa: F405
ESG_CHUNKS_INDEX_ENRICHED_CSV = REFERENCE_DIR / "esg_chunks_index_enriched.csv"  # noqa: F405
ESG_SOURCE_REGISTRY_CSV = REFERENCE_DIR / "esg_source_registry.csv"  # noqa: F405
ESG_PAGE_LAYOUT_QA_CSV = REFERENCE_DIR / "esg_page_layout_qa.csv"  # noqa: F405
ESG_PIPELINE_QA_CSV = REFERENCE_DIR / "esg_pipeline_qa.csv"  # noqa: F405
ESG_OCR_APPROVAL_CSV = REFERENCE_DIR / "esg_ocr_approval.csv"  # noqa: F405
ESG_PARSER_OVERRIDES_CSV = REFERENCE_DIR / "esg_parser_overrides.csv"  # noqa: F405
ESG_PAGE_OCR_OVERRIDES_CSV = REFERENCE_DIR / "esg_page_ocr_overrides.csv"  # noqa: F405
ESG_CHUNK_HISTORY_CSV = REFERENCE_DIR / "esg_chunk_history.csv"  # noqa: F405
ESG_ACCEPTED_COMPANY_MANIFEST_CSV = REFERENCE_DIR / "esg_accepted_company_manifest.csv"  # noqa: F405
ESG_CHUNK_EMBEDDING_CONTEXT_CSV = REFERENCE_DIR / "esg_chunk_embedding_context.csv"  # noqa: F405
ESG_LINES_AREA_SIGNALS_CSV = REFERENCE_DIR / "esg_lines_area_signals.csv"  # noqa: F405
BANNED_COMPANIES_CSV = REFERENCE_DIR / "banned_companies.csv"  # noqa: F405
VECTOR_INDEX_MANIFEST_CSV = REFERENCE_DIR / "vector_index_manifest.csv"  # noqa: F405
# Consumed by the sampling runners. The file is not currently generated.
ESG_SAMPLE_DOCS_CSV = REFERENCE_DIR / "esg_sample_docs.csv"  # noqa: F405

# ---------------------------------------------------------------------------
# Local SQLite packages and reports
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
# This pipeline's own raw root (the shared stage roots are created by
# common/config.py).
# ---------------------------------------------------------------------------
os.makedirs(RAW_SUSTAINABILITY_DIR, exist_ok=True)


if __name__ == "__main__":
    # Shared + ESG. The runners read the merged table from common/config.py;
    # this is the scoped view, for asking what the ESG pipeline alone owns.
    _main(globals())
