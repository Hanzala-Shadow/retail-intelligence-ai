import sqlite3
import pandas as pd

DB_PATH = 'data/esg_local_audit_2026-07-13.sqlite'

conn = sqlite3.connect(DB_PATH)

qa = pd.read_sql("SELECT * FROM qa", conn)
anomalies = pd.read_sql("SELECT * FROM anomalies", conn)
company_quality = pd.read_sql("SELECT * FROM company_quality", conn)

# Companies with HIGH severity anomalies - must exclude or fix before production
high_severity_tickers = set(anomalies[anomalies['severity'] == 'high']['ticker'].unique())

# Companies with "garbled" or "low readable" flags in needs_review notes
needs_review = qa[qa['status'] == 'needs_review']
garbled_tickers = set(needs_review[
    needs_review['notes'].str.contains('garbled|low readable|low text', case=False, na=False)
]['ticker'].unique())

# Build manifest per unique ticker
manifest_rows = []
all_tickers = qa['ticker'].dropna().unique()

for ticker in sorted(all_tickers):
    company_rows = qa[qa['ticker'] == ticker]
    company_name = company_rows['company_name'].iloc[0] if len(company_rows) > 0 else ticker
    
    statuses = set(company_rows['status'].unique())
    
    if ticker in high_severity_tickers or ticker in garbled_tickers:
        decision = 'EXCLUDE_PENDING_FIX'
        reason = 'High-severity garbled text or low readable word ratio detected'
    elif 'not_found' in statuses and len(statuses) == 1:
        decision = 'NOT_FOUND'
        reason = 'No ESG report found for this company'
    elif 'missing_pdf' in statuses:
        decision = 'MISSING_PDF'
        reason = 'PDF reference exists but file missing'
    elif 'needs_review' in statuses:
        decision = 'ACCEPT_MINOR_ISSUES'
        reason = 'Minor issues (e.g. missing citation metadata), acceptable for staging'
    else:
        decision = 'ACCEPT'
        reason = 'Clean QA pass'
    
    manifest_rows.append({
        'ticker': ticker,
        'company_name': company_name,
        'decision': decision,
        'reason': reason,
        'total_qa_rows': len(company_rows),
    })

manifest = pd.DataFrame(manifest_rows)

# Summary
print("=" * 60)
print("ACCEPTED-COMPANY MANIFEST SUMMARY")
print("=" * 60)
print(manifest['decision'].value_counts())
print()
print(f"Total companies evaluated: {len(manifest)}")
print(f"ACCEPTED (clean + minor issues): {len(manifest[manifest['decision'].isin(['ACCEPT', 'ACCEPT_MINOR_ISSUES'])])}")
print(f"EXCLUDED pending fix: {len(manifest[manifest['decision'] == 'EXCLUDE_PENDING_FIX'])}")
print(f"NOT FOUND: {len(manifest[manifest['decision'] == 'NOT_FOUND'])}")

# Save
manifest.to_csv('data/00_reference/esg_accepted_company_manifest.csv', index=False)
print(f"\nManifest saved to data/00_reference/esg_accepted_company_manifest.csv")

# Show excluded companies specifically
excluded = manifest[manifest['decision'] == 'EXCLUDE_PENDING_FIX']
print(f"\nCompanies excluded pending fix:")
print(excluded[['ticker', 'company_name', 'reason']].to_string(index=False))

conn.close()