"""Isolated configuration for the FY2023–FY2025 v2 rebuild."""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_V2_ROOT = Path(os.environ.get("RETAIL_DATA_V2_ROOT", REPO_ROOT / "data_v2")).resolve()

REFERENCE_V2_DIR = DATA_V2_ROOT / "00_reference"
RAW_10K_V2_DIR = DATA_V2_ROOT / "01_raw" / "10k"
INTERIM_10K_V2_DIR = DATA_V2_ROOT / "02_interim" / "10k"
SECTIONS_10K_V2_DIR = DATA_V2_ROOT / "03_sections" / "10k"
CHUNKS_10K_V2_DIR = DATA_V2_ROOT / "04_chunks" / "10k"
DB_V2_DIR = DATA_V2_ROOT / "05_db"
EMBEDDINGS_V2_DIR = DATA_V2_ROOT / "06_embeddings"

V2_DB_URL = os.environ.get("RETAIL_V2_DB_URL")
EXPECTED_V2_DATABASE = "retail_pipeline_fy2325_v2"


def ensure_v2_directories() -> None:
    """Create only the isolated v2 directory tree."""
    for path in (
        REFERENCE_V2_DIR,
        RAW_10K_V2_DIR,
        INTERIM_10K_V2_DIR,
        SECTIONS_10K_V2_DIR,
        CHUNKS_10K_V2_DIR,
        DB_V2_DIR,
        EMBEDDINGS_V2_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)


def validate_v2_database_url() -> None:
    """Reject missing URLs and accidental v1 database targeting."""
    if not V2_DB_URL:
        raise RuntimeError("RETAIL_V2_DB_URL is required for v2 database operations")
    database_name = V2_DB_URL.rsplit("/", 1)[-1].split("?", 1)[0]
    if database_name != EXPECTED_V2_DATABASE:
        raise RuntimeError(
            f"Refusing database {database_name!r}; expected {EXPECTED_V2_DATABASE!r}"
        )
