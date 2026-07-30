import pandas as pd
import time
import requests
import os
from datetime import datetime
from dotenv import load_dotenv
import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)

import config

# Load environment variables from .env file
load_dotenv()
SLACK_WEBHOOK_URL = os.getenv('SLACK_WEBHOOK_URL')

def generate_report():
    # Load the filing state
    df = pd.read_csv(config.FILING_STATE_CSV)
    companies = pd.read_csv(config.COMPANIES_CSV)

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
    incomplete = status_table[status_table['filings_found'] < 3]

    # Save report
    status_table.to_csv(config.DOWNLOAD_STATUS_REPORT_CSV, index=False)

    # Print to terminal
    print(f"\n{'='*50}")
    print(f"DOWNLOAD STATUS REPORT")
    print(f"Generated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}")
    print(f"Total companies tracked: {len(status_table)}")
    print(f"Fully downloaded (3/3): {len(status_table[status_table['filings_found'] == 3])}")
    print(f"Incomplete: {len(incomplete)}")
    print(f"Completely missing: {len(missing_companies)}")
    if len(incomplete) > 0:
        print(f"\nIncomplete companies:")
        print(incomplete.to_string())

    return status_table, incomplete, missing_companies

def post_to_slack(status_table, incomplete, missing_companies):
    if not SLACK_WEBHOOK_URL:
        print("No Slack webhook URL found in .env — skipping Slack post.")
        return

    incomplete_text = ""
    if len(incomplete) > 0:
        rows = incomplete[['ticker', 'filings_found']].to_string(index=False)
        incomplete_text = f"\n⚠️ Incomplete filings:\n```{rows}```"
    else:
        incomplete_text = "\n✅ All companies have 3/3 filings"

    message = (
        f"*📊 Daily Download Status Report*\n"
        f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Total companies: {len(status_table)}\n"
        f"Fully downloaded: {len(status_table[status_table['filings_found'] == 3])}\n"
        f"Missing companies: {len(missing_companies)}"
        f"{incomplete_text}"
    )

    response = requests.post(SLACK_WEBHOOK_URL, json={'text': message})
    if response.status_code == 200:
        print("✅ Posted to Slack successfully.")
    else:
        print(f"❌ Slack post failed: {response.status_code}")

# Run once immediately and post to Slack
print("Starting download monitor...")
status_table, incomplete, missing_companies = generate_report()
post_to_slack(status_table, incomplete, missing_companies)

# Then refresh every 15 minutes silently
while True:
    print(f"\nNext refresh in 15 minutes... (Press Ctrl+C to stop)")
    time.sleep(15 * 60)
    status_table, incomplete, missing_companies = generate_report()