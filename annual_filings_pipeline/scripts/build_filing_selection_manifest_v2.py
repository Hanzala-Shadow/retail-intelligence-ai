#!/usr/bin/env python3
"""Build the review-required FY2023–FY2025 filing-selection candidate manifest."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

EXCLUDED_TICKERS = {"APC", "ARKO", "XOM", "TBHC"}
SINGLE_2025_TICKERS = {"BOBS", "PEW", "PTRN"}
OUTPUT_COLUMNS = [
    "company_id",
    "ticker",
    "cik",
    "coverage_year",
    "filing_year",
    "filing_date",
    "accession_number",
    "document_period_end_date",
    "document_fiscal_year_focus",
    "form_type",
    "is_amendment",
    "source_url",
    "source_file",
    "source_sha256",
    "selection_method",
    "selection_status",
    "reviewer",
    "reviewed_at",
    "notes",
]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def normalize_ticker(value: str) -> str:
    return value.strip().upper()


def normalize_cik(value: str) -> str:
    return re.sub(r"\D", "", str(value)).zfill(10)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def resolve_source_path(row: dict[str, str], raw_root: Path | None) -> Path | None:
    candidates = []
    for key in ("resolved_path", "filepath"):
        value = row.get(key, "").strip()
        if value:
            candidates.append(Path(value))
            if raw_root:
                candidates.append(raw_root / normalize_ticker(row["ticker"]) / Path(value).name)
    return next((path for path in candidates if path.is_file()), None)


def period_end_iso(raw: str, fiscal_focus: str) -> str:
    value = re.sub(r"\s+", " ", raw.strip().replace(",", " "))
    if not value:
        return ""
    if not re.search(r"\b(?:19|20)\d{2}\b", value) and fiscal_focus.isdigit():
        value = f"{value} {fiscal_focus}"
    for fmt in ("%B %d %Y", "%b %d %Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.title(), fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def assign_candidate_years(rows_by_ticker: dict[str, list[dict[str, str]]]) -> None:
    for ticker, rows in rows_by_ticker.items():
        rows.sort(key=lambda row: (row["filing_date"], row["accession_number"]))
        if ticker in SINGLE_2025_TICKERS:
            if len(rows) != 1:
                raise ValueError(f"{ticker}: expected one filing, found {len(rows)}")
            rows[0]["candidate_coverage_year"] = "2025"
            rows[0]["selection_method"] = "approved_single_2025_company"
            continue
        if len(rows) != 3:
            raise ValueError(f"{ticker}: expected three filings, found {len(rows)}")
        for coverage_year, row in zip(("2023", "2024", "2025"), rows, strict=True):
            row["candidate_coverage_year"] = coverage_year
            row["selection_method"] = "approved_three_report_sequence_candidate"


def build(args: argparse.Namespace) -> tuple[list[dict[str, str]], dict[str, object]]:
    companies = read_csv(args.companies)
    if len(companies) != 190:
        raise ValueError(f"approved companies: expected 190 rows, found {len(companies)}")
    company_by_ticker = {normalize_ticker(row["ticker"]): row for row in companies}
    if len(company_by_ticker) != 190:
        raise ValueError("approved company tickers are not unique")
    if EXCLUDED_TICKERS & company_by_ticker.keys():
        raise ValueError("excluded ticker found in approved companies")
    if "YSWY" not in company_by_ticker:
        raise ValueError("YSWY is missing from approved companies")

    audit_rows = []
    for row in read_csv(args.audit):
        ticker = normalize_ticker(row["ticker"])
        if ticker in EXCLUDED_TICKERS:
            continue
        if ticker not in company_by_ticker:
            raise ValueError(f"audit ticker not approved: {ticker}")
        row["ticker"] = ticker
        audit_rows.append(row)
    if len(audit_rows) != 561:
        raise ValueError(f"retained audit: expected 561 rows, found {len(audit_rows)}")

    by_ticker: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in audit_rows:
        by_ticker[row["ticker"]].append(row)
    if set(by_ticker) != set(company_by_ticker) - {"YSWY"}:
        raise ValueError("filing-company set does not equal approved companies minus YSWY")
    assign_candidate_years(by_ticker)

    output_rows = []
    missing_source_count = 0
    period_parse_failures = 0
    for ticker in sorted(by_ticker):
        company = company_by_ticker[ticker]
        for row in by_ticker[ticker]:
            source_path = resolve_source_path(row, args.raw_root)
            source_sha256 = sha256_file(source_path) if source_path else ""
            if not source_path:
                missing_source_count += 1
            period_end = period_end_iso(
                row.get("period_end_date", ""),
                row.get("fiscal_year_focus", ""),
            )
            if row.get("period_end_date", "").strip() and not period_end:
                period_parse_failures += 1
            notes = []
            if not source_path:
                notes.append("source file unavailable during candidate build")
            if row.get("period_end_date", "").strip() and not period_end:
                notes.append(f"unparsed period end: {row['period_end_date']}")
            output_rows.append(
                {
                    "company_id": company["company_id"],
                    "ticker": ticker,
                    "cik": normalize_cik(company["cik"]),
                    "coverage_year": row["candidate_coverage_year"],
                    "filing_year": row["filing_date"][:4],
                    "filing_date": row["filing_date"],
                    "accession_number": row["accession_number"],
                    "document_period_end_date": period_end,
                    "document_fiscal_year_focus": row.get("fiscal_year_focus", ""),
                    "form_type": "10-K",
                    "is_amendment": "false",
                    "source_url": "",
                    "source_file": str(source_path or row.get("resolved_path") or row.get("filepath") or ""),
                    "source_sha256": source_sha256,
                    "selection_method": row["selection_method"],
                    "selection_status": "review_required",
                    "reviewer": "",
                    "reviewed_at": "",
                    "notes": "; ".join(notes),
                }
            )

    coverage_counts = Counter(row["coverage_year"] for row in output_rows)
    duplicate_pairs = len(output_rows) - len(
        {(row["ticker"], row["coverage_year"]) for row in output_rows}
    )
    if coverage_counts != Counter({"2023": 186, "2024": 186, "2025": 189}):
        raise ValueError(f"unexpected candidate coverage counts: {dict(coverage_counts)}")
    if duplicate_pairs:
        raise ValueError(f"duplicate company/coverage-year pairs: {duplicate_pairs}")

    summary = {
        "status": "REVIEW_REQUIRED",
        "approved_companies": len(companies),
        "companies_with_filings": len(by_ticker),
        "company_without_filings": "YSWY",
        "candidate_filings": len(output_rows),
        "coverage_counts": dict(sorted(coverage_counts.items())),
        "duplicate_company_coverage_pairs": duplicate_pairs,
        "missing_source_files": missing_source_count,
        "unparsed_period_end_dates": period_parse_failures,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    return output_rows, summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--companies", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path)
    args = parser.parse_args()
    rows, summary = build(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(args.output)
    summary_path = args.output.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
