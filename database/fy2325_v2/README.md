# FY2325 v2.16 database staging package

This package loads the frozen FY2023–FY2025 v2.16 corpus alongside the current
database. It does not overwrite the existing relational corpus or embedding
table.

## Safety design

- `coverage_year` is loaded only from the frozen selection manifest/derived
  v2 records.
- Source string IDs are preserved as `source_section_id` and
  `source_chunk_id`.
- Numeric surrogate keys preserve compatibility with production retrieval
  code that currently calls `int(chunk_id)`.
- NPZ vectors are joined by `source_chunk_id`, never by row position.
- Every shard checksum, vector shape, dtype, norm, and both text hashes are
  checked before insertion.
- The staging RAG view requires a matching embedding.
- Existing production tables and `rag_eligible_10k_chunks` remain untouched
  until the explicit cutover file is run.

## Expected frozen gates

Companies 190; filings/documents 561; coverage 2023=186, 2024=186,
2025=189; sections 13,455; chunks 224,561; included/embedded 158,570;
excluded 65,991.

## Installation and dry verification

Copy this directory to the repository, verify `SHA256SUMS`, activate
`~/projects/retail-intelligence-ai/venv`, and export
`PYTHONPATH="$PWD/src"`.

Run unit/static tests:

```bash
pytest -q <package-root>/tests/test_package_contract.py
```

Apply only the coexistence schema:

```bash
psql "$DB_URL" -v ON_ERROR_STOP=1 \
  -f <package-root>/V2__FY2325_Coexistence_Schema.sql
```

Confirm that the old active row count remains 89,335 before loading.

## Staged loading

From `~/projects/retail-intelligence-ai`:

```bash
python <package-root>/load_fy2325_v2_staging.py relational
python <package-root>/load_fy2325_v2_staging.py embeddings
python <package-root>/validate_fy2325_v2_staging.py
```

Run long stages in separate tmux sessions with logs and recorded exit codes.
Do not run `all` for the first production load; verify each stage separately.

Build the index only after validation:

```bash
psql "$DB_URL" -v ON_ERROR_STOP=1 \
  -f <package-root>/build_fy2325_v2_hnsw.sql
```

## Production compatibility before cutover

In the production retrieval repository, change only:

```python
EMBEDDING_TABLE = "benchmark_embeddings_bge_base_en_v15_v216"
```

The compatibility view continues to expose numeric `chunk_id`. Add
`source_chunk_id` to citations later as a separate enhancement; do not replace
numeric IDs throughout the dirty worktree during this database load.

Run the production unit tests and retrieval smoke tests against
`rag_eligible_10k_chunks_v216_staging` and the v216 embedding view before
executing `cutover_fy2325_v2.sql`.

## Cutover and cleanup

Cutover is a separate explicit transaction. Do not run it until staging,
HNSW, production-code tests, and retrieval smoke tests pass.

Do not delete old database rows or stale section/chunk roots until:

1. cutover succeeds;
2. active RAG count is 158,570;
3. retrieval smoke tests pass;
4. a post-cutover backup is verified;
5. exact stale paths are quarantined and approved.
