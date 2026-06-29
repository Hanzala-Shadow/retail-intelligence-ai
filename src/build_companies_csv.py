import pandas as pd
import requests

# Load both sheets
retail = pd.read_excel('data/00_reference/apparel_footwear_v3.xlsx', sheet_name='retail')
apparel = pd.read_excel('data/00_reference/apparel_footwear_v3.xlsx', sheet_name='apparel & footwear')

# Add sector labels
retail['sector'] = 'Retail'
apparel['sector'] = 'Apparel & Footwear'

# Combine and clean
combined = pd.concat([retail, apparel], ignore_index=True)
combined = combined.rename(columns={
    '(tic) Ticker Symbol': 'ticker',
    '(conm) Company Name': 'name',
    'Stock Exchange Name': 'exchange',
})
combined = combined.drop_duplicates(subset='ticker')
combined = combined.reset_index(drop=True)
combined['company_id'] = combined.index + 1

# Download SEC's ticker-to-CIK mapping
print("Downloading CIK numbers from SEC...")
headers = {'User-Agent': 'Suleyman research project saghamoghlanli22@ku.edu.tr'}
response = requests.get(
    'https://www.sec.gov/files/company_tickers.json',
    headers=headers
)
sec_data = response.json()

# Build a simple ticker -> CIK dictionary
ticker_to_cik = {}
for entry in sec_data.values():
    ticker_to_cik[entry['ticker'].upper()] = str(entry['cik_str']).zfill(10)

# Look up CIK for each company
combined['cik'] = combined['ticker'].str.upper().map(ticker_to_cik).fillna('')

# Keep only needed columns
output = combined[['company_id', 'ticker', 'cik', 'name', 'sector', 'exchange']]

# Save
output.to_csv('data/00_reference/companies.csv', index=False)

# Report results
found = (output['cik'] != '').sum()
missing = (output['cik'] == '').sum()
print(f"Done! {len(output)} companies saved.")
print(f"CIK found: {found}, Missing: {missing}")
print(output.head(10))

# Show which companies are missing CIK
missing_companies = output[output['cik'] == '']
print("\nMissing CIK:")
print(missing_companies[['ticker', 'name']])

# NOTE: JWN, GES, FL tickers may differ in SEC database
# These can be manually added later if needed