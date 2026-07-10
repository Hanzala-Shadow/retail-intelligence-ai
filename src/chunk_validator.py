import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

import tiktoken

MIN_TOKENS = 50
MAX_TOKENS = 500
ENCODING = "cl100k_base"

CHUNKS_INDEX = Path("data/00_reference/chunks_index.csv")
CHUNKS_DIR = Path("data/04_chunks/10k")
COMPANIES_CSV = Path("data/00_reference/companies.csv")
FILINGS_CSV = Path("data/00_reference/filings.csv")

FLAGGED_REPORT = Path(
    "data/00_reference/chunk_qa_report.csv"
)
COMPANY_REPORT = Path(
    "data/00_reference/chunk_qa_company_summary.csv"
)

REQUIRED_COLUMNS = {
    "chunk_id",
    "company",
    "doc_type",
    "accession",
    "section",
    "chunk_index",
    "token_count",
    "char_count",
    "file",
}

MAJOR_SECTIONS = (
    "Item_1",
    "Item_1A",
    "Item_7",
    "Item_8",
)


def read_csv(path):
    with path.open(
        newline="",
        encoding="utf-8-sig",
    ) as file:
        return list(csv.DictReader(file))


def encode_count(encoder, text):
    return len(
        encoder.encode(
            text,
            disallowed_special=(),
        )
    )


def main():
    if not CHUNKS_INDEX.exists():
        raise FileNotFoundError(
            f"Missing chunks index: {CHUNKS_INDEX}"
        )

    if not CHUNKS_DIR.exists():
        raise FileNotFoundError(
            f"Missing chunks directory: {CHUNKS_DIR}"
        )

    rows = read_csv(CHUNKS_INDEX)

    print(f"Total chunks to validate: {len(rows)}")

    if not rows:
        raise RuntimeError("Chunks index is empty")

    missing_columns = (
        REQUIRED_COLUMNS - set(rows[0].keys())
    )

    if missing_columns:
        raise RuntimeError(
            "Missing index columns: "
            f"{sorted(missing_columns)}"
        )

    encoder = tiktoken.get_encoding(ENCODING)

    seen_ids = set()
    seen_keys = set()
    indexed_paths = set()
    document_keys = set()
    section_keys = set()

    company_totals = Counter()
    company_issue_totals = Counter()
    company_min_tokens = {}
    company_max_tokens = {}

    major_coverage = {
        code: set()
        for code in MAJOR_SECTIONS
    }

    flagged_rows = []
    actual_token_counts = []

    counters = Counter()

    for number, row in enumerate(rows, 1):
        issues = []

        chunk_id = row["chunk_id"].strip()
        company = row["company"].strip()
        accession = row["accession"].strip()
        section = row["section"].strip()

        try:
            chunk_index = int(row["chunk_index"])
        except (TypeError, ValueError):
            chunk_index = -1
            issues.append("invalid_chunk_index")

        try:
            recorded_tokens = int(row["token_count"])
        except (TypeError, ValueError):
            recorded_tokens = -1
            issues.append("invalid_token_count")

        document_key = (company, accession)
        section_key = (
            company,
            accession,
            section,
        )
        composite_key = (
            company,
            accession,
            section,
            chunk_index,
        )

        document_keys.add(document_key)
        section_keys.add(section_key)
        company_totals[company] += 1

        if section in major_coverage:
            major_coverage[section].add(
                document_key
            )

        if chunk_id in seen_ids:
            issues.append("duplicate_chunk_id")
            counters["duplicate_chunk_ids"] += 1
        else:
            seen_ids.add(chunk_id)

        if composite_key in seen_keys:
            issues.append("duplicate_composite_key")
            counters["duplicate_composite_keys"] += 1
        else:
            seen_keys.add(composite_key)

        chunk_path = Path(row["file"])
        resolved_path = chunk_path.resolve()
        indexed_paths.add(resolved_path)

        if not chunk_path.exists():
            issues.append("missing_file")
            counters["missing_files"] += 1
            actual_tokens = None
        else:
            if (
                chunk_path.parent.resolve()
                != CHUNKS_DIR.resolve()
            ):
                issues.append("bad_file_path")
                counters["bad_paths"] += 1

            text = chunk_path.read_text(
                encoding="utf-8",
                errors="replace",
            )

            if not text.strip():
                issues.append("empty_file")
                counters["empty_files"] += 1

            actual_tokens = encode_count(
                encoder,
                text,
            )
            actual_token_counts.append(
                actual_tokens
            )

            if actual_tokens != recorded_tokens:
                issues.append("token_mismatch")
                counters["token_mismatches"] += 1

            if actual_tokens < MIN_TOKENS:
                issues.append("too_short")
                counters["too_short"] += 1

            if actual_tokens > MAX_TOKENS:
                issues.append("too_long")
                counters["too_long"] += 1

            current_min = company_min_tokens.get(
                company
            )
            current_max = company_max_tokens.get(
                company
            )

            company_min_tokens[company] = (
                actual_tokens
                if current_min is None
                else min(current_min, actual_tokens)
            )
            company_max_tokens[company] = (
                actual_tokens
                if current_max is None
                else max(current_max, actual_tokens)
            )

        if issues:
            company_issue_totals[company] += 1
            flagged_rows.append({
                "chunk_id": chunk_id,
                "company": company,
                "accession": accession,
                "section": section,
                "chunk_index": chunk_index,
                "recorded_token_count": (
                    recorded_tokens
                ),
                "actual_token_count": (
                    ""
                    if actual_tokens is None
                    else actual_tokens
                ),
                "issues": ";".join(issues),
                "file": row["file"],
            })

        if number % 10000 == 0:
            print(
                f"Validated {number}/{len(rows)}"
            )

    disk_paths = {
        path.resolve()
        for path in CHUNKS_DIR.glob("*.txt")
    }

    orphan_paths = disk_paths - indexed_paths
    counters["orphan_files"] = len(orphan_paths)

    companies = read_csv(COMPANIES_CSV)
    filings = read_csv(FILINGS_CSV)

    all_companies = {
        row["ticker"].strip()
        for row in companies
        if row.get("ticker", "").strip()
    }
    filing_companies = {
        row["ticker"].strip()
        for row in filings
        if row.get("ticker", "").strip()
    }
    chunked_companies = set(company_totals)

    zero_chunk_companies = (
        all_companies - chunked_companies
    )
    expected_zero_chunk_companies = (
        all_companies - filing_companies
    )
    unexpected_zero_chunk_companies = (
        zero_chunk_companies
        - expected_zero_chunk_companies
    )

    FLAGGED_REPORT.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    flagged_fields = [
        "chunk_id",
        "company",
        "accession",
        "section",
        "chunk_index",
        "recorded_token_count",
        "actual_token_count",
        "issues",
        "file",
    ]

    with FLAGGED_REPORT.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=flagged_fields,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(flagged_rows)

    company_fields = [
        "company",
        "total_chunks",
        "minimum_tokens",
        "maximum_tokens",
        "issue_chunks",
        "has_issues",
    ]

    with COMPANY_REPORT.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=company_fields,
            lineterminator="\n",
        )
        writer.writeheader()

        for company in sorted(
            chunked_companies
        ):
            issue_count = (
                company_issue_totals[company]
            )
            writer.writerow({
                "company": company,
                "total_chunks": (
                    company_totals[company]
                ),
                "minimum_tokens": (
                    company_min_tokens[company]
                ),
                "maximum_tokens": (
                    company_max_tokens[company]
                ),
                "issue_chunks": issue_count,
                "has_issues": (
                    "true"
                    if issue_count
                    else "false"
                ),
            })

    print()
    print("STRICT CHUNK VALIDATION RESULTS")
    print(f"Index rows: {len(rows)}")
    print(f"Disk files: {len(disk_paths)}")
    print(f"Unique chunk IDs: {len(seen_ids)}")
    print(f"Documents represented: {len(document_keys)}")
    print(f"Sections represented: {len(section_keys)}")
    print(
        "Minimum tokens:",
        min(actual_token_counts),
    )
    print(
        "Maximum tokens:",
        max(actual_token_counts),
    )
    print(
        "Average tokens:",
        f"{sum(actual_token_counts) / len(actual_token_counts):.2f}",
    )
    print(
        "Duplicate chunk IDs:",
        counters["duplicate_chunk_ids"],
    )
    print(
        "Duplicate composite keys:",
        counters["duplicate_composite_keys"],
    )
    print(
        "Missing files:",
        counters["missing_files"],
    )
    print(
        "Orphan files:",
        counters["orphan_files"],
    )
    print(
        "Empty files:",
        counters["empty_files"],
    )
    print(
        "Bad paths:",
        counters["bad_paths"],
    )
    print(
        "Token mismatches:",
        counters["token_mismatches"],
    )
    print(
        f"Too short (<{MIN_TOKENS}):",
        counters["too_short"],
    )
    print(
        f"Too long (>{MAX_TOKENS}):",
        counters["too_long"],
    )

    print()
    print("MAJOR-SECTION DOCUMENT COVERAGE")
    for code in MAJOR_SECTIONS:
        print(
            f"{code}: "
            f"{len(major_coverage[code])}/"
            f"{len(document_keys)}"
        )

    print()
    print(
        "Expected zero-chunk companies:",
        sorted(expected_zero_chunk_companies),
    )
    print(
        "Actual zero-chunk companies:",
        sorted(zero_chunk_companies),
    )
    print(
        "Unexpected zero-chunk companies:",
        sorted(unexpected_zero_chunk_companies),
    )
    print(f"Flagged chunks: {len(flagged_rows)}")
    print(f"Flagged report: {FLAGGED_REPORT}")
    print(f"Company report: {COMPANY_REPORT}")

    fatal_count = (
        len(flagged_rows)
        + counters["orphan_files"]
        + len(unexpected_zero_chunk_companies)
    )

    for code in MAJOR_SECTIONS:
        if (
            len(major_coverage[code])
            != len(document_keys)
        ):
            fatal_count += 1

    if len(document_keys) != len(filings):
        print(
            "ERROR: document coverage does not match "
            f"filings.csv: {len(document_keys)} "
            f"!= {len(filings)}"
        )
        fatal_count += 1

    if fatal_count:
        print(
            f"\nVALIDATION FAILED: "
            f"{fatal_count} issue(s)"
        )
        sys.exit(1)

    print("\nVALIDATION PASSED")


if __name__ == "__main__":
    main()
