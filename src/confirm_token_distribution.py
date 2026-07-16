import sqlite3
import pandas as pd

DB_PATH = 'data/esg_local_audit_2026-07-13.sqlite'

conn = sqlite3.connect(DB_PATH)
chunks = pd.read_sql('SELECT * FROM chunks', conn)
elig = pd.read_sql('SELECT * FROM index_eligibility', conn)

print("=" * 60)
print("TOKEN DISTRIBUTION CONFIRMATION")
print("=" * 60)
print(f"\nTotal chunks: {len(chunks)}")
print(f"Min tokens: {chunks['token_count'].min()}")
print(f"Max tokens: {chunks['token_count'].max()}")
print(f"Mean tokens: {chunks['token_count'].mean():.1f}")

below_50 = chunks[chunks['token_count'] < 50]
print(f"\nChunks below 50-token minimum: {len(below_50)} ({len(below_50)/len(chunks)*100:.2f}%)")
print("STATUS: PASS" if len(below_50) == 0 else "STATUS: FAIL")

print(f"\nRAG eligibility (from Aziz's index_eligibility table):")
print(f"Eligible: {elig['index_eligible'].sum()}")
print(f"Excluded: {len(elig) - elig['index_eligible'].sum()}")
print(f"Exclusion rate: {(len(elig) - elig['index_eligible'].sum())/len(elig)*100:.2f}%")

print("\nExclusion reasons breakdown:")
print(elig[elig['index_eligible']==0]['exclusion_reasons'].value_counts())

# Save report
report = {
    'metric': ['total_chunks', 'min_token_count', 'max_token_count', 'mean_token_count',
               'chunks_below_50_tokens', 'chunks_eligible', 'chunks_excluded', 'exclusion_rate_pct'],
    'value': [len(chunks), chunks['token_count'].min(), chunks['token_count'].max(),
              round(chunks['token_count'].mean(), 1), len(below_50),
              elig['index_eligible'].sum(), len(elig) - elig['index_eligible'].sum(),
              round((len(elig) - elig['index_eligible'].sum())/len(elig)*100, 2)]
}
pd.DataFrame(report).to_csv('data/00_reference/esg_token_distribution_confirmation.csv', index=False)
print(f"\nReport saved to data/00_reference/esg_token_distribution_confirmation.csv")

conn.close()