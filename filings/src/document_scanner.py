import csv
from pathlib import Path
import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)

import config

root = config.RAW_10K_DIR
output_path = config.DOCUMENT_SCAN_CSV

results = []

for company_folder in sorted(root.iterdir()):
    if not company_folder.is_dir():
        continue
    
    ticker = company_folder.name
    htm_files = list(company_folder.glob('*.htm'))
    
    for f in htm_files:
        size = f.stat().st_size
        status = 'ok' if size > 1000 else 'empty'
        results.append({
            'ticker': ticker,
            'filename': f.name,
            'size_bytes': size,
            'status': status,
        })

# Save results
output_path.parent.mkdir(parents=True, exist_ok=True)
with open(output_path, 'w', newline='', encoding='utf-8') as f:
    writer = csv.DictWriter(f, fieldnames=['ticker', 'filename', 'size_bytes', 'status'])
    writer.writeheader()
    writer.writerows(results)

# Print summary
ok = sum(1 for r in results if r['status'] == 'ok')
empty = sum(1 for r in results if r['status'] == 'empty')
print(f'Total files scanned: {len(results)}')
print(f'OK: {ok}')
print(f'Empty/suspicious: {empty}')
print(f'Companies scanned: {len(set(r["ticker"] for r in results))}')
print(f'Report saved to {output_path}')