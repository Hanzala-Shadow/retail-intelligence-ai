"""
db_loader.py — Database pipeline loader for the Retail Intelligence Pipeline.

"""

import os
import sys
import logging
import pandas as pd
from config import DATA_DIR
import db_utils

# ---------------------------------------------------------------------------
# Setup Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/db_loader.log", mode="a")
    ]
)
logger = logging.getLogger("db_loader")

COMPANIES_CSV = os.path.join(DATA_DIR, "00_reference", "companies.csv")
FILINGS_CSV = os.path.join(DATA_DIR, "00_reference", "filings.csv")


def seed_companies_table() -> None:
    """
    Reads companies.csv and seeds the `companies` table using db_utils.
    Gracefully handles missing entries (NaNs) and enforces CIK padding.
    """
    logger.info(f"Starting database seeding from {COMPANIES_CSV}...")
    
    if not os.path.exists(COMPANIES_CSV):
        logger.error(f"Seeding failed: {COMPANIES_CSV} does not exist.")
        return

    try:
        df = pd.read_csv(COMPANIES_CSV)
        
        # Replace Pandas NaN with safe Python None (SQL NULL) for optional fields
        df = df.where(pd.notnull(df), None)
        df.columns = [col.lower().strip() for col in df.columns]
        
        if "ticker" not in df.columns or "cik" not in df.columns:
            logger.error("Critical error: 'ticker' or 'cik' columns are missing from companies.csv.")
            return

        company_records = []
        for _, row in df.iterrows():
            if not row["ticker"] or not row["cik"]:
                logger.warning(f"Skipping row missing critical identification data: {row.to_dict()}")
                continue
                
            # Safely cast and pad CIK to standard 10 digits
            cik_raw = row["cik"]
            if isinstance(cik_raw, (int, float)):
                cik_str = str(int(cik_raw)).zfill(10)
            else:
                cik_str = str(cik_raw).strip().zfill(10)
            
            company_records.append({
                "ticker": str(row["ticker"]).upper().strip(),
                "cik": cik_str,
                "name": row["name"] if row["name"] else "Unknown Retailer",
                "sector": row["sector"],       # Ingests perfectly as None if missing
                "exchange": row["exchange"]    # Ingests perfectly as None if missing
            })
        
        logger.info(f"Parsed {len(company_records)} valid company profiles.")
        inserted_rows = db_utils.bulk_insert_companies(company_records)
        logger.info(f"Companies seeding complete. Newly inserted: {inserted_rows} records.")

    except Exception as e:
        logger.exception(f"An error occurred during company seeding: {str(e)}")


def load_filings_metadata() -> None:
    """
    Reads filings.csv and filters for filings where download_status is 'downloaded'.
    Resolves the internal DB company_id and populates the annual_filings table.
    """
    logger.info(f"Loading downloaded annual filings metadata from {FILINGS_CSV}...")
    
    if not os.path.exists(FILINGS_CSV):
        logger.warning(f"Metadata load skipped: {FILINGS_CSV} not found yet.")
        return

    try:
        df_filings = pd.read_csv(FILINGS_CSV)
        if df_filings.empty:
            logger.warning("filings.csv is empty. No records to parse.")
            return

        df_filings = df_filings.where(pd.notnull(df_filings), None)
        df_filings.columns = [col.lower().strip() for col in df_filings.columns]

        # FILTER CONDITION: Only load filings that have successfully passed the downloader phase
        if "download_status" in df_filings.columns:
            df_downloaded = df_filings[df_filings["download_status"] == "downloaded"]
        else:
            logger.warning("download_status column missing from filings.csv. Defaulting to all rows.")
            df_downloaded = df_filings

        if df_downloaded.empty:
            logger.info("No filings with 'downloaded' status found yet in filings.csv.")
            return

        filing_records = []
        missing_tickers = set()

        for _, row in df_downloaded.iterrows():
            if not row.get("ticker") or not row.get("accession_number"):
                logger.warning(f"Skipping malformed filing record: {row.to_dict()}")
                continue

            ticker = str(row["ticker"]).upper().strip()
            company_info = db_utils.get_company_by_ticker(ticker)
            
            if not company_info:
                missing_tickers.add(ticker)
                continue

            filing_records.append({
                "company_id": company_info["company_id"],
                "year": int(row["year"]) if row["year"] is not None else None,
                "accession_number": str(row["accession_number"]).strip(),
                "filing_date": row.get("filing_date"),
                "download_status": "downloaded"
            })

        if missing_tickers:
            logger.warning(f"Skipped filings for tickers missing from DB: {missing_tickers}")

        logger.info(f"Resolved DB primary keys for {len(filing_records)} downloaded filings.")
        inserted_filings = db_utils.bulk_insert_filings(filing_records)
        logger.info(f"Filings metadata ingestion complete. Newly inserted: {inserted_filings} records.")

    except Exception as e:
        logger.exception(f"An error occurred during filing metadata ingestion: {str(e)}")


def run_loader_pipeline() -> None:
    """Runs the database staging components sequentially."""
    logger.info("Initializing database connection pool...")
    db_utils.connect()
    
    # Task 1: Seed base dimension tables
    seed_companies_table()
    
    # Task 2: Seed verified download tracks
    load_filings_metadata()
    
    # Task 3: Execution tracking diagnostics
    health = db_utils.db_health_check()
    logger.info(
        f"Database state after current pipeline block:\n"
        f"  - Total Registered Companies: {health.get('companies', 0)}\n"
        f"  - Total Loaded Filings:       {health.get('annual_filings', 0)}\n"
        f"  - Completed Downloads in DB:  {health.get('annual_filings_done', 0)}"
    )


if __name__ == "__main__":
    os.makedirs("logs", exist_ok=True)
    run_loader_pipeline()