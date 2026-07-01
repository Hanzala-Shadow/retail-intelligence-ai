import pandas as pd

# Load the filing state
df = pd.read_csv('data/00_reference/filing_state.csv')

# Group by ticker, count filings and downloaded ones
status_table = df.groupby('ticker').agg(
    filings_found=('accession_number', 'count'),
    uploaded_to_drive=('download_status', lambda x: (x == 'downloaded').sum()),
    failed=('download_status', lambda x: (x == 'failed').sum())
).reset_index()

print(status_table.to_string())
print(f"\nTotal companies tracked: {len(status_table)}")


# Cross-check against companies.csv to find fully missing companies
companies = pd.read_csv('data/00_reference/companies.csv')

tracked_tickers = set(status_table['ticker'])
all_tickers = set(companies['ticker'])

missing_companies = all_tickers - tracked_tickers

print(f"\nCompanies with incomplete filings (less than 3):")
print(status_table[status_table['filings_found'] < 3])

print(f"\nCompanies completely missing from filing_state.csv: {len(missing_companies)}")
if missing_companies:
    print(missing_companies)


# Save the status report to a file
status_table.to_csv('data/00_reference/download_status_report.csv', index=False)
print("\nReport saved to data/00_reference/download_status_report.csv")