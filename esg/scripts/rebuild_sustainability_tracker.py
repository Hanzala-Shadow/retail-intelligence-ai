"""Rebuild sustainability_report_tracker.csv from the Drive manifest.

The tracker was hand-maintained and had drifted: it registered roughly four
recent years per company, while the Drive download had pulled the full
back-history. The result was 258 parsed PDFs with no tracker row, which
`esg_pipeline_qa.py` reports as `tracker_needs_cleanup`.

This rebuilds the file from `esg_drive_manifest.csv` so it holds exactly one
row per Drive PDF and nothing else. `not_found` rows for companies with no
report are NOT carried over -- the rebuilt tracker describes files that exist.
The previous file is git-tracked; recover those rows from history if the
policy changes back.

`report_year` is a single year, not a span. Multi-year filenames such as
`GES-GUESS INC-2020-2021.pdf` resolve to their latest year, matching the
fallback in `drive_to_db.choose_report_years`. A span in `report_year` would
make one PDF expand into several `sustainability_reports` rows and break the
one-row-per-PDF property this script exists to create. The full filename is
kept in `notes`, so the span is never lost.

Re-run after any Drive change:

    python esg/scripts/rebuild_sustainability_tracker.py --dry-run
    python esg/scripts/rebuild_sustainability_tracker.py
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter
from pathlib import Path

import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)
import config  # noqa: E402

FIELDNAMES = [
    "company_id",
    "ticker",
    "company_name",
    "report_year",
    "format",
    "drive_file_link",
    "status",
    "notes",
]
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
DRIVE_LINK = "https://drive.google.com/file/d/{file_id}/view"


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def years_in(name: str) -> list[int]:
    """Every year in a filename, newest first -- same rule as drive_to_db."""
    return sorted({int(y) for y in YEAR_RE.findall(name or "")}, reverse=True)


def build_rows(manifest: list[dict], companies: dict[str, dict]) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    problems: list[str] = []
    seen_year: Counter = Counter()

    for entry in manifest:
        ticker = (entry.get("ticker") or "").strip().upper()
        drive_name = (entry.get("drive_file_name") or "").strip()
        file_id = (entry.get("drive_file_id") or "").strip()

        company = companies.get(ticker)
        if company is None:
            problems.append(f"{ticker}: not in companies.csv ({drive_name})")
            continue
        if not file_id:
            problems.append(f"{ticker}: manifest row has no drive_file_id ({drive_name})")

        found = years_in(drive_name)
        if not found:
            problems.append(f"{ticker}: no year in filename ({drive_name})")
            report_year = ""
        else:
            report_year = str(found[0])
            seen_year[(ticker, found[0])] += 1

        note = drive_name
        if len(found) > 1:
            note = f"{drive_name} (filename spans {'-'.join(str(y) for y in sorted(found))})"

        rows.append({
            "company_id": (company.get("company_id") or "").strip(),
            "ticker": ticker,
            "company_name": (company.get("name") or "").strip(),
            "report_year": report_year,
            "format": "PDF",
            "drive_file_link": DRIVE_LINK.format(file_id=file_id) if file_id else "",
            "status": "downloaded",
            "notes": note,
        })

    # sustainability_reports is UNIQUE(company_id, year), so any collision here
    # is a row the DB cannot hold. Report it rather than silently dropping it.
    for (ticker, year), count in sorted(seen_year.items()):
        if count > 1:
            problems.append(
                f"{ticker} {year}: {count} PDFs resolve to the same report year; "
                f"sustainability_reports can hold only one"
            )

    rows.sort(key=lambda r: (int(r["company_id"] or 0), r["ticker"], r["report_year"]))
    return rows, problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=config.ESG_DRIVE_MANIFEST_CSV)
    parser.add_argument("--companies", type=Path, default=config.COMPANIES_CSV)
    parser.add_argument("--out", type=Path, default=config.SUSTAINABILITY_TRACKER_CSV)
    parser.add_argument("--dry-run", action="store_true", help="Print the summary without writing.")
    args = parser.parse_args()

    manifest = read_csv(args.manifest)
    companies = {
        (r.get("ticker") or "").strip().upper(): r
        for r in read_csv(args.companies)
        if (r.get("ticker") or "").strip()
    }

    rows, problems = build_rows(manifest, companies)

    previous = read_csv(args.out) if args.out.exists() else []
    print(f"manifest rows      : {len(manifest)}")
    print(f"tracker rows before: {len(previous)}")
    print(f"tracker rows after : {len(rows)}")
    print(f"distinct tickers   : {len({r['ticker'] for r in rows})}")
    print(f"report years       : {min((r['report_year'] for r in rows if r['report_year']), default='-')}"
          f" .. {max((r['report_year'] for r in rows if r['report_year']), default='-')}")

    if problems:
        print(f"\nissues ({len(problems)}):")
        for problem in problems:
            print(f"  - {problem}")

    if args.dry_run:
        print("\nDry run. Nothing written.")
        return

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
