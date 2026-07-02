import pandas as pd
import time
from datetime import datetime

def generate_report():
    # Load the filing state
    df = pd.read_csv('data/00_reference/filing_state.csv')
    companies = pd.read_csv('data/00_reference/companies.csv')

    # Group by ticker
    status_table = df.groupby('ticker').agg(
        filings_found=('accession_number', 'count'),
        uploaded_to_drive=('download_status', lambda x: (x == 'downloaded').sum()),
        failed=('download_status', lambda x: (x == 'failed').sum())
    ).reset_index()

    # Cross-check against companies.csv
    tracked_tickers = set(status_table['ticker'])
    all_tickers = set(companies['ticker'])
    missing_companies = all_tickers - tracked_tickers

    # Print report with timestamp
    print(f"\n{'='*50}")
    print(f"DOWNLOAD STATUS REPORT")
    print(f"Generated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}")
    print(status_table.to_string())
    print(f"\nTotal companies tracked: {len(status_table)}")
    print(f"\nCompanies with incomplete filings (less than 3):")
    incomplete = status_table[status_table['filings_found'] < 3]
    print(incomplete if len(incomplete) > 0 else "None")
    print(f"\nCompanies completely missing: {len(missing_companies)}")
    if missing_companies:
        print(missing_companies)

    # Save report
    status_table.to_csv('data/00_reference/download_status_report.csv', index=False)
    print(f"\nReport saved.")

# Run every 15 minutes forever
while True:
    generate_report()
    print(f"\nNext refresh in 15 minutes... (Press Ctrl+C to stop)")
    time.sleep(15 * 60)  # 15 minutes in seconds