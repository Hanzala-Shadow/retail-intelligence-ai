import pandas as pd
import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)

import config

df = pd.read_csv(config.FILING_STATE_CSV)
print(f"Total filings: {len(df)}")
print(f"Downloaded: {(df['download_status'] == 'downloaded').sum()}")
print(f"Pending: {(df['download_status'] == 'pending').sum()}")
print(f"Failed: {(df['download_status'] == 'failed').sum()}")
print()
print("Companies with pending filings:")
pending = df[df['download_status'] == 'pending']
print(pending['ticker'].unique())