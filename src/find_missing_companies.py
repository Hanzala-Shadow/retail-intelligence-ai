# Create src/find_missing_companies.py
from pathlib import Path
import pandas as pd

# Companies we have locally
local_folders = set(f.name for f in Path('data/01_raw/10k').iterdir() if f.is_dir())

# All companies in companies.csv
companies = pd.read_csv('data/00_reference/companies.csv')
all_tickers = set(companies['ticker'])

# Missing ones
missing = all_tickers - local_folders
print(f"Missing {len(missing)} companies locally:")
for t in sorted(missing):
    print(t)