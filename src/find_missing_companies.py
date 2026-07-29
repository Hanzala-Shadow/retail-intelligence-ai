# Create src/find_missing_companies.py
from pathlib import Path
import pandas as pd

import config

# Companies we have locally
local_folders = set(f.name for f in config.RAW_10K_DIR.iterdir() if f.is_dir())

# All companies in companies.csv
companies = pd.read_csv(config.COMPANIES_CSV)
all_tickers = set(companies['ticker'])

# Missing ones
missing = all_tickers - local_folders
print(f"Missing {len(missing)} companies locally:")
for t in sorted(missing):
    print(t)