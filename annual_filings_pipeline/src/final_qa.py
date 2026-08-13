import argparse
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

CHUNKS_INDEX_CSV = Path("data/00_reference/chunks_index.csv")
SECTIONS_CSV = Path("data/00_reference/sections_index.csv")
OUTPUT_CSV = Path("data/tables/qa_pass_fail.csv")

MIN_SECTIONS_PER_FILING = 3
TENK_DOC_TYPE_VALUES = {"10-K", "10K", "10-k", "10k"}

ACCESSION_RE = re.compile(r"__(?:10-K|10K)__([0-9]{10}-[0-9]{2}-[0-9]{6})__", re.IGNORECASE)

def extract_filing_id_from_path(file_path):
    """Extract accession from filenames like: PRTS__10-K__0001378950-26-000035__Item_1.txt"""
    filename = Path(file_path).name
    m = ACCESSION_RE.search(filename)
    if m:
        return m.group(1)
    return None

def load_chunks_index(path):
    """Load chunks_index.csv and return filings per company and chunk counts per section."""
    filings_by_company = defaultdict(set)
    chunk_counts_by_section = defaultdict(int)
    chunk_id_counts = defaultdict(int)

    if not path.exists():
        print(f"ERROR: {path} not found.")
        sys.exit(1)

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        required = {"chunk_id", "company", "doc_type", "accession", "section"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            print(f"ERROR: {path} is missing expected columns: {missing}")
            print(f"       Columns found: {reader.fieldnames}")
            sys.exit(1)

        for row in reader:
            company = row["company"].strip()
            doc_type = row["doc_type"].strip()
            accession = row["accession"].strip()
            section = row["section"].strip()
            chunk_id = row["chunk_id"].strip()

            chunk_id_counts[chunk_id] += 1
            chunk_counts_by_section[(company, accession, section)] += 1

            if doc_type in TENK_DOC_TYPE_VALUES:
                filings_by_company[company].add(accession)

    return filings_by_company, chunk_counts_by_section, chunk_id_counts

def load_sections(path):
    sections_by_company_filing = defaultdict(set)

    if not path.exists():
        print(f"ERROR: {path} not found.")
        sys.exit(1)

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        required = {"company", "section_code", "file"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            print(f"ERROR: {path} is missing expected columns: {missing}")
            print(f"       Columns found: {reader.fieldnames}")
            sys.exit(1)

        for row in reader:
            company = row["company"].strip()
            section_code = row["section_code"].strip()
            file_path = row["file"].strip()

            filing_id = extract_filing_id_from_path(file_path)
            if filing_id is None:
                continue  

            sections_by_company_filing[(company, filing_id)].add(section_code)

    return sections_by_company_filing

def run_qa(chunks_index_path=CHUNKS_INDEX_CSV, sections_path=SECTIONS_CSV, output_csv=OUTPUT_CSV):
    filings_by_company, chunk_counts_by_section, chunk_id_counts = load_chunks_index(chunks_index_path)
    sections_by_company_filing = load_sections(sections_path)

    # Find duplicate chunk_ids
    duplicate_ids = {cid for cid, count in chunk_id_counts.items() if count > 1}
    company_dupe_ids = defaultdict(set)
    if duplicate_ids:
        with open(chunks_index_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                cid = row["chunk_id"].strip()
                if cid in duplicate_ids:
                    company_dupe_ids[row["company"].strip()].add(cid)

    # Get all companies
    all_companies = set(filings_by_company.keys())
    all_companies |= {c for (c, _fid) in sections_by_company_filing.keys()}
    companies = sorted(all_companies)

    rows = []
    n_pass = 0

    for company in companies:
        reasons = []

        # Check 1: >=1 10-K filing
        accessions = filings_by_company.get(company, set())
        n_filings = len(accessions)
        if n_filings < 1:
            reasons.append("no 10-K filings found")

        # Check 2: >=3 sections per filing
        sections_missing_chunks = []
        for accession in accessions:
            section_codes = sections_by_company_filing.get((company, accession), set())
            if len(section_codes) < MIN_SECTIONS_PER_FILING:
                reasons.append(
                    f"filing '{accession}' has only {len(section_codes)} sections "
                    f"(need >= {MIN_SECTIONS_PER_FILING})"
                )

            # Check 3: all sections have >=1 chunk
            for section_code in section_codes:
                n_chunks = chunk_counts_by_section.get((company, accession, section_code), 0)
                if n_chunks < 1:
                    sections_missing_chunks.append(f"{accession}/{section_code}")

        if sections_missing_chunks:
            reasons.append(
                f"{len(sections_missing_chunks)} section(s) with zero chunks: "
                + ", ".join(sections_missing_chunks[:5])
                + (" ..." if len(sections_missing_chunks) > 5 else "")
            )

        # Check 4: no duplicate chunk_ids
        dupe_ids = sorted(company_dupe_ids.get(company, set()))
        if dupe_ids:
            reasons.append(
                f"{len(dupe_ids)} duplicate chunk_id(s): " + ", ".join(dupe_ids[:5])
                + (" ..." if len(dupe_ids) > 5 else "")
            )

        status = "PASS" if not reasons else "FAIL"
        if status == "PASS":
            n_pass += 1

        rows.append({
            "company": company,
            "status": status,
            "n_filings": n_filings,
            "n_sections_total": sum(len(sections_by_company_filing.get((company, acc), set())) 
                                    for acc in accessions),
            "sections_missing_chunks_count": len(sections_missing_chunks),
            "duplicate_chunk_ids_count": len(dupe_ids),
            "fail_reasons": " | ".join(reasons),
        })

    # Write output
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "company", "status", "n_filings", "n_sections_total",
            "sections_missing_chunks_count", "duplicate_chunk_ids_count",
            "fail_reasons",
        ])
        writer.writeheader()
        writer.writerows(rows)

    total = len(rows)
    pass_pct = (n_pass / total * 100) if total else 0.0

    print(f"QA complete: {n_pass}/{total} companies PASS ({pass_pct:.1f}%)")
    print(f"Output written to: {output_csv}")

    if pass_pct < 90:
        print("WARNING: pass rate is below the 90% sprint acceptance gate.")
        return 1

    return 0

def main():
    parser = argparse.ArgumentParser(description="Final QA gate checks for 10-K filings.")
    parser.add_argument("--chunks-index", default=str(CHUNKS_INDEX_CSV))
    parser.add_argument("--sections", default=str(SECTIONS_CSV))
    parser.add_argument("--output", default=str(OUTPUT_CSV))
    args = parser.parse_args()

    exit_code = run_qa(
        chunks_index_path=Path(args.chunks_index),
        sections_path=Path(args.sections),
        output_csv=Path(args.output),
    )
    sys.exit(exit_code)

if __name__ == "__main__":
    main()