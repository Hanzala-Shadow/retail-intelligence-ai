# Corrected Annual 10-K History — FY2015–FY2025

This package creates a relational-only, isolated historical dataset from the
frozen v3 selection manifest. It does not alter `fy2325-v2.16`, embeddings,
HNSW indexes, `rag_eligible_10k_chunks`, API services, or the chatbot.

## Canonical server layout

```text
data_history/annual_10k_fy2015_2025_v1/
├── 00_reference/
│   ├── source_manifest_v3.csv
│   ├── canonical_manifest.csv
│   ├── canonical_manifest.csv.sha256
│   ├── canonical_manifest.audit.json
│   └── batches/
├── 01_raw/10k/<TICKER>/FY<YEAR>/*.htm
├── 02_work/<batch-id>/
├── 03_receipts/<batch-id>.json
└── 04_reports/
```

No archives, logs, or temporary trees belong in the repository root. Transfer
archives are staged in `/tmp` and removed by the operator after verification.

## Frozen input gates

- manifest rows: 1,752
- selected: 1,743
- missing: 9
- unique selected accessions: 1,743
- unique selected ticker/year pairs: 1,743
- years: FY2015–FY2025
- historical v3 fiscal-year mapping is authoritative

## Installation and preparation

Apply the coexistence schema only after a verified PostgreSQL backup:

```bash
psql "$DB_URL" -v ON_ERROR_STOP=1 \
  -f database/annual_history_v1/V1__Annual_History_Relational_Schema.sql
```

Build the canonical Linux manifest after raw files are present:

```bash
python scripts/annual_history/build_manifest.py \
  --v3-manifest data_history/annual_10k_fy2015_2025_v1/00_reference/source_manifest_v3.csv \
  --companies data_v2/00_reference/approved_companies.csv \
  --raw-root data_history/annual_10k_fy2015_2025_v1/01_raw/10k \
  --output data_history/annual_10k_fy2015_2025_v1/00_reference/canonical_manifest.csv

sha256sum data_history/annual_10k_fy2015_2025_v1/00_reference/canonical_manifest.csv \
  > data_history/annual_10k_fy2015_2025_v1/00_reference/canonical_manifest.csv.sha256

python scripts/annual_history/make_batches.py \
  --manifest data_history/annual_10k_fy2015_2025_v1/00_reference/canonical_manifest.csv \
  --output-dir data_history/annual_10k_fy2015_2025_v1/00_reference/batches \
  --tickers-per-batch 10
```

## Batch execution

Run one batch at a time in tmux:

```bash
bash scripts/annual_history/run_batch.sh batch-001
```

Review its receipt and database row counts. Only then reclaim working space:

```bash
python scripts/annual_history/cleanup_batch.py batch-001 \
  --repo-root /home/ubuntu/projects/retail-intelligence-ai
```

The cleanup command refuses non-committed batches, invalid names, missing
receipts, and paths outside the canonical `02_work` directory.

## Final validation

After every indexed batch is committed:

```bash
python scripts/annual_history/validate_dataset.py
```

Passing final validation marks only the isolated historical dataset as
`validated`. It does not expose it to retrieval.
