import pandas as pd
from pathlib import Path

# Load chunks index
chunks = pd.read_csv('data/00_reference/chunks_index.csv')

print(f"Total chunks to validate: {len(chunks)}")

# Flag problematic chunks
chunks['flag'] = 'ok'
chunks.loc[chunks['token_count'] < 50, 'flag'] = 'too_short'
chunks.loc[chunks['token_count'] > 500, 'flag'] = 'too_long'

# Summary
ok = (chunks['flag'] == 'ok').sum()
too_short = (chunks['flag'] == 'too_short').sum()
too_long = (chunks['flag'] == 'too_long').sum()

print(f"\nValidation Results:")
print(f"  OK:        {ok}")
print(f"  Too short (<50 tokens):  {too_short}")
print(f"  Too long  (>600 tokens): {too_long}")

# Companies with zero chunks
companies = pd.read_csv('data/00_reference/companies.csv')
chunked_companies = set(chunks['company'].unique())
all_companies = set(companies['ticker'].unique())
zero_chunk_companies = all_companies - chunked_companies

print(f"\nCompanies with zero chunks: {len(zero_chunk_companies)}")
if zero_chunk_companies:
    print(sorted(zero_chunk_companies))

# Per company flag summary
company_summary = chunks.groupby('company').agg(
    total_chunks=('chunk_id', 'count'),
    too_short=('flag', lambda x: (x == 'too_short').sum()),
    too_long=('flag', lambda x: (x == 'too_long').sum()),
    ok=('flag', lambda x: (x == 'ok').sum()),
).reset_index()

company_summary['has_issues'] = (
    (company_summary['too_short'] > 0) | 
    (company_summary['too_long'] > 0)
)

# Save QA report
output_path = Path('data/00_reference/chunk_qa_report.csv')
flagged = chunks[chunks['flag'] != 'ok'][[
    'chunk_id', 'company', 'section', 'chunk_index', 'token_count', 'flag'
]]
flagged.to_csv(output_path, index=False)

# Save company summary
summary_path = Path('data/00_reference/chunk_qa_company_summary.csv')
company_summary.to_csv(summary_path, index=False)

print(f"\nFlagged chunks saved to {output_path}")
print(f"Company summary saved to {summary_path}")
print(f"\nCompanies with issues: {company_summary['has_issues'].sum()}")