from __future__ import annotations

import argparse
import csv
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path


REFERENCE_DIR = Path("data/00_reference")
RAW_ESG_ROOT = Path("data/01_raw/sustainability")
VALID_TRACKER_STATUSES = {"downloaded", "not_found"}
VALID_PARSE_STATUSES = {"parsed", "ocr_required", "failed"}

Company = SustainabilityReport = Document = Section = Chunk = None


@dataclass
class LoadPlan:
    companies: dict[str, dict] = field(default_factory=dict)
    reports: list[dict] = field(default_factory=list)
    documents: list[dict] = field(default_factory=list)
    sections: list[dict] = field(default_factory=list)
    chunks: list[dict] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def resolve_path(raw_path: str | None) -> Path | None:
    if not raw_path:
        return None
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return Path.cwd() / path


def display_path(path: str | Path) -> str:
    path = Path(path)
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def normalize_status(status: str | None) -> str:
    raw = (status or "").strip().lower().replace("-", "_").replace(" ", "_")
    if raw == "downloaded":
        return "downloaded"
    if raw in {"not_found", "notfound"}:
        return "not_found"
    return raw


def parse_int(value: str | int | None) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return (value or "").strip().lower() in {"true", "1", "yes", "y"}


def row_quality_flags(row: dict) -> set[str]:
    raw_flags = (row.get("quality_flags") or "").strip()
    return {flag for flag in raw_flags.split("|") if flag}


def doc_quality_status_for_parse_row(row: dict) -> str:
    flags = row_quality_flags(row)
    if parse_bool(row.get("possible_wrong_doc_type")):
        return "exclude_from_esg_rag"
    if row.get("status") != "parsed" or flags & {
        "garbled_text",
        "low_readable_word_ratio",
        "low_text_per_page",
    }:
        return "needs_review"
    return "ok"


def rag_action_for_quality_status(status: str) -> str:
    if status == "exclude_from_esg_rag":
        return "exclude_from_esg_index"
    if status == "needs_review":
        return "manual_review_before_indexing"
    return "index_as_esg"


def doc_type_for_parse_row(row: dict) -> str:
    if parse_bool(row.get("possible_wrong_doc_type")):
        return "annual_report_with_esg"
    return "sustainability"


def extract_years(raw_year: str | None) -> list[int]:
    years = sorted({int(y) for y in re.findall(r"\b(?:19|20)\d{2}\b", raw_year or "")}, reverse=True)
    return years


def local_pdfs_by_ticker(raw_root: Path = RAW_ESG_ROOT) -> dict[str, list[Path]]:
    pdfs: dict[str, list[Path]] = defaultdict(list)
    if not raw_root.exists():
        return pdfs
    for pdf_file in raw_root.glob("*/*.pdf"):
        pdfs[pdf_file.parent.name.upper()].append(pdf_file)
    return pdfs


def is_bad_drive_link(value: str | None, ticker: str) -> bool:
    raw = (value or "").strip()
    if not raw:
        return True
    if raw.upper() == ticker.upper():
        return True
    if raw.startswith(("http://", "https://")):
        return False
    return not bool(re.fullmatch(r"[A-Za-z0-9_-]{20,}", raw))


def load_company_map(companies_path: Path) -> dict[str, dict]:
    companies: dict[str, dict] = {}
    for row in read_csv(companies_path):
        ticker = (row.get("ticker") or "").strip().upper()
        if ticker:
            companies[ticker] = row
    return companies


def choose_report_years(tracker: dict, local_pdfs: list[Path], anomalies: list[str]) -> list[int | None]:
    ticker = (tracker.get("ticker") or "").strip().upper()
    years = extract_years(tracker.get("report_year"))
    if not years:
        return [None]
    if len(years) == 1:
        return [years[0]]

    filename_years = {
        year
        for pdf_file in local_pdfs
        for year in extract_years(pdf_file.name)
        if year in years
    }
    if len(filename_years) > 1 and len(local_pdfs) >= len(filename_years):
        return sorted(filename_years, reverse=True)

    anomalies.append(
        f"{ticker}: tracker report_year has multiple years ({tracker.get('report_year')}); "
        f"using latest year {years[0]} for DB metadata"
    )
    return [years[0]]


def build_reports(tracker_rows: list[dict], companies: dict[str, dict], local_pdfs: dict[str, list[Path]], anomalies: list[str]) -> list[dict]:
    reports: list[dict] = []
    seen: set[tuple[str, int | None]] = set()

    for row in tracker_rows:
        ticker = (row.get("ticker") or "").strip().upper()
        if not ticker:
            anomalies.append("tracker row missing ticker")
            continue

        company = companies.get(ticker)
        if company is None:
            anomalies.append(f"{ticker}: tracker ticker missing from companies.csv")
            continue

        status = normalize_status(row.get("status"))
        if status not in VALID_TRACKER_STATUSES:
            anomalies.append(f"{ticker}: blank or invalid tracker status '{row.get('status', '')}'")
            continue

        if status == "downloaded" and not local_pdfs.get(ticker):
            anomalies.append(f"{ticker}: tracker says downloaded but no local PDF exists")

        if status == "downloaded" and not (row.get("format") or "").strip():
            anomalies.append(f"{ticker}: downloaded tracker row has blank format")

        if status == "downloaded" and is_bad_drive_link(row.get("drive_file_link"), ticker):
            anomalies.append(f"{ticker}: drive_file_link is blank or not a usable URL/file ID")

        for year in choose_report_years(row, local_pdfs.get(ticker, []), anomalies):
            key = (ticker, year)
            if key in seen:
                continue
            seen.add(key)
            reports.append(
                {
                    "ticker": ticker,
                    "company_id_csv": parse_int(company.get("company_id")),
                    "year": year,
                    "report_url": (row.get("drive_file_link") or "").strip() or None,
                    "format": (row.get("format") or "").strip() or None,
                    "download_status": status,
                }
            )

    return reports


def parse_doc_key(row: dict) -> tuple[str, str] | None:
    ticker = (row.get("ticker") or "").strip().upper()
    source_pdf = row.get("source_pdf") or row.get("filepath") or ""
    if not ticker or not source_pdf:
        return None
    return ticker, Path(source_pdf).stem


def build_documents(
    parse_rows: list[dict],
    companies: dict[str, dict],
    local_pdfs: dict[str, list[Path]],
    anomalies: list[str],
) -> list[dict]:
    documents: dict[tuple[str, str], dict] = {}

    for row in parse_rows:
        key = parse_doc_key(row)
        if key is None:
            anomalies.append(f"parse index row missing ticker/source_pdf: {row}")
            continue

        ticker, pdf_stem = key
        if ticker not in companies:
            anomalies.append(f"{ticker}: parse index ticker missing from companies.csv")
            continue

        status = row.get("status") or ""
        if status not in VALID_PARSE_STATUSES:
            anomalies.append(f"{ticker} {pdf_stem}: invalid parse status '{status}'")
            status = "failed"

        source_pdf = row.get("source_pdf") or ""
        if source_pdf and not (resolve_path(source_pdf) or Path()).exists():
            anomalies.append(f"{ticker} {pdf_stem}: source_pdf missing locally: {source_pdf}")

        documents[key] = {
            "ticker": ticker,
            "pdf_stem": pdf_stem,
            "company_id_csv": parse_int(companies[ticker].get("company_id")),
            "doc_type": doc_type_for_parse_row(row),
            "filepath": source_pdf,
            "parse_status": status,
            "quality_flags": row.get("quality_flags") or "",
            "possible_wrong_doc_type": parse_bool(row.get("possible_wrong_doc_type")),
            "doc_quality_status": doc_quality_status_for_parse_row(row),
            "rag_action": rag_action_for_quality_status(doc_quality_status_for_parse_row(row)),
        }

    for ticker, pdf_files in local_pdfs.items():
        if ticker not in companies:
            anomalies.append(f"{ticker}: local PDF ticker missing from companies.csv")
            continue
        for pdf_file in pdf_files:
            key = (ticker, pdf_file.stem)
            documents.setdefault(
                key,
                {
                    "ticker": ticker,
                    "pdf_stem": pdf_file.stem,
                    "company_id_csv": parse_int(companies[ticker].get("company_id")),
                    "doc_type": "sustainability",
                    "filepath": display_path(pdf_file),
                    "parse_status": "not_started",
                    "quality_flags": "",
                    "possible_wrong_doc_type": False,
                    "doc_quality_status": "needs_review",
                    "rag_action": "manual_review_before_indexing",
                },
            )

    return list(documents.values())


def build_sections(section_rows: list[dict], documents: list[dict], anomalies: list[str]) -> list[dict]:
    doc_keys = {(doc["ticker"], doc["pdf_stem"]) for doc in documents}
    sections: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    for row in section_rows:
        ticker = (row.get("ticker") or "").strip().upper()
        pdf_stem = (row.get("pdf_stem") or "").strip()
        section_code = (row.get("section_code") or "").strip()
        key = (ticker, pdf_stem, section_code)

        if not ticker or not pdf_stem or not section_code:
            anomalies.append(f"section index row missing ticker/pdf_stem/section_code: {row}")
            continue
        if (ticker, pdf_stem) not in doc_keys:
            anomalies.append(f"{ticker} {pdf_stem}: section has no matching document")
            continue
        if key in seen:
            anomalies.append(f"{ticker} {pdf_stem} {section_code}: duplicate section index row")
            continue
        seen.add(key)

        section_file = row.get("section_file") or ""
        path = resolve_path(section_file)
        if path is None or not path.exists():
            anomalies.append(f"{ticker} {pdf_stem} {section_code}: section file missing: {section_file}")
            continue

        text = path.read_text(encoding="utf-8", errors="replace")
        sections.append(
            {
                "ticker": ticker,
                "pdf_stem": pdf_stem,
                "section_code": section_code,
                "section_title": row.get("section_title") or section_code.replace("_", " ").title(),
                "section_text": text,
                "char_count": len(text),
                "source_start_char": parse_int(row.get("source_start_char")),
                "source_end_char": parse_int(row.get("source_end_char")),
                "page_start": parse_int(row.get("page_start")),
                "page_end": parse_int(row.get("page_end")),
            }
        )

    return sections


def build_chunks(chunk_rows: list[dict], sections: list[dict], anomalies: list[str]) -> list[dict]:
    section_keys = {(s["ticker"], s["pdf_stem"], s["section_code"]) for s in sections}
    chunks: list[dict] = []
    seen: set[tuple[str, str, str, int]] = set()

    for row in chunk_rows:
        ticker = (row.get("ticker") or "").strip().upper()
        pdf_stem = (row.get("pdf_stem") or "").strip()
        section_code = (row.get("section_code") or "").strip()
        chunk_index = parse_int(row.get("chunk_index"))

        if not ticker or not pdf_stem or not section_code or chunk_index is None:
            anomalies.append(f"chunk index row missing ticker/pdf_stem/section_code/chunk_index: {row}")
            continue

        key = (ticker, pdf_stem, section_code, chunk_index)
        section_key = (ticker, pdf_stem, section_code)
        if section_key not in section_keys:
            anomalies.append(f"{ticker} {pdf_stem} {section_code}: chunk has no matching section")
            continue
        if key in seen:
            anomalies.append(f"{ticker} {pdf_stem} {section_code} chunk {chunk_index}: duplicate chunk row")
            continue
        seen.add(key)

        token_count = parse_int(row.get("token_count"))
        if token_count is None or token_count < 100 or token_count > 600:
            anomalies.append(f"{ticker} {pdf_stem} {section_code} chunk {chunk_index}: invalid token_count {row.get('token_count')}")
            continue

        chunk_file = row.get("chunk_file") or ""
        path = resolve_path(chunk_file)
        if path is None or not path.exists():
            anomalies.append(f"{ticker} {pdf_stem} {section_code} chunk {chunk_index}: chunk file missing: {chunk_file}")
            continue

        chunks.append(
            {
                "ticker": ticker,
                "pdf_stem": pdf_stem,
                "section_code": section_code,
                "chunk_index": chunk_index,
                "chunk_text": path.read_text(encoding="utf-8", errors="replace"),
                "token_count": token_count,
                "doc_type": row.get("doc_type") or "sustainability",
                "doc_quality_status": row.get("doc_quality_status") or "needs_review",
                "rag_action": row.get("rag_action") or "manual_review_before_indexing",
                "quality_flags": row.get("quality_flags") or "",
                "source_start_char": parse_int(row.get("source_start_char")),
                "source_end_char": parse_int(row.get("source_end_char")),
                "page_start": parse_int(row.get("page_start")),
                "page_end": parse_int(row.get("page_end")),
                "citation_ready": parse_bool(row.get("citation_ready")),
            }
        )

    return chunks


def build_plan(args) -> LoadPlan:
    plan = LoadPlan()
    tracker_rows = read_csv(Path(args.tracker))
    parse_rows = read_csv(Path(args.parse_index))
    section_rows = read_csv(Path(args.sections_index))
    chunk_rows = read_csv(Path(args.chunks_index))
    plan.companies = load_company_map(Path(args.companies))
    local_pdfs = local_pdfs_by_ticker(Path(args.raw_root))

    plan.reports = build_reports(tracker_rows, plan.companies, local_pdfs, plan.anomalies)
    plan.documents = build_documents(parse_rows, plan.companies, local_pdfs, plan.anomalies)
    plan.sections = build_sections(section_rows, plan.documents, plan.anomalies)
    plan.chunks = build_chunks(chunk_rows, plan.sections, plan.anomalies)
    return plan


def print_plan(plan: LoadPlan) -> None:
    print("ESG DB load plan:")
    print(f"  sustainability_reports upsert candidates: {len(plan.reports)}")
    print(f"  documents upsert candidates: {len(plan.documents)}")
    print(f"  sections upsert candidates: {len(plan.sections)}")
    print(f"  chunks upsert candidates: {len(plan.chunks)}")
    print(f"  tracker/company rows available: {len(plan.companies)}")
    print(f"  anomalies: {len(plan.anomalies)}")
    for anomaly in plan.anomalies[:50]:
        print(f"  - {anomaly}")
    if len(plan.anomalies) > 50:
        print(f"  ... {len(plan.anomalies) - 50} more")


def load_db_dependencies():
    global Company, SustainabilityReport, Document, Section, Chunk

    try:
        from dotenv import load_dotenv
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from models import Company as _Company
        from models import SustainabilityReport as _SustainabilityReport
        from models import Document as _Document
        from models import Section as _Section
        from models import Chunk as _Chunk
    except ModuleNotFoundError as exc:
        missing = exc.name or str(exc)
        raise ModuleNotFoundError(
            f"Missing dependency '{missing}'. Install project requirements before "
            "running commit mode: pip install -r requirements.txt"
        ) from exc

    Company = _Company
    SustainabilityReport = _SustainabilityReport
    Document = _Document
    Section = _Section
    Chunk = _Chunk

    return load_dotenv, create_engine, sessionmaker


def get_session_factory():
    load_dotenv, create_engine, sessionmaker = load_db_dependencies()
    load_dotenv()
    db_url = os.getenv("DB_URL") or os.getenv("DATABASE_URL")
    if not db_url:
        raise EnvironmentError("DB_URL or DATABASE_URL is not set; commit mode cannot run.")
    engine = create_engine(db_url, future=True, pool_pre_ping=True)
    return sessionmaker(bind=engine, future=True)


def ensure_companies(session, companies: dict[str, dict]) -> dict[str, Company]:
    existing = {
        company.ticker.upper(): company
        for company in session.query(Company).all()
        if company.ticker
    }

    for ticker, row in companies.items():
        if ticker in existing:
            continue
        cik = (row.get("cik") or "").strip()
        name = (row.get("name") or row.get("company_name") or "").strip()
        if not cik or not name:
            continue
        company = Company(
            ticker=ticker,
            cik=cik,
            name=name,
            sector=(row.get("sector") or "").strip() or None,
            exchange=(row.get("exchange") or "").strip() or None,
        )
        session.add(company)
        session.flush()
        existing[ticker] = company

    return existing


def find_report(session, company_id: int, year: int | None) -> SustainabilityReport | None:
    query = session.query(SustainabilityReport).filter(SustainabilityReport.company_id == company_id)
    if year is None:
        query = query.filter(SustainabilityReport.year.is_(None))
    else:
        query = query.filter(SustainabilityReport.year == year)
    return query.first()


def apply_plan(plan: LoadPlan) -> dict[str, int]:
    SessionLocal = get_session_factory()
    counts = defaultdict(int)

    with SessionLocal() as session:
        companies = ensure_companies(session, plan.companies)

        for row in plan.reports:
            company = companies.get(row["ticker"])
            if company is None:
                counts["reports_skipped"] += 1
                continue
            report = find_report(session, company.company_id, row["year"])
            if report is None:
                report = SustainabilityReport(company_id=company.company_id, year=row["year"])
                session.add(report)
                counts["reports_inserted"] += 1
            else:
                counts["reports_updated"] += 1
            report.report_url = row["report_url"]
            report.format = row["format"]
            report.download_status = row["download_status"]

        session.flush()

        doc_map: dict[tuple[str, str], Document] = {}
        for row in plan.documents:
            company = companies.get(row["ticker"])
            if company is None:
                counts["documents_skipped"] += 1
                continue
            document = session.query(Document).filter(Document.filepath == row["filepath"]).first()
            if document is None:
                document = Document(
                    company_id=company.company_id,
                    doc_type=row["doc_type"],
                    filepath=row["filepath"],
                    parse_status=row["parse_status"],
                )
                session.add(document)
                counts["documents_inserted"] += 1
            else:
                counts["documents_updated"] += 1
                document.company_id = company.company_id
                document.doc_type = row["doc_type"]
                document.parse_status = row["parse_status"]
            document.quality_flags = row.get("quality_flags") or ""
            document.possible_wrong_doc_type = bool(row.get("possible_wrong_doc_type"))
            document.doc_quality_status = row.get("doc_quality_status")
            document.rag_action = row.get("rag_action")
            session.flush()
            doc_map[(row["ticker"], row["pdf_stem"])] = document

        section_map: dict[tuple[str, str, str], Section] = {}
        for row in plan.sections:
            document = doc_map.get((row["ticker"], row["pdf_stem"]))
            if document is None:
                counts["sections_skipped"] += 1
                continue
            section = (
                session.query(Section)
                .filter(
                    Section.doc_id == document.doc_id,
                    Section.section_code == row["section_code"],
                )
                .first()
            )
            if section is None:
                section = Section(doc_id=document.doc_id, section_code=row["section_code"])
                session.add(section)
                counts["sections_inserted"] += 1
            else:
                counts["sections_updated"] += 1
            section.section_title = row["section_title"]
            section.section_text = row["section_text"]
            section.char_count = row["char_count"]
            section.source_start_char = row.get("source_start_char")
            section.source_end_char = row.get("source_end_char")
            section.page_start = row.get("page_start")
            section.page_end = row.get("page_end")
            session.flush()
            section_map[(row["ticker"], row["pdf_stem"], row["section_code"])] = section

        for row in plan.chunks:
            section = section_map.get((row["ticker"], row["pdf_stem"], row["section_code"]))
            document = doc_map.get((row["ticker"], row["pdf_stem"]))
            company = companies.get(row["ticker"])
            if section is None or document is None or company is None:
                counts["chunks_skipped"] += 1
                continue
            chunk = (
                session.query(Chunk)
                .filter(
                    Chunk.section_id == section.section_id,
                    Chunk.chunk_index == row["chunk_index"],
                )
                .first()
            )
            if chunk is None:
                chunk = Chunk(section_id=section.section_id, chunk_index=row["chunk_index"])
                session.add(chunk)
                counts["chunks_inserted"] += 1
            else:
                counts["chunks_updated"] += 1

            chunk.doc_id = document.doc_id
            chunk.company_id = company.company_id
            chunk.doc_type = row["doc_type"]
            chunk.section_code = row["section_code"]
            chunk.chunk_text = row["chunk_text"]
            chunk.token_count = row["token_count"]
            chunk.doc_quality_status = row["doc_quality_status"]
            chunk.rag_action = row["rag_action"]
            chunk.quality_flags = row["quality_flags"]
            chunk.source_start_char = row["source_start_char"]
            chunk.source_end_char = row["source_end_char"]
            chunk.page_start = row["page_start"]
            chunk.page_end = row["page_end"]
            chunk.citation_ready = row["citation_ready"]

        session.commit()

    return dict(sorted(counts.items()))


def main():
    parser = argparse.ArgumentParser(description="Load ESG tracker, documents, sections, and chunks into PostgreSQL.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Build and print the ESG DB load plan without DB writes.")
    mode.add_argument("--commit", action="store_true", help="Write ESG rows to PostgreSQL idempotently.")
    parser.add_argument("--companies", default=str(REFERENCE_DIR / "companies.csv"))
    parser.add_argument("--tracker", default=str(REFERENCE_DIR / "sustainability_report_tracker.csv"))
    parser.add_argument("--parse-index", default=str(REFERENCE_DIR / "esg_parse_index.csv"))
    parser.add_argument("--sections-index", default=str(REFERENCE_DIR / "esg_sections_index.csv"))
    parser.add_argument("--chunks-index", default=str(REFERENCE_DIR / "esg_chunks_index.csv"))
    parser.add_argument("--raw-root", default=str(RAW_ESG_ROOT))
    args = parser.parse_args()

    plan = build_plan(args)
    print_plan(plan)

    if args.dry_run:
        print()
        print("Dry run complete. No DB writes performed.")
        return

    counts = apply_plan(plan)
    print()
    print("Commit complete:")
    for name, count in counts.items():
        print(f"  {name}: {count}")


if __name__ == "__main__":
    main()
