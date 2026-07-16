import pandas as pd
from pathlib import Path
import sys

MANIFEST_PATH = Path("reports/week3_day1/vector_index_manifest.csv")
COMPANIES_PATH = Path("data/00_reference/companies.csv")

BASELINE_FILINGS = 571
BASELINE_TOTAL_CHUNKS = 113454
BASELINE_ELIGIBLE_CHUNKS = 89760
BASELINE_COMPANIES = 194


def load_data():
    if not MANIFEST_PATH.exists():
        print(f"FATAL: {MANIFEST_PATH} not found")
        sys.exit(1)
    if not COMPANIES_PATH.exists():
        print(f"FATAL: {COMPANIES_PATH} not found")
        sys.exit(1)
    manifest = pd.read_csv(MANIFEST_PATH)
    companies = pd.read_csv(COMPANIES_PATH)
    return manifest, companies


def check_1_token_distribution(manifest):
    """Token count distribution by chunk type (grouped by section_label)."""
    print("\n" + "=" * 70)
    print("CHECK 1 — Token count distribution by chunk type")
    print("=" * 70)

    stats = manifest.groupby("section_label")["token_count"].agg(
        ["count", "min", "max", "mean"]
    ).round(1)
    print(stats.to_string())

    below_50 = manifest[manifest["token_count"] < 50]
    above_600 = manifest[manifest["token_count"] > 600]

    passed = len(below_50) == 0 and len(above_600) == 0
    print(f"\nChunks below 50 tokens: {len(below_50)}")
    print(f"Chunks above 600 tokens: {len(above_600)}")
    print(f"RESULT: {'PASS' if passed else 'FAIL'}")
    return passed


def check_2_section_attribution(manifest):
    """Section attribution completeness rate - % of chunks with a valid section_label."""
    print("\n" + "=" * 70)
    print("CHECK 2 — Section attribution completeness rate")
    print("=" * 70)

    total = len(manifest)
    labeled = manifest["section_label"].notna().sum()
    rate = labeled / total * 100

    print(f"Total chunks: {total}")
    print(f"Chunks with section_label: {labeled}")
    print(f"Completeness rate: {rate:.2f}%")

    passed = rate == 100.0
    print(f"RESULT: {'PASS' if passed else 'FAIL'}")
    return passed


def check_3_chunk_count_per_company(manifest, companies):
    """Chunk count per company vs expected (flag companies with 0 or suspiciously low counts)."""
    print("\n" + "=" * 70)
    print("CHECK 3 — Chunk count per company vs expected")
    print("=" * 70)

    per_company = manifest.groupby("ticker").size()
    all_tickers = set(companies["ticker"])
    manifest_tickers = set(manifest["ticker"])

    zero_chunk_companies = all_tickers - manifest_tickers
    print(f"Companies with 0 eligible chunks: {len(zero_chunk_companies)}")
    if zero_chunk_companies:
        print(f"  {sorted(zero_chunk_companies)}")

    low_chunk_companies = per_company[per_company < 50]
    print(f"\nCompanies with suspiciously low chunk counts (<50): {len(low_chunk_companies)}")
    if len(low_chunk_companies) > 0:
        print(low_chunk_companies.to_string())

    print(f"\nAvg chunks per company: {per_company.mean():.1f}")
    print(f"Median chunks per company: {per_company.median():.1f}")

    # Known expected exception: YSWY has no SEC filings (delisted/private)
    unexpected_zero = zero_chunk_companies - {"YSWY"}
    passed = len(unexpected_zero) == 0
    if unexpected_zero:
        print(f"\nUNEXPECTED zero-chunk companies (not YSWY): {unexpected_zero}")
    print(f"RESULT: {'PASS' if passed else 'FAIL'} (YSWY zero-chunk is a known, expected exception)")
    return passed


def check_4_metadata_null_rates(manifest):
    """Metadata NULL rates for rag_eligible_10k_chunks rows."""
    print("\n" + "=" * 70)
    print("CHECK 4 — Metadata NULL rates (rag_eligible_10k_chunks view)")
    print("=" * 70)

    null_counts = manifest.isnull().sum()
    total = len(manifest)

    print(f"Total rows: {total}")
    print("NULL counts per column:")
    print(null_counts.to_string())

    total_nulls = null_counts.sum()
    null_rate = total_nulls / (total * len(manifest.columns)) * 100

    print(f"\nOverall NULL rate: {null_rate:.4f}%")
    passed = total_nulls == 0
    print(f"RESULT: {'PASS' if passed else 'FAIL'}")
    return passed


def check_5_company_coverage(manifest, companies):
    """Total coverage against the 194-company list."""
    print("\n" + "=" * 70)
    print("CHECK 5 — Total coverage against company list")
    print("=" * 70)

    total_companies = len(companies)
    covered_companies = manifest["ticker"].nunique()
    missing = set(companies["ticker"]) - set(manifest["ticker"])

    print(f"Total companies in companies.csv: {total_companies}")
    print(f"Companies with eligible chunks: {covered_companies}")
    print(f"Missing companies: {sorted(missing) if missing else 'none'}")

    coverage_matches_baseline = total_companies == BASELINE_COMPANIES
    known_exception = missing == {"YSWY"}

    print(f"\nBaseline company count: {BASELINE_COMPANIES}")
    print(f"Current company count: {total_companies}")
    print(f"Delta: {total_companies - BASELINE_COMPANIES}")

    passed = coverage_matches_baseline and known_exception
    print(f"RESULT: {'PASS' if passed else 'FAIL'} (YSWY missing is a known, documented exception)")
    return passed


def main():
    print("=" * 70)
    print("THURSDAY REGRESSION TEST SUITE — 5-check deep validation")
    print(f"Baseline: {BASELINE_FILINGS} filings, {BASELINE_TOTAL_CHUNKS} total chunks, "
          f"{BASELINE_ELIGIBLE_CHUNKS} eligible chunks, {BASELINE_COMPANIES} companies")
    print("=" * 70)

    manifest, companies = load_data()

    print(f"\nLoaded manifest: {len(manifest)} rows")
    manifest_matches_baseline = len(manifest) == BASELINE_ELIGIBLE_CHUNKS
    print(f"Manifest row count vs baseline eligible chunks: "
          f"{'MATCH' if manifest_matches_baseline else 'MISMATCH'} "
          f"(delta: {len(manifest) - BASELINE_ELIGIBLE_CHUNKS})")

    results = {
        "1_token_distribution": check_1_token_distribution(manifest),
        "2_section_attribution": check_2_section_attribution(manifest),
        "3_chunk_count_per_company": check_3_chunk_count_per_company(manifest, companies),
        "4_metadata_null_rates": check_4_metadata_null_rates(manifest),
        "5_company_coverage": check_5_company_coverage(manifest, companies),
    }

    print("\n" + "=" * 70)
    print("FINAL SUMMARY")
    print("=" * 70)
    for check, passed in results.items():
        print(f"  {check}: {'PASS' if passed else 'FAIL'}")

    all_passed = all(results.values()) and manifest_matches_baseline
    print(f"\nOVERALL: {'ALL CHECKS PASSED' if all_passed else 'SOME CHECKS FAILED'}")
    print("\nNOTE: This suite validates against the local vector_index_manifest.csv snapshot")
    print("(the frozen eligible-chunk baseline). It does not independently re-query the live")
    print("PostgreSQL database — that would require DB credentials this script does not have.")
    print("=" * 70)

    if not all_passed:
        sys.exit(1)


if __name__ == "__main__":
    main()