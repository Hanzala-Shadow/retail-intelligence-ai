from __future__ import annotations

import argparse
import csv
import os
import re
from collections import defaultdict
from pathlib import Path


QA_FIELDS = [
    "company_id",
    "ticker",
    "company_name",
    "tracker_status",
    "report_year",
    "format",
    "drive_file_link",
    "pdf_file",
    "pdf_count",
    "parsed_count",
    "ocr_required_count",
    "failed_parse_count",
    "doc_quality_status",
    "rag_action",
    "quality_flag_count",
    "wrong_doc_type_count",
    "garbled_text_count",
    "low_text_quality_count",
    "section_count",
    "chunk_count",
    "citation_ready_chunk_count",
    "missing_citation_metadata_count",
    "min_chunk_tokens",
    "max_chunk_tokens",
    "status",
    "notes",
]

VALID_PARSE_STATUSES = {"parsed", "ocr_required", "failed"}


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=QA_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp_path, path)


def resolve_path(raw_path: str | None) -> Path | None:
    if not raw_path:
        return None
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return Path.cwd() / path


def normalize_status(status: str | None) -> str:
    raw = (status or "").strip().lower().replace("-", "_").replace(" ", "_")
    if raw == "downloaded":
        return "downloaded"
    if raw in {"not_found", "notfound"}:
        return "not_found"
    return raw


def is_bad_drive_link(value: str | None, ticker: str) -> bool:
    raw = (value or "").strip()
    if not raw:
        return True
    if raw.upper() == ticker.upper():
        return True
    if raw.startswith(("http://", "https://")):
        return False
    # Accept plausible Drive IDs, but flag short ticker-like placeholders.
    return not bool(re.fullmatch(r"[A-Za-z0-9_-]{20,}", raw))


def count_local_pdfs(raw_root: Path) -> dict[str, list[Path]]:
    pdfs: dict[str, list[Path]] = defaultdict(list)
    if not raw_root.exists():
        return pdfs
    for pdf_file in raw_root.glob("*/*.pdf"):
        pdfs[pdf_file.parent.name.upper()].append(pdf_file)
    return pdfs


def group_by(rows: list[dict], key: str) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[(row.get(key) or "").strip().upper()].append(row)
    return grouped


def parse_int(value: str | int | None) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def chunk_token_counts(rows: list[dict]) -> list[int]:
    counts: list[int] = []
    for row in rows:
        value = parse_int(row.get("token_count"))
        if value is not None:
            counts.append(value)
    return counts


def parse_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return (value or "").strip().lower() in {"true", "1", "yes", "y"}


def row_quality_flags(row: dict) -> set[str]:
    raw_flags = (row.get("quality_flags") or "").strip()
    return {flag for flag in raw_flags.split("|") if flag}


def extract_years(raw_year: str | None) -> list[int]:
    return sorted({int(y) for y in re.findall(r"\b(?:20|19)\d{2}\b", raw_year or "")})


def row_years(row: dict, fields: tuple[str, ...]) -> set[int]:
    text = " ".join(row.get(field, "") or "" for field in fields)
    return set(extract_years(text))


def pdf_stem_from_parse_row(row: dict) -> str:
    source_pdf = row.get("source_pdf") or row.get("pdf_file") or ""
    return Path(source_pdf).stem if source_pdf else Path(row.get("pdf_file") or "").stem


def filter_parse_rows_for_tracker(tracker: dict, parse_rows: list[dict]) -> list[dict]:
    years = set(extract_years(tracker.get("report_year")))
    if not years:
        return parse_rows
    return [
        row
        for row in parse_rows
        if row_years(row, ("pdf_file", "source_pdf", "parsed_text_file")) & years
    ]


def filter_paths_for_tracker(tracker: dict, paths: list[Path]) -> list[Path]:
    years = set(extract_years(tracker.get("report_year")))
    if not years:
        return paths
    return [path for path in paths if set(extract_years(path.name)) & years]


def group_by_doc(rows: list[dict]) -> dict[tuple[str, str], list[dict]]:
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        ticker = (row.get("ticker") or "").strip().upper()
        pdf_stem = (row.get("pdf_stem") or "").strip()
        if ticker and pdf_stem:
            grouped[(ticker, pdf_stem)].append(row)
    return grouped


def quality_status_for_parse_rows(parse_rows: list[dict]) -> str:
    if not parse_rows:
        return ""
    if any(parse_bool(row.get("possible_wrong_doc_type")) for row in parse_rows):
        return "exclude_from_esg_rag"
    for row in parse_rows:
        flags = row_quality_flags(row)
        if row.get("status") != "parsed" or flags & {
            "garbled_text",
            "low_readable_word_ratio",
            "low_text_per_page",
        }:
            return "needs_review"
    return "ok"


def rag_action_for_quality_status(quality_status: str, tracker_status: str) -> str:
    if tracker_status == "not_found":
        return "not_applicable"
    if quality_status == "exclude_from_esg_rag":
        return "exclude_from_esg_index"
    if quality_status == "needs_review":
        return "manual_review_before_indexing"
    if quality_status == "ok":
        return "index_as_esg"
    return "manual_review_before_indexing"


def missing_index_files(rows: list[dict], path_field: str) -> int:
    missing = 0
    for row in rows:
        path = resolve_path(row.get(path_field))
        if path is None or not path.exists():
            missing += 1
    return missing


def has_multi_year(raw_year: str | None) -> bool:
    years = re.findall(r"\b(?:20|19)\d{2}\b", raw_year or "")
    return len(set(years)) > 1


def load_company_map(companies_path: Path) -> dict[str, dict]:
    companies = {}
    for row in read_csv(companies_path):
        ticker = (row.get("ticker") or "").strip().upper()
        if ticker:
            companies[ticker] = row
    return companies


def status_for_row(
    tracker_status: str,
    pdf_count: int,
    parsed_count: int,
    ocr_required_count: int,
    failed_parse_count: int,
    section_count: int,
    chunk_count: int,
    invalid_chunk_count: int,
    doc_quality_status: str,
    missing_citation_metadata_count: int,
) -> str:
    if tracker_status == "not_found":
        return "not_found"
    if tracker_status != "downloaded":
        return "tracker_needs_cleanup"
    if pdf_count == 0:
        return "missing_pdf"
    if doc_quality_status in {"exclude_from_esg_rag", "needs_review"}:
        return "needs_review"
    if chunk_count > 0 and missing_citation_metadata_count > 0:
        return "needs_review"
    if parsed_count > 0 and section_count > 0 and chunk_count > 0 and invalid_chunk_count == 0:
        return "complete"
    if parsed_count == 0 and ocr_required_count > 0 and failed_parse_count == 0:
        return "ocr_required"
    if parsed_count == 0 and failed_parse_count > 0 and ocr_required_count == 0:
        return "parse_failed"
    return "incomplete"


def summarize_counts(rows: list[dict], field: str) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[row.get(field, "")] += 1
    return dict(sorted(counts.items()))


def print_priority_fixes(rows: list[dict], cleanup_tickers: list[str]) -> None:
    print()
    print("Priority fixes:")

    priorities = [
        (
            "possible wrong document type",
            [
                r["ticker"]
                for r in rows
                if parse_int(r.get("wrong_doc_type_count")) and parse_int(r.get("wrong_doc_type_count")) > 0
            ],
        ),
        ("downloaded but no local PDF", [r["ticker"] for r in rows if r["status"] == "missing_pdf"]),
        (
            "parsed but zero sections",
            [
                r["ticker"]
                for r in rows
                if parse_int(r["parsed_count"]) and not parse_int(r["section_count"])
            ],
        ),
        (
            "sections but zero chunks",
            [
                r["ticker"]
                for r in rows
                if parse_int(r["section_count"]) and not parse_int(r["chunk_count"])
            ],
        ),
        (
            "invalid chunk token counts",
            [
                r["ticker"]
                for r in rows
                if "invalid chunk token count" in r.get("notes", "")
            ],
        ),
        (
            "chunks missing citation metadata",
            [
                r["ticker"]
                for r in rows
                if parse_int(r.get("missing_citation_metadata_count"))
                and parse_int(r.get("missing_citation_metadata_count")) > 0
            ],
        ),
        ("tracker cleanup issues", cleanup_tickers),
    ]

    for title, tickers in priorities:
        unique = sorted(set(tickers))
        preview = ", ".join(unique[:20])
        if len(unique) > 20:
            preview += f", ... {len(unique) - 20} more"
        print(f"  {title}: {len(unique)}" + (f" ({preview})" if preview else ""))


def run(
    out: str | Path,
    tracker_path: str | Path = "data/00_reference/sustainability_report_tracker.csv",
    companies_path: str | Path = "data/00_reference/companies.csv",
    parse_index_path: str | Path = "data/00_reference/esg_parse_index.csv",
    sections_index_path: str | Path = "data/00_reference/esg_sections_index.csv",
    chunks_index_path: str | Path = "data/00_reference/esg_chunks_index.csv",
    raw_root: str | Path = "data/01_raw/sustainability",
) -> list[dict]:
    out_path = Path(out)
    tracker_rows = read_csv(Path(tracker_path))
    company_map = load_company_map(Path(companies_path))
    parse_rows = read_csv(Path(parse_index_path))
    section_rows = read_csv(Path(sections_index_path))
    chunk_rows = read_csv(Path(chunks_index_path))
    local_pdfs = count_local_pdfs(Path(raw_root))

    parse_by_ticker = group_by(parse_rows, "ticker")
    sections_by_ticker = group_by(section_rows, "ticker")
    chunks_by_ticker = group_by(chunk_rows, "ticker")
    sections_by_doc = group_by_doc(section_rows)
    chunks_by_doc = group_by_doc(chunk_rows)
    tracker_by_ticker = group_by(tracker_rows, "ticker")

    rows: list[dict] = []
    cleanup_tickers: list[str] = []

    for tracker in tracker_rows:
        ticker = (tracker.get("ticker") or "").strip().upper()
        if not ticker:
            continue

        company = company_map.get(ticker, {})
        tracker_status = normalize_status(tracker.get("status"))
        ticker_parse_rows = filter_parse_rows_for_tracker(
            tracker,
            parse_by_ticker.get(ticker, []),
        )
        matched_pdf_stems = {pdf_stem_from_parse_row(row) for row in ticker_parse_rows}
        ticker_section_rows = [
            row
            for pdf_stem in matched_pdf_stems
            for row in sections_by_doc.get((ticker, pdf_stem), [])
        ]
        ticker_chunk_rows = [
            row
            for pdf_stem in matched_pdf_stems
            for row in chunks_by_doc.get((ticker, pdf_stem), [])
        ]
        token_counts = chunk_token_counts(ticker_chunk_rows)
        invalid_chunk_count = sum(1 for count in token_counts if count < 100 or count > 600)
        citation_ready_chunk_count = sum(
            1
            for row in ticker_chunk_rows
            if parse_bool(row.get("citation_ready"))
        )
        missing_citation_metadata_count = max(
            len(ticker_chunk_rows) - citation_ready_chunk_count,
            0,
        )

        parsed_count = sum(1 for row in ticker_parse_rows if row.get("status") == "parsed")
        ocr_required_count = sum(1 for row in ticker_parse_rows if row.get("status") == "ocr_required")
        failed_parse_count = sum(1 for row in ticker_parse_rows if row.get("status") == "failed")
        doc_quality_status = quality_status_for_parse_rows(ticker_parse_rows)
        rag_action = rag_action_for_quality_status(doc_quality_status, tracker_status)
        quality_flag_count = sum(1 for row in ticker_parse_rows if row_quality_flags(row))
        wrong_doc_type_count = sum(1 for row in ticker_parse_rows if parse_bool(row.get("possible_wrong_doc_type")))
        garbled_text_count = sum(1 for row in ticker_parse_rows if "garbled_text" in row_quality_flags(row))
        low_text_quality_count = sum(
            1
            for row in ticker_parse_rows
            if row_quality_flags(row) & {"low_readable_word_ratio", "low_text_per_page"}
        )
        bad_parse_status = [
            row.get("status", "")
            for row in ticker_parse_rows
            if row.get("status") not in VALID_PARSE_STATUSES
        ]

        notes: list[str] = []
        if not company:
            notes.append("ticker missing from companies.csv")
            cleanup_tickers.append(ticker)
        if tracker_status not in {"downloaded", "not_found"}:
            notes.append("blank or invalid tracker status")
            cleanup_tickers.append(ticker)
        if tracker_status == "downloaded" and not (tracker.get("format") or "").strip():
            notes.append("downloaded row has blank format")
            cleanup_tickers.append(ticker)
        if tracker_status == "downloaded" and is_bad_drive_link(tracker.get("drive_file_link"), ticker):
            notes.append("drive_file_link is blank or not a usable URL/file ID")
            cleanup_tickers.append(ticker)
        if has_multi_year(tracker.get("report_year")):
            notes.append("report_year contains multiple years; DB loader uses latest year unless files clearly split by year")
        if bad_parse_status:
            notes.append(f"invalid parse status values: {sorted(set(bad_parse_status))}")
        if wrong_doc_type_count:
            notes.append("possible 10-K or SEC filing in sustainability folder; exclude from ESG RAG")
        if garbled_text_count:
            notes.append("garbled parsed text")
        if any("low_readable_word_ratio" in row_quality_flags(row) for row in ticker_parse_rows):
            notes.append("low readable word ratio")
        if any("low_text_per_page" in row_quality_flags(row) for row in ticker_parse_rows):
            notes.append("low text per page")
        missing_text = missing_index_files(ticker_parse_rows, "parsed_text_file")
        if missing_text:
            notes.append(f"missing parsed text files: {missing_text}")
        missing_sections = missing_index_files(ticker_section_rows, "section_file")
        if missing_sections:
            notes.append(f"missing section files: {missing_sections}")
        missing_chunks = missing_index_files(ticker_chunk_rows, "chunk_file")
        if missing_chunks:
            notes.append(f"missing chunk files: {missing_chunks}")
        if invalid_chunk_count:
            notes.append(f"invalid chunk token count rows: {invalid_chunk_count}")
        if missing_citation_metadata_count:
            notes.append(f"chunks missing page/span citation metadata: {missing_citation_metadata_count}")

        matching_pdfs = filter_paths_for_tracker(tracker, local_pdfs.get(ticker, []))
        pdf_count = len(matching_pdfs)
        status = status_for_row(
            tracker_status,
            pdf_count,
            parsed_count,
            ocr_required_count,
            failed_parse_count,
            len(ticker_section_rows),
            len(ticker_chunk_rows),
            invalid_chunk_count,
            doc_quality_status,
            missing_citation_metadata_count,
        )

        rows.append(
            {
                "company_id": tracker.get("company_id") or company.get("company_id", ""),
                "ticker": ticker,
                "company_name": tracker.get("company_name") or company.get("name", ""),
                "tracker_status": tracker.get("status", ""),
                "report_year": tracker.get("report_year", ""),
                "format": tracker.get("format", ""),
                "drive_file_link": tracker.get("drive_file_link", ""),
                "pdf_file": "|".join(sorted(row.get("pdf_file", "") for row in ticker_parse_rows if row.get("pdf_file"))),
                "pdf_count": pdf_count,
                "parsed_count": parsed_count,
                "ocr_required_count": ocr_required_count,
                "failed_parse_count": failed_parse_count,
                "doc_quality_status": doc_quality_status,
                "rag_action": rag_action,
                "quality_flag_count": quality_flag_count,
                "wrong_doc_type_count": wrong_doc_type_count,
                "garbled_text_count": garbled_text_count,
                "low_text_quality_count": low_text_quality_count,
                "section_count": len(ticker_section_rows),
                "chunk_count": len(ticker_chunk_rows),
                "citation_ready_chunk_count": citation_ready_chunk_count,
                "missing_citation_metadata_count": missing_citation_metadata_count,
                "min_chunk_tokens": min(token_counts) if token_counts else "",
                "max_chunk_tokens": max(token_counts) if token_counts else "",
                "status": status,
                "notes": "; ".join(dict.fromkeys(notes)),
            }
        )

    tracker_tickers = set(tracker_by_ticker)
    chunk_tickers = set(chunks_by_ticker)
    for ticker in sorted(chunk_tickers - tracker_tickers):
        company = company_map.get(ticker, {})
        ticker_chunk_rows = chunks_by_ticker.get(ticker, [])
        ticker_parse_rows = parse_by_ticker.get(ticker, [])
        token_counts = chunk_token_counts(ticker_chunk_rows)
        wrong_doc_type_count = sum(1 for row in ticker_parse_rows if parse_bool(row.get("possible_wrong_doc_type")))
        garbled_text_count = sum(1 for row in ticker_parse_rows if "garbled_text" in row_quality_flags(row))
        low_text_quality_count = sum(
            1
            for row in ticker_parse_rows
            if row_quality_flags(row) & {"low_readable_word_ratio", "low_text_per_page"}
        )
        rows.append(
            {
                "company_id": company.get("company_id", ""),
                "ticker": ticker,
                "company_name": company.get("name", ""),
                "tracker_status": "",
                "report_year": "",
                "format": "",
                "drive_file_link": "",
                "pdf_file": "|".join(sorted(row.get("pdf_file", "") for row in ticker_parse_rows if row.get("pdf_file"))),
                "pdf_count": len(local_pdfs.get(ticker, [])),
                "parsed_count": sum(1 for row in ticker_parse_rows if row.get("status") == "parsed"),
                "ocr_required_count": 0,
                "failed_parse_count": 0,
                "doc_quality_status": quality_status_for_parse_rows(ticker_parse_rows),
                "rag_action": rag_action_for_quality_status(
                    quality_status_for_parse_rows(ticker_parse_rows),
                    "",
                ),
                "quality_flag_count": sum(1 for row in ticker_parse_rows if row_quality_flags(row)),
                "wrong_doc_type_count": wrong_doc_type_count,
                "garbled_text_count": garbled_text_count,
                "low_text_quality_count": low_text_quality_count,
                "section_count": len(sections_by_ticker.get(ticker, [])),
                "chunk_count": len(ticker_chunk_rows),
                "citation_ready_chunk_count": sum(1 for row in ticker_chunk_rows if parse_bool(row.get("citation_ready"))),
                "missing_citation_metadata_count": sum(1 for row in ticker_chunk_rows if not parse_bool(row.get("citation_ready"))),
                "min_chunk_tokens": min(token_counts) if token_counts else "",
                "max_chunk_tokens": max(token_counts) if token_counts else "",
                "status": "tracker_needs_cleanup",
                "notes": "ESG chunks exist but ticker has no tracker row",
            }
        )
        cleanup_tickers.append(ticker)

    rows = sorted(rows, key=lambda r: (r["status"], r["ticker"]))
    write_csv(out_path, rows)

    print(f"Wrote ESG QA report: {out_path}")
    print("Status counts:")
    for status, count in summarize_counts(rows, "status").items():
        print(f"  {status}: {count}")
    print_priority_fixes(rows, cleanup_tickers)
    return rows


def main():
    parser = argparse.ArgumentParser(description="Create ESG pipeline QA summary CSV.")
    parser.add_argument("--out", default="data/00_reference/esg_pipeline_qa.csv")
    parser.add_argument("--tracker", default="data/00_reference/sustainability_report_tracker.csv")
    parser.add_argument("--companies", default="data/00_reference/companies.csv")
    parser.add_argument("--parse-index", default="data/00_reference/esg_parse_index.csv")
    parser.add_argument("--sections-index", default="data/00_reference/esg_sections_index.csv")
    parser.add_argument("--chunks-index", default="data/00_reference/esg_chunks_index.csv")
    parser.add_argument("--raw-root", default="data/01_raw/sustainability")
    args = parser.parse_args()

    run(
        out=args.out,
        tracker_path=args.tracker,
        companies_path=args.companies,
        parse_index_path=args.parse_index,
        sections_index_path=args.sections_index,
        chunks_index_path=args.chunks_index,
        raw_root=args.raw_root,
    )


if __name__ == "__main__":
    main()
