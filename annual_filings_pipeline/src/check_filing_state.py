import pandas as pd

df = pd.read_csv('data/00_reference/filing_state.csv')
print(f"Total filings: {len(df)}")
print(f"Downloaded: {(df['download_status'] == 'downloaded').sum()}")
print(f"Pending: {(df['download_status'] == 'pending').sum()}")
print(f"Failed: {(df['download_status'] == 'failed').sum()}")
print()
print("Companies with pending filings:")
pending = df[df['download_status'] == 'pending']
print(pending['ticker'].unique())