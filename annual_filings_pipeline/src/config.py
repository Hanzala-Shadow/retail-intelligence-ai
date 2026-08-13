"""
config.py — Centralized path and environment configuration for the
Retail Intelligence Pipeline. No script should hardcode a path or call
os.getenv() directly for these values; import from here instead.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Load .env once, here, for the whole pipeline
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

# ---------------------------------------------------------------------------
# Data directory structure (matches repo layout in the Week 1 plan)
# ---------------------------------------------------------------------------
DATA_DIR = str(REPO_ROOT / "data")

REFERENCE_DIR = str(REPO_ROOT / "data" / "00_reference")
RAW_10K_DIR = str(REPO_ROOT / "data" / "01_raw" / "10k")
RAW_SUSTAINABILITY_DIR = str(REPO_ROOT / "data" / "01_raw" / "sustainability")
INTERIM_DIR = str(REPO_ROOT / "data" / "02_interim")
SECTIONS_DIR = str(REPO_ROOT / "data" / "03_sections")
CHUNKS_DIR = str(REPO_ROOT / "data" / "04_chunks")
DB_OUTPUT_DIR = str(REPO_ROOT / "data" / "05_db")
LOGS_DIR = str(REPO_ROOT / "logs")

# Reference CSVs
COMPANIES_CSV = str(Path(REFERENCE_DIR) / "companies.csv")
FILINGS_CSV = str(Path(REFERENCE_DIR) / "filings.csv")
SUSTAINABILITY_URLS_CSV = str(Path(REFERENCE_DIR) / "sustainability_urls.csv")

# Ensure all output/log directories exist (idempotent, safe to call repeatedly)
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

if __name__ == "__main__":
    validate_config()
    print("config OK")
    print(f"REPO_ROOT: {REPO_ROOT}")
    print(f"DATA_DIR:  {DATA_DIR}")
    for k, v in _REQUIRED.items():
        print(f"{k}: {'set' if v else 'MISSING'}")
