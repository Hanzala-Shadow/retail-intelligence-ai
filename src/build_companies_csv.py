import pandas as pd
import requests # requests: tool for downloading things from the internet

# Load both sheets
retail = pd.read_excel('data/00_reference/apparel_footwear_v3.xlsx', sheet_name='retail')
apparel = pd.read_excel('data/00_reference/apparel_footwear_v3.xlsx', sheet_name='apparel & footwear')


# Add sector labels
retail['sector'] = 'Broadline Retail'
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

# Manual fixes for tickers not found in SEC file
ticker_to_cik['JWN'] = '0000072333'
ticker_to_cik['FL'] = '0000850209'
ticker_to_cik['GES'] = '0000912463'
ticker_to_cik['TBHC'] = '0001056285'

# Look up CIK for each company
combined['cik'] = combined['ticker'].str.upper().map(ticker_to_cik).fillna('')

# Remove companies with missing CIK - can't download their filings without it
combined = combined[combined['cik'] != '']

# Keep only needed columns
output = combined[['company_id', 'ticker', 'cik', 'name', 'sector', 'exchange']]

# Save
output.to_csv('data/00_reference/companies.csv', index=False)

# Report results
found = (output['cik'] != '').sum()
missing = (output['cik'] == '').sum()
print(f"Done! {len(output)} companies saved.")
print(f"CIK found: {found}, Missing: {missing}")
print(output.to_string())

# Show which companies are missing CIK
missing_companies = output[output['cik'] == '']
if len(missing_companies) > 0:
    print("\nMissing CIK:")
    print(missing_companies[['ticker', 'name']])
else:
    print("\nAll companies have CIK numbers!")