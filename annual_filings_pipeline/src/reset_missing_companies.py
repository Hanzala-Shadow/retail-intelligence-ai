import pandas as pd
from pathlib import Path

# List of missing companies
missing = set(['AEO', 'AKA', 'AMZN', 'ANF', 'BIRD', 'BKE', 'BOOT', 'BURL', 
'CAL', 'CATO', 'COLM', 'CPNG', 'CRI', 'CROX', 'CTRN', 'CURV', 'DBGI', 'DBI', 
'DDS', 'DECK', 'DIBS', 'DLTH', 'DXLG', 'EBAY', 'ETSY', 'FIGS', 'FL', 'FOSL', 
'GAP', 'GCO', 'GES', 'GIII', 'GRPN', 'HOUR', 'JILL', 'JRSH', 'JWN', 'KSS', 
'KTB', 'LAKE', 'LE', 'LEVI', 'LULU', 'LVLU', 'M', 'MELI', 'MOV', 'NKE', 'OLLI', 
'OXM', 'PLBY', 'PLCE', 'PTRN', 'PVH', 'RCKY', 'RL', 'ROST', 'RVLV', 'SFIX', 
'SGC', 'SHOE', 'SHOO', 'SVV', 'TJX', 'TLYS', 'TPR', 'UAA', 'URBN', 'VFC', 
'VNCE', 'VRA', 'VSXY', 'WWW', 'XELB', 'YSWY', 'ZUMZ'])

# Reset filing_state.csv
df1 = pd.read_csv('data/00_reference/filing_state.csv')
mask1 = df1['ticker'].isin(missing)
df1.loc[mask1, 'download_status'] = 'pending'
df1.to_csv('data/00_reference/filing_state.csv', index=False)
print(f"Reset {mask1.sum()} filings in filing_state.csv")

# Reset filings.csv
df2 = pd.read_csv('data/00_reference/filings.csv')
mask2 = df2['ticker'].isin(missing)
df2.loc[mask2, 'download_status'] = 'pending'
df2.to_csv('data/00_reference/filings.csv', index=False)
print(f"Reset {mask2.sum()} filings in filings.csv")