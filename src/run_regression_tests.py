import pandas as pd
from pathlib import Path
import sys

BASELINE_PATH = Path("reports/week3_day1/vector_index_manifest.csv")
CURRENT_CHUNKS_PATH = Path("data/00_reference/chunks_index.csv")

def load_baseline():
    if not BASELINE_PATH.exists():
        print(f"FATAL: Baseline file not found at {BASELINE_PATH}")
        sys.exit(1)
    return pd.read_csv(BASELINE_PATH)

def load_current():
    if not CURRENT_CHUNKS_PATH.exists():
        print(f"FATAL: Current chunks file not found at {CURRENT_CHUNKS_PATH}")
        sys.exit(1)
    return pd.read_csv(CURRENT_CHUNKS_PATH)

def main():
    print("=" * 60)
    print("REGRESSION TEST — comparing against frozen baseline")
    print("=" * 60)
    print("\nNOTE: baseline uses DB-generated integer chunk_ids (RAG-eligible")
    print("chunks only). Local chunks_index.csv uses descriptive string IDs")
    print("and includes ALL chunks, not just eligible ones. Exact chunk-level")
    print("comparison requires live DB access. This script checks what CAN")
    print("be verified locally: company coverage and relative chunk volume.\n")

    baseline = load_baseline()
    current = load_current()

    failures = []
    warnings = []

    # --- Check 1: company coverage ---
    baseline_tickers = set(baseline['ticker'].unique())
    current_tickers = set(current['company'].unique())
    missing_tickers = baseline_tickers - current_tickers
    extra_tickers = current_tickers - baseline_tickers

    if missing_tickers:
        failures.append(f"{len(missing_tickers)} baseline companies missing locally: {sorted(missing_tickers)[:10]}")
    else:
        print(f"PASS: all {len(baseline_tickers)} baseline companies present locally")

    if extra_tickers:
        warnings.append(f"{len(extra_tickers)} companies exist locally but not in baseline: {sorted(extra_tickers)[:10]}")

    # --- Check 2: per-company chunk count sanity (local should be >= baseline eligible count) ---
    baseline_per_company = baseline.groupby('ticker').size()
    current_per_company = current.groupby('company').size()

    undersized = []
    for ticker, baseline_n in baseline_per_company.items():
        current_n = current_per_company.get(ticker, 0)
        if current_n < baseline_n:
            undersized.append((ticker, baseline_n, current_n))

    if undersized:
        failures.append(f"{len(undersized)} companies have FEWER local chunks than baseline eligible count (should never happen — local includes non-eligible too): {undersized[:5]}")
    else:
        print("PASS: every company has at least as many local chunks as its baseline eligible count")

    # --- Check 3: total volume sanity ---
    print(f"\nBaseline (RAG-eligible only): {len(baseline)} chunks")
    print(f"Local (all chunks, incl. excluded): {len(current)} chunks")
    print(f"Difference: {len(current) - len(baseline)} (expected to be positive — local includes excluded chunks)")

    if len(current) < len(baseline):
        failures.append("Local total is SMALLER than baseline eligible count — this should be impossible")

    print("\n" + "=" * 60)
    if warnings:
        print("WARNINGS:")
        for w in warnings:
            print(f"  - {w}")
    if failures:
        print(f"\nREGRESSION TEST FAILED — {len(failures)} issue(s) found:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("\nREGRESSION TEST PASSED (local-verifiable checks only)")
        print("Full chunk-level verification requires DB access — recommend Hanzala runs this against live DB too.")
    print("=" * 60)

if __name__ == "__main__":
    main()