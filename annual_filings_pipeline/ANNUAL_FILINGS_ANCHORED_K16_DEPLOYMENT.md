# Annual-filings anchored K16 deployment

This patch adds the frozen retrieval policy behind `RAG_RETRIEVAL_POLICY`.
The legacy path remains available for shadow comparison and immediate rollback.

## Frozen target

- BGE Base first-stage embeddings are unchanged.
- MiniLM-L12 combined-512 supplies six protected anchors.
- BGE reranker v2-M3 combined-512 supplies balanced expansion.
- Exact-text/source identity deduplication is applied.
- The final evidence limit is 16.
- No benchmark question-ID overrides, gold fields, or database writes are used.

## Resource warning

The audited EC2 host has 3.7 GiB RAM and no swap. The committed configuration
uses sequential reranker loading to avoid keeping MiniLM-L12 and BGE-M3 resident
at the same time. This protects memory but adds cold-model latency. Run the
model and end-to-end latency smoke tests before promotion. Do not switch to
resident lifecycle on this host without a measured memory margin.

## Activation

Use the existing virtual environment and repository root:

```bash
export RAG_RETRIEVAL_POLICY="balanced_anchored_round_robin_k16"
export RAG_ANCHORED_CONFIG="config/retrieval_anchored_k16_v1.json"
```

First run static and database checks. Model loading is deliberately separate:

```bash
python scripts/verify_anchored_server.py
python scripts/verify_anchored_server.py --check-db
python scripts/verify_anchored_server.py --load-models
```

Then execute the supplied unit/regression tests and a small read-only shadow
question set. Capture peak RSS and latency. Do not run Final80 for tuning.

Verify that the pure selector exactly replays the already-frozen audit (this is
reproduction, not tuning):

```bash
python scripts/validate_anchored_selector_replay.py \
  --selection-audit reports/final80_nova_pro_20260729/inputs/final80_selection_audit.jsonl \
  --expected-questions 80
```

## Rollback

```bash
export RAG_RETRIEVAL_POLICY="legacy_v1"
unset RAG_ANCHORED_CONFIG
```

Restart only the retrieval service/process after changing the feature flag.
The database, BGE Base embeddings and HNSW index are not modified by this patch.
