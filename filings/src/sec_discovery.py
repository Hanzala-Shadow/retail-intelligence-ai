"""
sec_discovery.py

Arsal's Days 2-3 deliverable.

For every company in data/00_reference/companies.csv, calls the SEC
Submissions API (https://data.sec.gov/submissions/CIK##########.json),
filters to 10-K filings only, keeps the 3 most recent fiscal years, and
upserts the results into data/00_reference/filings.csv.

Idempotent: re-running this script will not create duplicate rows in
filings.csv. It keys on accession_number, which is unique per SEC filing
(matches the UNIQUE constraint on annual_filings.accession_number in
V1__Schema.sql).

Rate limit: SEC EDGAR allows max 8 requests/sec. This script self-throttles
to 8 req/sec using a fixed sleep between requests, and uses exponential
backoff on failures (429 / 5xx / connection errors).

Required before running:
- A real contact email in SEC_USER_AGENT_EMAIL below (or via env var).
  SEC will block/throttle requests with a generic or missing User-Agent.
- data/00_reference/companies.csv must exist (columns: company_id, ticker,
  cik, name, sector, exchange).

Usage:
    python filings/src/sec_discovery.py
    python filings/src/sec_discovery.py --limit 5      # test on first 5 companies only
    python filings/src/sec_discovery.py --ticker GPS   # test a single company
"""

import argparse
import csv
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config — loaded centrally from config.py (single source of truth for env
# vars across the whole pipeline; do not re-invent env var names here)
# ---------------------------------------------------------------------------
import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)
import config
from config import SEC_USER_AGENT

USER_AGENT = SEC_USER_AGENT

SEC_REQUESTS_PER_SECOND = 8
SEC_MIN_INTERVAL = 1.0 / SEC_REQUESTS_PER_SECOND  # seconds between requests

MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 2  # 2s, 4s, 8s

YEARS_TO_KEEP = 3  # most recent fiscal years per company

REPO_ROOT = config.REPO_ROOT
COMPANIES_CSV = config.COMPANIES_CSV
FILINGS_CSV = config.FILINGS_CSV
LOG_PATH = config.DOWNLOAD_ERRORS_LOG

SUBMISSIONS_URL_TMPL = "https://data.sec.gov/submissions/CIK{cik10}.json"

FILINGS_CSV_FIELDS = [
    "company_id",
    "ticker",
    "cik",
    "accession_number",
    "form",
    "filing_date",
    "year",
    "primary_doc_url",
    "download_status",
]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("sec_discovery")

# ---------------------------------------------------------------------------
# Rate-limited session
# ---------------------------------------------------------------------------


class RateLimitedSession:
    """Wraps requests.Session and guarantees no more than
    SEC_REQUESTS_PER_SECOND requests/sec, with exponential backoff retries."""

    def __init__(self, user_agent: str):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        self._last_request_time = 0.0

    def get(self, url: str) -> requests.Response:
        # Throttle
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < SEC_MIN_INTERVAL:
            time.sleep(SEC_MIN_INTERVAL - elapsed)

        last_exc = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                self._last_request_time = time.monotonic()
                resp = self.session.get(url, timeout=15)
                if resp.status_code == 200:
                    return resp
                if resp.status_code == 429:
                    wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                    log.warning(f"429 rate-limited on {url}, retry {attempt}/{MAX_RETRIES} in {wait}s")
                    time.sleep(wait)
                    continue
                if resp.status_code == 404:
                    # CIK not found / no submissions — not retryable
                    log.error(f"404 Not Found: {url}")
                    return resp
                # Other server errors -> retry
                log.warning(f"HTTP {resp.status_code} on {url}, retry {attempt}/{MAX_RETRIES}")
                time.sleep(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
            except requests.RequestException as exc:
                last_exc = exc
                wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                log.warning(f"Request error on {url}: {exc}. Retry {attempt}/{MAX_RETRIES} in {wait}s")
                time.sleep(wait)

        if last_exc:
            raise last_exc
        raise RuntimeError(f"Failed to fetch {url} after {MAX_RETRIES} retries")


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def load_companies(limit: int = None, only_ticker: str = None):
    if not COMPANIES_CSV.exists():
        log.error(f"companies.csv not found at {COMPANIES_CSV}")
        sys.exit(1)

    with open(COMPANIES_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if only_ticker:
        rows = [r for r in rows if r["ticker"].upper() == only_ticker.upper()]
        if not rows:
            log.error(f"Ticker {only_ticker} not found in companies.csv")
            sys.exit(1)
    if limit:
        rows = rows[:limit]

    return rows


def fetch_filings_for_company(session: RateLimitedSession, cik: str) -> dict:
    """Calls the SEC Submissions API for a single CIK. Returns parsed JSON,
    or None if the company has no submissions on file."""
    cik10 = cik.strip().zfill(10)
    url = SUBMISSIONS_URL_TMPL.format(cik10=cik10)
    resp = session.get(url)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def extract_10k_filings(submissions_json: dict, ticker: str) -> list:
    """Pulls 10-K filings out of the SEC submissions payload and returns the
    YEARS_TO_KEEP most recent ones as a list of dicts."""
    recent = submissions_json.get("filings", {}).get("recent", {})

    forms = recent.get("form", [])
    accession_numbers = recent.get("accessionNumber", [])
    filing_dates = recent.get("filingDate", [])
    primary_docs = recent.get("primaryDocument", [])

    cik = str(submissions_json.get("cik", "")).zfill(10)

    candidates = []
    for i, form in enumerate(forms):
        if form != "10-K":
            continue
        accession_raw = accession_numbers[i]  # e.g. "0000320193-23-000106"
        accession_nodash = accession_raw.replace("-", "")
        filing_date = filing_dates[i]
        primary_doc = primary_docs[i] if i < len(primary_docs) else ""

        if not primary_doc:
            log.warning(f"{ticker}: 10-K {accession_raw} has no primaryDocument, skipping")
            continue

        primary_doc_url = (
            f"https://www.sec.gov/Archives/edgar/data/"
            f"{int(cik)}/{accession_nodash}/{primary_doc}"
        )

        try:
            year = int(filing_date[:4])
        except (ValueError, TypeError):
            year = None

        candidates.append(
            {
                "accession_number": accession_raw,
                "form": form,
                "filing_date": filing_date,
                "year": year,
                "primary_doc_url": primary_doc_url,
            }
        )

    # Most recent first, then keep top N
    candidates.sort(key=lambda r: r["filing_date"], reverse=True)
    return candidates[:YEARS_TO_KEEP]


def load_existing_filings() -> dict:
    """Loads filings.csv if it exists, returns dict keyed by accession_number
    for idempotent upsert."""
    existing = {}
    if FILINGS_CSV.exists():
        with open(FILINGS_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing[row["accession_number"]] = row
    return existing


def write_filings_csv(filings_by_accession: dict):
    FILINGS_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(FILINGS_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FILINGS_CSV_FIELDS)
        writer.writeheader()
        for row in filings_by_accession.values():
            writer.writerow(row)


def run(limit: int = None, only_ticker: str = None):
    if not USER_AGENT or "REPLACE_ME" in USER_AGENT:
        log.error(
            "SEC_USER_AGENT is not set (or still a placeholder) in .env. "
            "Set a real descriptive value, e.g.:\n"
            '  SEC_USER_AGENT="Retail Intelligence Project your_real_email@example.com"'
        )
        sys.exit(1)

    companies = load_companies(limit=limit, only_ticker=only_ticker)
    log.info(f"Loaded {len(companies)} companies from {COMPANIES_CSV}")

    session = RateLimitedSession(USER_AGENT)
    existing = load_existing_filings()
    log.info(f"Loaded {len(existing)} existing filings from {FILINGS_CSV.name}")

    success_count = 0
    fail_count = 0
    no_10k_count = 0

    for row in companies:
        ticker = row["ticker"]
        cik = row["cik"]
        company_id = row["company_id"]

        try:
            submissions = fetch_filings_for_company(session, cik)
        except Exception as exc:
            log.error(f"{ticker} (CIK {cik}): FAILED to fetch submissions — {exc}")
            fail_count += 1
            continue

        if submissions is None:
            log.warning(f"{ticker} (CIK {cik}): no submissions found on SEC (404)")
            fail_count += 1
            continue

        ten_ks = extract_10k_filings(submissions, ticker)
        if not ten_ks:
            log.warning(f"{ticker}: no 10-K filings found")
            no_10k_count += 1
            continue

        for filing in ten_ks:
            existing[filing["accession_number"]] = {
                "company_id": company_id,
                "ticker": ticker,
                "cik": cik,
                "accession_number": filing["accession_number"],
                "form": filing["form"],
                "filing_date": filing["filing_date"],
                "year": filing["year"],
                "primary_doc_url": filing["primary_doc_url"],
                # Preserve existing download_status if this accession_number
                # was already in filings.csv (e.g. already downloaded);
                # otherwise default to 'pending'.
                "download_status": existing.get(filing["accession_number"], {}).get(
                    "download_status", "pending"
                ),
            }

        log.info(f"{ticker}: found {len(ten_ks)} 10-K filing(s)")
        success_count += 1

    write_filings_csv(existing)

    log.info(
        f"Done. companies_ok={success_count} companies_failed={fail_count} "
        f"companies_no_10k={no_10k_count} total_filings_in_csv={len(existing)}"
    )
    log.info(f"Output written to {FILINGS_CSV}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Discover 10-K filings via SEC EDGAR Submissions API")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N companies (testing)")
    parser.add_argument("--ticker", type=str, default=None, help="Only process a single ticker (testing)")
    args = parser.parse_args()

    run(limit=args.limit, only_ticker=args.ticker)
