"""
chunks_bulk_loader.py
Bulk loads chunks_index.csv + chunk text files into PostgreSQL chunks table.
Requires db_loader.py to have created:
  data/00_reference/companies_key_map.csv
  data/00_reference/sections_key_map.csv
"""

import argparse
import csv
import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import sessionmaker

from chunk_rag_policy import chunk_rag_metadata
from models import Chunk

load_dotenv()

DB_URL = os.getenv("DB_URL") or os.getenv("DATABASE_URL")
if not DB_URL:
    raise EnvironmentError("DB_URL or DATABASE_URL is not set.")

REFERENCE_DIR = Path("data/00_reference")
LOGS_DIR = Path("logs")
LOGS_DIR.mkdir(exist_ok=True)

BATCH_SIZE = 5000

logging.basicConfig(
    filename=LOGS_DIR / "chunk_load_errors.log",
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(message)s",
)

engine = create_engine(DB_URL, future=True, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, future=True)


def load_sections_key_map(path=REFERENCE_DIR / "sections_key_map.csv"):
    key_map = {}

    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key_map[row["stem"]] = {
                "section_id": int(row["section_id"]),
                "doc_id": int(row["doc_id"]),
            }

    return key_map


def load_companies_key_map(path=REFERENCE_DIR / "companies_key_map.csv"):
    company_map = {}

    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            company_map[row["ticker"]] = int(row["company_id"])

    return company_map


def require_chunk_table_ownership(session):
    """Fail before dropping indexes unless current user owns chunks."""
    row = session.execute(
        text(
            """
            SELECT
                current_user,
                pg_get_userbyid(c.relowner) AS table_owner
            FROM pg_class c
            JOIN pg_namespace n
              ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema()
              AND c.relname = 'chunks'
              AND c.relkind = 'r'
            """
        )
    ).one_or_none()

    if row is None:
        raise RuntimeError(
            "Could not determine owner of table chunks"
        )

    current_user, table_owner = row

    if current_user != table_owner:
        raise PermissionError(
            "--drop-indexes requires the database connection "
            f"user to own chunks; current_user={current_user}, "
            f"table_owner={table_owner}. Run without "
            "--drop-indexes or manage indexes as the owner."
        )


def drop_chunk_indexes(session):
    session.execute(text("DROP INDEX IF EXISTS idx_chunks_section"))
    session.execute(text("DROP INDEX IF EXISTS idx_chunks_company"))
    session.execute(text("DROP INDEX IF EXISTS idx_chunks_doc"))
    session.execute(text("DROP INDEX IF EXISTS idx_chunks_doc_type"))
    session.execute(text("DROP INDEX IF EXISTS idx_chunks_section_code"))
    session.commit()
    print("Dropped chunk indexes")


def rebuild_chunk_indexes(session):
    t0 = time.time()
    session.execute(text("CREATE INDEX IF NOT EXISTS idx_chunks_section ON chunks(section_id)"))
    session.execute(text("CREATE INDEX IF NOT EXISTS idx_chunks_company ON chunks(company_id)"))
    session.execute(text("CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id)"))
    session.execute(text("CREATE INDEX IF NOT EXISTS idx_chunks_doc_type ON chunks(doc_type)"))
    session.execute(text("CREATE INDEX IF NOT EXISTS idx_chunks_section_code ON chunks(section_code)"))
    session.commit()
    print(f"Rebuilt chunk indexes in {time.time() - t0:.1f}s")


def flush_batch(session, batch, dry_run=False):
    if not batch:
        return 0

    if dry_run:
        return len(batch)

    stmt = (
        pg_insert(Chunk.__table__)
        .values(batch)
        .on_conflict_do_nothing(index_elements=["section_id", "chunk_index"])
    )

    result = session.execute(stmt)
    session.commit()
    return result.rowcount


def stream_and_load_chunks(chunks_index_path, sections_map, company_map, dry_run=False):
    n_rows = 0
    n_resolved = 0
    n_inserted = 0
    n_errors = 0
    n_token_warnings = 0

    batch = []
    t0 = time.time()

    session = SessionLocal()

    try:
        if not dry_run:
            session.execute(text("SET LOCAL synchronous_commit TO OFF"))

        with open(chunks_index_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)

            for row in reader:
                n_rows += 1

                stem = f"{row['company']}__{row['doc_type']}__{row['accession']}__{row['section']}"

                section_info = sections_map.get(stem)
                company_id = company_map.get(row["company"])

                if section_info is None or company_id is None:
                    n_errors += 1
                    logging.warning(f"No FK match for chunk_id={row.get('chunk_id')} stem={stem}")
                    continue

                chunk_file = Path(row["file"])

                try:
                    chunk_text = chunk_file.read_text(encoding="utf-8", errors="replace")
                except OSError as e:
                    n_errors += 1
                    logging.warning(f"Could not read chunk file {chunk_file}: {e}")
                    continue

                try:
                    token_count = int(row["token_count"])
                except Exception:
                    token_count = None

                if token_count is not None and (token_count < 50 or token_count > 500):
                    n_token_warnings += 1
                    logging.warning(
                        f"chunk_id={row.get('chunk_id')} token_count={token_count} outside 50-500"
                    )

                chunk_index = int(row["chunk_index"])
                rag_metadata = chunk_rag_metadata(
                    doc_type=row["doc_type"],
                    section_code=row["section"],
                    chunk_index=chunk_index,
                    chunk_text=chunk_text,
                    token_count=token_count,
                )

                batch.append({
                    "section_id": section_info["section_id"],
                    "doc_id": section_info["doc_id"],
                    "company_id": company_id,
                    "doc_type": row["doc_type"],
                    "section_code": row["section"],
                    "chunk_index": chunk_index,
                    "chunk_text": chunk_text,
                    "token_count": token_count,
                    **rag_metadata,
                })

                n_resolved += 1

                if len(batch) >= BATCH_SIZE:
                    inserted = flush_batch(session, batch, dry_run=dry_run)
                    n_inserted += inserted
                    batch.clear()

                    elapsed = time.time() - t0
                    rate = n_resolved / elapsed if elapsed else 0
                    print(
                        f"  resolved={n_resolved} inserted={n_inserted} "
                        f"errors={n_errors} rate={rate:.0f} rows/s"
                    )

        if batch:
            inserted = flush_batch(session, batch, dry_run=dry_run)
            n_inserted += inserted
            batch.clear()

    finally:
        session.close()

    elapsed_min = (time.time() - t0) / 60

    print()
    print(f"Rows read: {n_rows}")
    print(f"Rows resolved: {n_resolved}")
    print(f"Rows inserted: {n_inserted}" if not dry_run else f"[dry-run] Rows ready: {n_resolved}")
    print(f"Errors: {n_errors}")
    print(f"Token warnings: {n_token_warnings}")
    print(f"Elapsed: {elapsed_min:.1f} minutes")
    print(f"Error log: {LOGS_DIR / 'chunk_load_errors.log'}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--chunks-index", default=str(REFERENCE_DIR / "chunks_index.csv"))
    parser.add_argument("--drop-indexes", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    chunks_index_path = Path(args.chunks_index)

    if not chunks_index_path.exists():
        raise FileNotFoundError(f"Missing chunks index: {chunks_index_path}")

    print("Loading lookup maps...")
    sections_map = load_sections_key_map()
    company_map = load_companies_key_map()
    print(f"  sections map: {len(sections_map)}")
    print(f"  companies map: {len(company_map)}")

    indexes_dropped = False

    if args.drop_indexes and not args.dry_run:
        session = SessionLocal()

        try:
            require_chunk_table_ownership(session)
            drop_chunk_indexes(session)
            indexes_dropped = True
        finally:
            session.close()

    try:
        stream_and_load_chunks(
            chunks_index_path,
            sections_map,
            company_map,
            dry_run=args.dry_run,
        )
    finally:
        if indexes_dropped:
            session = SessionLocal()

            try:
                rebuild_chunk_indexes(session)
            finally:
                session.close()


if __name__ == "__main__":
    main()
