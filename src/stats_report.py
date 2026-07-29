import pandas as pd
from pathlib import Path

import config

# Load indexes
chunks = pd.read_csv(config.CHUNKS_INDEX_CSV)
sections = pd.read_csv(config.SECTIONS_INDEX_CSV)
companies = pd.read_csv(config.COMPANIES_CSV)

# Per company stats from chunks
chunk_stats = chunks.groupby('company').agg(
    n_chunks=('chunk_id', 'count'),
    n_filings=('accession', 'nunique'),
    avg_tokens=('token_count', 'mean')
).reset_index()

# Per company stats from sections
section_stats = sections.groupby('company').agg(
    n_sections=('section_code', 'count')
).reset_index()

# Merge everything
report = chunk_stats.merge(section_stats, on='company', how='outer')
report = report.merge(companies[['ticker', 'name', 'sector']], 
                      left_on='company', right_on='ticker', how='left')

# Clean up
report = report.drop(columns=['ticker'])
report['avg_tokens'] = report['avg_tokens'].round(1)
report = report.sort_values('company')

# Save
output_path = config.STATS_REPORT_CSV
report.to_csv(output_path, index=False)

# Print summary
print(f"Stats Report — {len(report)} companies")
print(f"Total filings: {report['n_filings'].sum()}")
print(f"Total sections: {report['n_sections'].sum()}")
print(f"Total chunks: {report['n_chunks'].sum()}")
print(f"Avg chunks per company: {report['n_chunks'].mean():.1f}")
print(f"Avg tokens per chunk: {report['avg_tokens'].mean():.1f}")
print()
print(report[['company', 'n_filings', 'n_sections', 'n_chunks']].to_string(index=False))
print(f"\nReport saved to {output_path}")