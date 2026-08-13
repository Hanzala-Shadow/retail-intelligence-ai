"""
db_loader.py
Loads companies -> annual_filings -> documents -> sections into PostgreSQL.

Outputs:
  data/00_reference/companies_key_map.csv
  data/00_reference/filings_key_map.csv
  data/00_reference/documents_key_map.csv
  data/00_reference/sections_key_map.csv
"""

import csv
import os
import time
from pathlib import Path

import tiktoken

from dotenv import load_dotenv
from sqlalchemy import create_engine, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import sessionmaker

from models import Company, AnnualFiling, Document, Section

load_dotenv()

DB_URL = os.getenv("DB_URL") or os.getenv("DATABASE_URL")
if not DB_URL:
    raise EnvironmentError("DB_URL or DATABASE_URL is not set.")

REFERENCE_DIR = Path("data/00_reference")
SECTIONS_DIR = Path("data/03_sections/10k")
MIN_SECTION_TOKENS = 50
ENCODER = tiktoken.get_encoding("cl100k_base")

engine = create_engine(DB_URL, future=True, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, future=True)


def write_csv(path, rows, fieldnames):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def insert_ignore(session, model, mappings, conflict_cols):
    if not mappings:
        return 0

    stmt = (
        pg_insert(model.__table__)
        .values(mappings)
        .on_conflict_do_nothing(index_elements=conflict_cols)
    )
    result = session.execute(stmt)
    session.commit()
    return result.rowcount


def load_companies(csv_path=REFERENCE_DIR / "companies.csv"):
    t0 = time.time()

    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    mappings = []
    for r in rows:
        ticker = (r.get("ticker") or "").strip()
        cik = (r.get("cik") or "").strip()
        name = (r.get("name") or "").strip()

        if not ticker or not cik or not name:
            continue

        mappings.append({
            "ticker": ticker,
            "cik": cik,
            "name": name,
            "sector": (r.get("sector") or "").strip() or None,
            "exchange": (r.get("exchange") or "").strip() or None,
        })

    session = SessionLocal()
    try:
        inserted = insert_ignore(session, Company, mappings, ["ticker"])
        companies = session.query(Company.company_id, Company.ticker).all()
        ticker_to_id = {ticker: company_id for company_id, ticker in companies}
    finally:
        session.close()

    write_csv(
        REFERENCE_DIR / "companies_key_map.csv",
        [{"ticker": ticker, "company_id": cid} for ticker, cid in sorted(ticker_to_id.items())],
        ["ticker", "company_id"],
    )

    print(f"[companies] {len(ticker_to_id)} available, {inserted} newly inserted in {time.time()-t0:.1f}s")
    return ticker_to_id


def load_annual_filings(company_map, csv_path=REFERENCE_DIR / "filings.csv"):
    t0 = time.time()

    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    mappings = []
    skipped = 0

    for r in rows:
        ticker = (r.get("ticker") or "").strip()
        cid = company_map.get(ticker)

        if cid is None:
            skipped += 1
            continue

        year_raw = (r.get("year") or "").strip()
        accession = (r.get("accession_number") or "").strip()

        if not year_raw or not accession:
            skipped += 1
            continue

        mappings.append({
            "company_id": cid,
            "year": int(year_raw),
            "accession_number": accession,
            "filing_date": (r.get("filing_date") or "").strip() or None,
            "download_status": (r.get("download_status") or "pending").strip() or "pending",
        })

    session = SessionLocal()
    try:
        inserted = insert_ignore(session, AnnualFiling, mappings, ["accession_number"])
        filings = session.query(AnnualFiling.filing_id, AnnualFiling.accession_number).all()
        accession_to_id = {accession: filing_id for filing_id, accession in filings}
    finally:
        session.close()

    write_csv(
        REFERENCE_DIR / "filings_key_map.csv",
        [{"accession_number": a, "filing_id": fid} for a, fid in sorted(accession_to_id.items())],
        ["accession_number", "filing_id"],
    )

    print(f"[annual_filings] {len(accession_to_id)} available, {inserted} newly inserted, {skipped} skipped in {time.time()-t0:.1f}s")
    return accession_to_id


def discover_doc_triples(sections_dir=SECTIONS_DIR):
    triples = set()

    for f in sections_dir.rglob("*.txt"):
        if "FULL_DOCUMENT_FALLBACK" in f.name:
            continue

        parts = f.stem.split("__")
        if len(parts) < 4:
            continue

        company, doc_type, accession = parts[0], parts[1], parts[2]
        triples.add((company, doc_type, accession))

    return sorted(triples)


def load_documents(company_map, sections_dir=SECTIONS_DIR):
    t0 = time.time()
    triples = discover_doc_triples(sections_dir)

    mappings = []
    skipped = 0
    triple_to_filepath = {}

    for company, doc_type, accession in triples:
        cid = company_map.get(company)

        if cid is None:
            skipped += 1
            continue

        doc_type_norm = doc_type.lower().replace("-", "")
        raw_dir = "10k" if doc_type_norm == "10k" else doc_type
        ext = "htm" if doc_type_norm == "10k" else "pdf"
        filepath = f"data/01_raw/{raw_dir}/{company}/{accession}.{ext}"

        triple_to_filepath[(company, doc_type, accession)] = filepath

        mappings.append({
            "company_id": cid,
            "doc_type": doc_type,
            "filepath": filepath,
            "parse_status": "parsed",
        })

    session = SessionLocal()
    try:
        inserted = insert_ignore(session, Document, mappings, ["filepath"])

        all_filepaths = list(triple_to_filepath.values())
        docs = (
            session.query(Document.doc_id, Document.company_id, Document.doc_type, Document.filepath)
            .filter(Document.filepath.in_(all_filepaths))
            .all()
        )

        filepath_to_doc = {filepath: (doc_id, company_id, doc_type) for doc_id, company_id, doc_type, filepath in docs}
    finally:
        session.close()

    triple_to_doc_id = {}
    doc_rows = []

    for triple, filepath in triple_to_filepath.items():
        company, doc_type, accession = triple
        doc = filepath_to_doc.get(filepath)

        if doc is None:
            skipped += 1
            continue

        doc_id, cid, _ = doc
        triple_to_doc_id[triple] = doc_id

        doc_rows.append({
            "company": company,
            "doc_type": doc_type,
            "accession": accession,
            "doc_id": doc_id,
            "company_id": cid,
            "filepath": filepath,
        })

    write_csv(
        REFERENCE_DIR / "documents_key_map.csv",
        doc_rows,
        ["company", "doc_type", "accession", "doc_id", "company_id", "filepath"],
    )

    print(f"[documents] {len(triple_to_doc_id)} available, {inserted} newly inserted, {skipped} skipped in {time.time()-t0:.1f}s")
    return triple_to_doc_id


def load_sections(triple_to_doc_id, sections_dir=SECTIONS_DIR, batch_size=500):
    t0 = time.time()

    files = sorted([
        f for f in sections_dir.rglob("*.txt")
        if "FULL_DOCUMENT_FALLBACK" not in f.name
    ])

    session = SessionLocal()
    all_key_rows = []
    total_available = 0
    total_inserted = 0
    skipped = 0

    try:
        for i in range(0, len(files), batch_size):
            batch_files = files[i:i + batch_size]
            mappings = []
            wanted_pairs = []
            pair_to_stem = {}

            for f in batch_files:
                parts = f.stem.split("__")
                if len(parts) < 4:
                    skipped += 1
                    continue

                company = parts[0]
                doc_type = parts[1]
                accession = parts[2]
                section = "__".join(parts[3:])

                doc_id = triple_to_doc_id.get((company, doc_type, accession))

                if doc_id is None:
                    skipped += 1
                    continue

                text = f.read_text(encoding="utf-8", errors="replace")

                # Keep DB sections aligned with chunker.py.
                # Tiny sections under 50 tokens are skipped because they create invalid retrieval chunks.
                if len(ENCODER.encode(text)) < MIN_SECTION_TOKENS:
                    skipped += 1
                    continue

                mappings.append({
                    "doc_id": doc_id,
                    # 10-K section filenames already identify one contiguous
                    # section, so their legacy code is also a safe instance ID.
                    "section_instance_id": section,
                    "section_code": section,
                    "section_title": section.replace("_", " ").title(),
                    "section_text": text,
                    "char_count": len(text),
                })

                pair = (doc_id, section)
                wanted_pairs.append(pair)
                pair_to_stem[pair] = f.stem

            if not mappings:
                continue

            inserted = insert_ignore(
                session,
                Section,
                mappings,
                ["doc_id", "section_instance_id"],
            )
            total_inserted += inserted

            rows = (
                session.query(
                    Section.section_id,
                    Section.doc_id,
                    Section.section_instance_id,
                )
                .filter(
                    tuple_(Section.doc_id, Section.section_instance_id).in_(wanted_pairs)
                )
                .all()
            )

            for section_id, doc_id, section_instance_id in rows:
                pair = (doc_id, section_instance_id)
                stem = pair_to_stem.get(pair)

                if stem:
                    all_key_rows.append({
                        "stem": stem,
                        "section_id": section_id,
                        "doc_id": doc_id,
                    })
                    total_available += 1

            print(f"  sections batch {i // batch_size + 1}: {total_available} mapped so far")

    finally:
        session.close()

    write_csv(
        REFERENCE_DIR / "sections_key_map.csv",
        all_key_rows,
        ["stem", "section_id", "doc_id"],
    )

    print(f"[sections] {total_available} available, {total_inserted} newly inserted, {skipped} skipped in {time.time()-t0:.1f}s")
    return all_key_rows


if __name__ == "__main__":
    company_map = load_companies()
    load_annual_filings(company_map)
    triple_to_doc_id = load_documents(company_map)
    load_sections(triple_to_doc_id)
    print("Done. sections_key_map.csv is ready for chunks_bulk_loader.py")
