"""
db_utils.py — Database helper functions for the Retail Intelligence Pipeline.
All operations are idempotent: safe to re-run without creating duplicates.
"""

import os
import logging
from contextlib import contextmanager
from typing import Optional

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.dialects.postgresql import insert as pg_insert
from dotenv import load_dotenv

from common.models import Base, Company, AnnualFiling, SustainabilityReport, Document

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Engine & Session
# ---------------------------------------------------------------------------

_engine = None
_SessionLocal = None


def get_engine():
    """Return (and lazily create) the singleton SQLAlchemy engine."""
    global _engine
    if _engine is None:
        db_url = os.getenv("DB_URL")
        if not db_url:
            raise EnvironmentError("DB_URL is not set in the environment / .env file.")
        _engine = create_engine(
            db_url,
            pool_pre_ping=True,      # drop stale connections automatically
            pool_size=5,
            max_overflow=10,
            echo=False,              # set True for SQL debug output
        )
        logger.info("SQLAlchemy engine created.")
    return _engine


def connect() -> sessionmaker:
    """
    Initialise the connection pool and return the session factory.
    Call once at startup; re-calling is safe (no-op after first call).

    Usage:
        SessionLocal = connect()
        with SessionLocal() as session:
            ...
    """
    global _SessionLocal
    if _SessionLocal is None:
        engine = get_engine()
        Base.metadata.create_all(engine)   # no-op if tables already exist
        _SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
        logger.info("Database connection pool initialised.")
    return _SessionLocal


@contextmanager
def get_session() -> Session:
    """
    Context manager that yields a Session and handles commit / rollback.

    Usage:
        with get_session() as session:
            session.add(obj)
    """
    SessionLocal = connect()
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Bulk insert helpers  (all use ON CONFLICT DO NOTHING — fully idempotent)
# ---------------------------------------------------------------------------

def bulk_insert_companies(companies: list[dict]) -> int:
    """
    Upsert a list of company dicts into the companies table.

    Each dict must contain: ticker, cik, name
    Optional keys: sector, exchange

    Returns the number of rows actually inserted (not skipped).

    Example:
        rows = [
            {"ticker": "GAP", "cik": "0000039911", "name": "Gap Inc.",
             "sector": "Apparel & Footwear", "exchange": "NYSE"},
        ]
        bulk_insert_companies(rows)
    """
    if not companies:
        return 0

    stmt = (
        pg_insert(Company.__table__)
        .values(companies)
        .on_conflict_do_nothing(index_elements=["ticker"])   # skip if ticker already exists
    )

    with get_session() as session:
        result = session.execute(stmt)
        inserted = result.rowcount
        logger.info(f"bulk_insert_companies: {inserted}/{len(companies)} rows inserted.")
        return inserted


def bulk_insert_filings(filings: list[dict]) -> int:
    """
    Upsert a list of annual filing dicts into the annual_filings table.

    Each dict must contain: company_id, year, accession_number
    Optional keys: filing_date, download_status

    Returns the number of rows actually inserted (not skipped).

    Example:
        rows = [
            {"company_id": 1, "year": 2024,
             "accession_number": "0000039911-24-000066",
             "filing_date": "2024-03-19", "download_status": "pending"},
        ]
        bulk_insert_filings(rows)
    """
    if not filings:
        return 0

    stmt = (
        pg_insert(AnnualFiling.__table__)
        .values(filings)
        .on_conflict_do_nothing(index_elements=["accession_number"])
    )

    with get_session() as session:
        result = session.execute(stmt)
        inserted = result.rowcount
        logger.info(f"bulk_insert_filings: {inserted}/{len(filings)} rows inserted.")
        return inserted


def bulk_insert_sustainability_reports(reports: list[dict]) -> int:
    """
    Upsert sustainability/ESG report metadata into sustainability_reports.

    Each dict must contain: company_id
    Optional keys: year, report_url, format, download_status

    Conflict key: (company_id, year) — one report per company per year.

    Returns the number of rows actually inserted.
    """
    if not reports:
        return 0

    stmt = (
        pg_insert(SustainabilityReport.__table__)
        .values(reports)
        .on_conflict_do_nothing(index_elements=["company_id", "year"])
    )

    with get_session() as session:
        result = session.execute(stmt)
        inserted = result.rowcount
        logger.info(f"bulk_insert_sustainability_reports: {inserted}/{len(reports)} rows inserted.")
        return inserted


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def get_pending_downloads(doc_type: str = "10k") -> list[dict]:
    """
    Return all annual filings (doc_type='10k') or sustainability reports
    (doc_type='sustainability') that still have download_status='pending'.

    Returns a list of dicts, each containing the columns needed by the
    downloader: company_id, ticker, accession_number / report_url, year.

    Usage:
        pending = get_pending_downloads("10k")
        for row in pending:
            download(row["accession_number"])
    """
    with get_session() as session:
        if doc_type == "10k":
            rows = (
                session.query(
                    AnnualFiling.filing_id,
                    AnnualFiling.company_id,
                    AnnualFiling.year,
                    AnnualFiling.accession_number,
                    AnnualFiling.filing_date,
                    Company.ticker,
                    Company.cik,
                )
                .join(Company, Company.company_id == AnnualFiling.company_id)
                .filter(AnnualFiling.download_status == "pending")
                .order_by(Company.ticker, AnnualFiling.year.desc())
                .all()
            )
            result = [
                {
                    "filing_id": r.filing_id,
                    "company_id": r.company_id,
                    "ticker": r.ticker,
                    "cik": r.cik,
                    "year": r.year,
                    "accession_number": r.accession_number,
                    "filing_date": str(r.filing_date) if r.filing_date else None,
                }
                for r in rows
            ]

        elif doc_type == "sustainability":
            rows = (
                session.query(
                    SustainabilityReport.report_id,
                    SustainabilityReport.company_id,
                    SustainabilityReport.year,
                    SustainabilityReport.report_url,
                    SustainabilityReport.format,
                    Company.ticker,
                )
                .join(Company, Company.company_id == SustainabilityReport.company_id)
                .filter(SustainabilityReport.download_status == "pending")
                .order_by(Company.ticker)
                .all()
            )
            result = [
                {
                    "report_id": r.report_id,
                    "company_id": r.company_id,
                    "ticker": r.ticker,
                    "year": r.year,
                    "report_url": r.report_url,
                    "format": r.format,
                }
                for r in rows
            ]

        else:
            raise ValueError(f"doc_type must be '10k' or 'sustainability', got '{doc_type}'")

        logger.info(f"get_pending_downloads('{doc_type}'): {len(result)} pending rows.")
        return result


def get_company_by_ticker(ticker: str) -> Optional[dict]:
    """
    Look up a single company by ticker.
    Returns a dict or None if not found.
    """
    with get_session() as session:
        row = session.query(Company).filter(Company.ticker == ticker.upper()).first()
        if row is None:
            return None
        return {
            "company_id": row.company_id,
            "ticker": row.ticker,
            "cik": row.cik,
            "name": row.name,
            "sector": row.sector,
            "exchange": row.exchange,
        }


# ---------------------------------------------------------------------------
# Status update helpers
# ---------------------------------------------------------------------------

# Valid status values per table — used for input validation
_FILING_STATUSES = {"pending", "downloaded", "failed", "skipped"}
_REPORT_STATUSES = {"pending", "downloaded", "failed", "not_found", "ocr_required", "skipped"}
_PARSE_STATUSES  = {"not_started", "parsed", "failed", "ocr_required"}


def update_status(
    record_type: str,
    record_id: int,
    new_status: str,
    drive_file_id: str = None,
    filepath: str = None,
) -> bool:
    """
    Update the status of a single record. Idempotent — safe to call multiple times.

    Args:
        record_type:  'filing' | 'sustainability_report' | 'document'
        record_id:    Primary key value (filing_id / report_id / doc_id)
        new_status:   New status string (validated against allowed values)
        drive_file_id: Optional; stored on annual_filings if provided
        filepath:     Optional; stored on documents if provided

    Returns True on success, False if the record was not found.

    Examples:
        update_status("filing", 42, "downloaded", drive_file_id="1BxiM...")
        update_status("sustainability_report", 7, "not_found")
        update_status("document", 15, "parsed", filepath="data/02_interim/GAP/...")
    """
    with get_session() as session:

        if record_type == "filing":
            if new_status not in _FILING_STATUSES:
                raise ValueError(f"Invalid filing status '{new_status}'. Allowed: {_FILING_STATUSES}")

            row = session.query(AnnualFiling).filter(AnnualFiling.filing_id == record_id).first()
            if row is None:
                logger.warning(f"update_status: filing_id={record_id} not found.")
                return False

            row.download_status = new_status
            if drive_file_id is not None:
                # Store drive_file_id via the documents table (one document per filing)
                doc = (
                    session.query(Document)
                    .filter(
                        Document.company_id == row.company_id,
                        Document.doc_type == "10k",
                        Document.filepath.contains(row.accession_number),
                    )
                    .first()
                )
                if doc and filepath:
                    doc.filepath = filepath

        elif record_type == "sustainability_report":
            if new_status not in _REPORT_STATUSES:
                raise ValueError(f"Invalid report status '{new_status}'. Allowed: {_REPORT_STATUSES}")

            row = (
                session.query(SustainabilityReport)
                .filter(SustainabilityReport.report_id == record_id)
                .first()
            )
            if row is None:
                logger.warning(f"update_status: report_id={record_id} not found.")
                return False
            row.download_status = new_status

        elif record_type == "document":
            if new_status not in _PARSE_STATUSES:
                raise ValueError(f"Invalid parse status '{new_status}'. Allowed: {_PARSE_STATUSES}")

            row = session.query(Document).filter(Document.doc_id == record_id).first()
            if row is None:
                logger.warning(f"update_status: doc_id={record_id} not found.")
                return False
            row.parse_status = new_status
            if filepath is not None:
                row.filepath = filepath

        else:
            raise ValueError(
                f"record_type must be 'filing', 'sustainability_report', or 'document'. Got '{record_type}'"
            )

        logger.info(f"update_status: {record_type} id={record_id} -> '{new_status}'")
        return True


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def db_health_check() -> dict:
    """
    Return row counts for all 6 tables plus pending download counts.
    Useful for daily status posts in #data-eng.

    Usage:
        from db_utils import db_health_check
        print(db_health_check())
    """
    with get_session() as session:
        counts = {
            "companies":               session.execute(text("SELECT COUNT(*) FROM companies")).scalar(),
            "annual_filings":          session.execute(text("SELECT COUNT(*) FROM annual_filings")).scalar(),
            "annual_filings_pending":  session.execute(text("SELECT COUNT(*) FROM annual_filings WHERE download_status='pending'")).scalar(),
            "annual_filings_done":     session.execute(text("SELECT COUNT(*) FROM annual_filings WHERE download_status='downloaded'")).scalar(),
            "sustainability_reports":  session.execute(text("SELECT COUNT(*) FROM sustainability_reports")).scalar(),
            "esg_pending":             session.execute(text("SELECT COUNT(*) FROM sustainability_reports WHERE download_status='pending'")).scalar(),
            "esg_done":                session.execute(text("SELECT COUNT(*) FROM sustainability_reports WHERE download_status='downloaded'")).scalar(),
            "documents":               session.execute(text("SELECT COUNT(*) FROM documents")).scalar(),
            "sections":                session.execute(text("SELECT COUNT(*) FROM sections")).scalar(),
            "chunks":                  session.execute(text("SELECT COUNT(*) FROM chunks")).scalar(),
        }
        return counts
