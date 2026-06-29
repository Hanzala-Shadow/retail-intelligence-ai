import pandas as pd

# Load both sheets
retail = pd.read_excel('data/00_reference/apparel_footwear_v3.xlsx', sheet_name='retail')
apparel = pd.read_excel('data/00_reference/apparel_footwear_v3.xlsx', sheet_name='apparel & footwear')

# Add a sector label to each so we know where they came from
retail['sector'] = 'Retail'
apparel['sector'] = 'Apparel & Footwear'

# Combine both sheets into one
combined = pd.concat([retail, apparel], ignore_index=True)

# Rename the columns we need to simpler names
combined = combined.rename(columns={
    '(tic) Ticker Symbol': 'ticker',
    '(conm) Company Name': 'name',
    'Stock Exchange Name': 'exchange',
})

# Drop duplicate tickers (same company in both sheets)
combined = combined.drop_duplicates(subset='ticker')

# Add a company_id column (just a number 1, 2, 3...)
combined = combined.reset_index(drop=True)
combined['company_id'] = combined.index + 1

# Add empty cik column for now (we'll fill this next)
combined['cik'] = ''

# Keep only the columns we need
output = combined[['company_id', 'ticker', 'cik', 'name', 'sector', 'exchange']]

# Save to csv
output.to_csv('data/00_reference/companies.csv', index=False)

print(f"Done! {len(output)} companies saved.")
print(output.head(10))