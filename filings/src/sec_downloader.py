"""
sec_downloader.py

Arsal's Days 4-5 deliverable.

Reads data/00_reference/filings.csv (produced by sec_discovery.py), and for
every row with download_status == 'pending':
  1. Downloads the .htm filing document from SEC EDGAR (primary_doc_url)
  2. Validates the downloaded file size > 0
  3. Saves it locally to data/01_raw/10k/{ticker}/{accession_number}.htm
  4. Uploads it to Google Drive /10-K Filings/{ticker}/ via drive_uploader.py
  5. Records the returned Drive file_id and updates download_status in
     data/00_reference/filing_state.csv

Retries up to 3 times per file before marking as 'failed'.
Safe to re-run: rows already marked 'downloaded' or 'failed' are skipped
unless --retry-failed is passed.

Usage:
    python filings/src/sec_downloader.py                  # process all pending
    python filings/src/sec_downloader.py --limit 10        # first 10 pending only
    python filings/src/sec_downloader.py --retry-failed     # also retry failed rows
    python filings/src/sec_downloader.py --no-drive         # download only, skip Drive upload
                                                      (useful if credentials.json isn't ready yet)
"""

import argparse
import csv
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import requests
import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)

import config
from sec_discovery import USER_AGENT, RateLimitedSession, REPO_ROOT, FILINGS_CSV, FILINGS_CSV_FIELDS

# drive_uploader pulls in google-api-python-client / google-auth, which may
# not be installed yet (e.g. before Hanzala's Days 2-3 service account setup
# is ready). Import lazily so --no-drive mode still works without them.
try:
    import drive_uploader
    _DRIVE_AVAILABLE = True
except ImportError:
    drive_uploader = None
    _DRIVE_AVAILABLE = False

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

RAW_10K_DIR = config.RAW_10K_DIR
FILING_STATE_CSV = config.FILING_STATE_CSV
LOG_PATH = config.DOWNLOAD_ERRORS_LOG

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 3

FILING_STATE_FIELDS = [
    "accession_number",
    "ticker",
    "last_checked_at",
    "n_filings_known",
    "download_status",
    "drive_file_id",
]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("sec_downloader")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_filings() -> list:
    if not FILINGS_CSV.exists():
        log.error(f"filings.csv not found at {FILINGS_CSV}. Run sec_discovery.py first.")
        sys.exit(1)
    with open(FILINGS_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_filing_state() -> dict:
    """Keyed by accession_number."""
    state = {}
    if FILING_STATE_CSV.exists():
        with open(FILING_STATE_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                state[row["accession_number"]] = row
    return state


def write_filing_state(state: dict):
    FILING_STATE_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(FILING_STATE_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FILING_STATE_FIELDS)
        writer.writeheader()
        for row in state.values():
            writer.writerow(row)


def update_filings_csv_status(filings: list, accession_number: str, new_status: str):
    """Updates download_status in the in-memory filings list; caller is
    responsible for re-writing filings.csv after the run."""
    for row in filings:
        if row["accession_number"] == accession_number:
            row["download_status"] = new_status
            break


def write_filings_csv(filings: list):
    with open(FILINGS_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FILINGS_CSV_FIELDS)
        writer.writeheader()
        for row in filings:
            writer.writerow(row)


def download_one(session: RateLimitedSession, url: str, dest_path: Path) -> bool:
    """Downloads url to dest_path. Returns True on success (file exists,
    size > 0), False otherwise. Raises on unrecoverable errors after
    retries are exhausted."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url)
            if resp.status_code != 200:
                log.warning(f"HTTP {resp.status_code} downloading {url} (attempt {attempt}/{MAX_RETRIES})")
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue

            dest_path.write_bytes(resp.content)

            if dest_path.stat().st_size > 0:
                return True

            log.warning(f"Downloaded file is empty: {url} (attempt {attempt}/{MAX_RETRIES})")
            dest_path.unlink(missing_ok=True)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)

        except requests.RequestException as exc:
            log.warning(f"Download error on {url}: {exc} (attempt {attempt}/{MAX_RETRIES})")
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)

    return False


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------


def run(limit: int = None, retry_failed: bool = False, skip_drive: bool = False):
    filings = load_filings()
    state = load_filing_state()

    statuses_to_process = {"pending"}
    if retry_failed:
        statuses_to_process.add("failed")

    pending = [r for r in filings if r["download_status"] in statuses_to_process]
    if limit:
        pending = pending[:limit]

    log.info(f"{len(pending)} filing(s) to process out of {len(filings)} total in filings.csv")

    if skip_drive:
        log.warning("--no-drive set: files will be downloaded locally only, NOT uploaded to Drive")
    elif not _DRIVE_AVAILABLE:
        log.error(
            "drive_uploader could not be imported (google-api-python-client / "
            "google-auth not installed). Either run:\n"
            "  pip install google-api-python-client google-auth\n"
            "or pass --no-drive to download locally only for now."
        )
        sys.exit(1)

    session = RateLimitedSession(USER_AGENT)

    downloaded_count = 0
    uploaded_count = 0
    failed_count = 0

    for row in pending:
        accession = row["accession_number"]
        ticker = row["ticker"]
        url = row["primary_doc_url"]

        local_filename = f"{accession}.htm"
        local_path = RAW_10K_DIR / ticker / local_filename

        if local_path.exists() and local_path.stat().st_size > 0:
            log.info(f"{ticker}: local copy already exists ({local_filename}), skipping re-download")
            ok = True
        else:
            log.info(f"{ticker}: downloading {accession} ...")
            ok = download_one(session, url, local_path)

        now = datetime.now(timezone.utc).isoformat()

        if not ok:
            log.error(f"{ticker}: FAILED to download {accession} after {MAX_RETRIES} retries")
            update_filings_csv_status(filings, accession, "failed")
            state[accession] = {
                "accession_number": accession,
                "ticker": ticker,
                "last_checked_at": now,
                "n_filings_known": state.get(accession, {}).get("n_filings_known", 1),
                "download_status": "failed",
                "drive_file_id": state.get(accession, {}).get("drive_file_id", ""),
            }
            failed_count += 1
            continue

        downloaded_count += 1
        drive_file_id = ""

        if not skip_drive:
            try:
                last_exc = None
                for attempt in range(1, 4):
                    try:
                        drive_file_id = drive_uploader.upload_file_for_ticker(
                            str(local_path), ticker, drive_filename=local_filename
                        )
                        break
                    except Exception as exc:
                        last_exc = exc
                        if attempt == 3:
                            raise
                        time.sleep(2 ** attempt)
                uploaded_count += 1
                log.info(f"{ticker}: uploaded {accession} to Drive (file_id={drive_file_id})")
                update_filings_csv_status(filings, accession, "downloaded")
            except Exception as exc:
                log.error(f"{ticker}: downloaded but Drive upload FAILED for {accession} — {exc}")
                update_filings_csv_status(filings, accession, "downloaded_not_uploaded")
        else:
            update_filings_csv_status(filings, accession, "downloaded_not_uploaded")

        state[accession] = {
            "accession_number": accession,
            "ticker": ticker,
            "last_checked_at": now,
            "n_filings_known": state.get(accession, {}).get("n_filings_known", 1),
            "download_status": filings_status_lookup(filings, accession),
            "drive_file_id": drive_file_id or state.get(accession, {}).get("drive_file_id", ""),
        }

    write_filings_csv(filings)
    write_filing_state(state)

    log.info(
        f"Done. downloaded={downloaded_count} uploaded_to_drive={uploaded_count} "
        f"failed={failed_count} (out of {len(pending)} processed)"
    )
    log.info(f"filing_state.csv written to {FILING_STATE_CSV}")


def filings_status_lookup(filings: list, accession_number: str) -> str:
    for row in filings:
        if row["accession_number"] == accession_number:
            return row["download_status"]
    return "unknown"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download pending 10-K filings and upload to Drive")
    parser.add_argument("--limit", type=int, default=None, help="Only process first N pending filings")
    parser.add_argument("--retry-failed", action="store_true", help="Also retry rows marked 'failed'")
    parser.add_argument("--no-drive", action="store_true", help="Download locally only, skip Drive upload")
    args = parser.parse_args()

    run(limit=args.limit, retry_failed=args.retry_failed, skip_drive=args.no_drive)
