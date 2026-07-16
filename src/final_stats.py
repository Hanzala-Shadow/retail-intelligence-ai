import pandas as pd
from pathlib import Path

print("=" * 50)
print("SPRINT 2 DELIVERY STATS")
print("=" * 50)

# Companies
companies = pd.read_csv('data/00_reference/companies.csv')
print(f"\nCompanies: {len(companies)}")

# Filings
filings = pd.read_csv('data/00_reference/filings.csv')
filing_state = pd.read_csv('data/00_reference/filing_state.csv')
downloaded = filing_state[filing_state['download_status'].isin(['downloaded', 'downloaded_not_uploaded'])]
print(f"10-K Filings discovered: {len(filings)}")
print(f"10-K Filings downloaded: {len(downloaded)}")

# Sections
sections = pd.read_csv('data/00_reference/sections_index.csv')
print(f"\nSections: {len(sections)}")
print(f"Avg sections per company: {len(sections)/len(companies):.1f}")

# Chunks
chunks = pd.read_csv('data/00_reference/chunks_index.csv')
print(f"\nChunks: {len(chunks)}")
print(f"Avg chunks per company: {len(chunks)/len(companies):.1f}")
print(f"Avg tokens per chunk: {chunks['token_count'].mean():.1f}")

# ESG
esg_index = Path('data/00_reference/esg_sections_index.csv')
if esg_index.exists():
    esg = pd.read_csv(esg_index)
    print(f"\nESG sections: {len(esg)}")
    print(f"ESG companies: {esg['ticker'].nunique()}")
else:
    print("\nESG sections: not available locally")

# Fallbacks
fallback_file = Path('reports/fallback_10k_sections_final.txt')
if fallback_file.exists():
    fallbacks = [l.strip() for l in fallback_file.read_text().strip().split('\n') if l.strip()]
    print(f"\nFallback sections: {len(fallbacks)}")
    fallback_companies = set(f.split('__')[0] for f in fallbacks)
    print(f"Companies with fallbacks: {len(fallback_companies)}")

print("\n" + "=" * 50)
print("Sprint 2 pipeline complete.")
print("=" * 50)