# Generic Decomposer v1.3 — Novel-Five Post-Fix Runbook

This patch expands generic language coverage without changing the five frozen
questions, retrieval models, embedding index, ranking policy, or evidence
allocation policy.

## Generic changes

- Distinguish an explicitly named 10-K filing year from a fiscal content year.
- Add safe unique leading-brand aliases derived from legal company names.
- Recognize Item 1 business/service expansion and strategic progress language.
- Recognize Item 7 SG&A, deleverage, overhead, wage, and operating-expense language.
- Recognize Item 8 lease maturity/payment and debt/borrowing/credit-facility language.
- Recognize plural risk wording.
- Permit multiple independent claims routed to the same Item 8 source.

No ticker, question ID, expected answer, gold chunk, or supporting passage is
hard-coded in the implementation.

## Install and test

```bash
cd ~/projects/retail-intelligence-production-retrieval
tar -xzf /tmp/generic_decomposer_v1_3_novel5_20260721.tar.gz
source ~/projects/retail-intelligence-ai/venv/bin/activate
```

```bash
python -m py_compile \
  src/query_decomposition.py \
  src/decomposed_query_api.py \
  src/detector_coverage_audit.py \
  tests/test_query_decomposition.py \
  tests/test_decomposed_query_api.py
```

```bash
python -m pytest -q \
  tests/test_query_decomposition.py \
  tests/test_decomposed_query_api.py \
  tests/test_detector_coverage_audit.py \
  tests/test_generic_decomposition_benchmark.py
```

```bash
python -m pytest -q
```

## Frozen paths

```bash
NOVEL5_DIR="reports/week4_retrieval/novel5_corpus_backed_20260721"
PREFX_DIR="reports/week4_retrieval/novel5_first_frozen_run_20260721"
POSTFIX_DIR="reports/week4_retrieval/novel5_postfix_decomposer_v1_3_20260721"
VAL50_DIR="reports/week4_retrieval/generalization_validation50_20260721/generalization_validation50_20260721"
VAL50_POSTFIX_DIR="reports/week4_retrieval/generalization_validation50_20260721/detector_postfix_v1_3"

mkdir -p "$POSTFIX_DIR" "$VAL50_POSTFIX_DIR"
```

Preserve the pre-fix detector checksum:

```bash
sha256sum "$PREFX_DIR/detector_coverage.json"
```

Expected frozen SHA-256:

```text
7954deadba3effc68c42015b0b90024c4f5daf905dbfda0c2119691d1f48c45c
```

## Gate 1 — Original validation-50 must remain exact

```bash
PGDATABASE="retail_pipeline" \
scripts/with_temporary_readonly_pg_role.sh -- \
python scripts/run_detector_coverage_audit.py \
  --questions "$VAL50_DIR/validation50_questions.csv" \
  --output "$VAL50_POSTFIX_DIR/detector_coverage.json" \
  --expected-supported 50 \
  --expected-refusals 0
```

```bash
python - "$VAL50_POSTFIX_DIR/detector_coverage.json" <<'PY'
import json, sys
report=json.load(open(sys.argv[1],encoding="utf-8"))
overall=report["summary"]["overall"]
print(json.dumps(overall,indent=2))
assert overall["routing_exact"] == 50
assert overall["resolved"] == 50
print("PASS: validation-50 remains 50/50 exact")
PY
```

## Gate 2 — Frozen novel five post-fix detector

```bash
PGDATABASE="retail_pipeline" \
scripts/with_temporary_readonly_pg_role.sh -- \
python scripts/run_detector_coverage_audit.py \
  --questions "$NOVEL5_DIR/novel5_questions.csv" \
  --output "$POSTFIX_DIR/detector_coverage.json" \
  --expected-supported 5 \
  --expected-refusals 0
```

```bash
python - "$POSTFIX_DIR/detector_coverage.json" <<'PY'
import json, sys
report=json.load(open(sys.argv[1],encoding="utf-8"))
overall=report["summary"]["overall"]
print(json.dumps(overall,indent=2))
assert overall["routing_exact"] == 5
assert overall["resolved"] == 5
print("PASS: frozen novel five resolves 5/5")
PY
```

Do not continue to retrieval unless both gates pass.

## Gate 3 — Frozen production retrieval

```bash
PGDATABASE="retail_pipeline" \
scripts/with_temporary_readonly_pg_role.sh -- \
python scripts/run_generic_decomposition_benchmark.py \
  --questions "$NOVEL5_DIR/novel5_questions.csv" \
  --output "$POSTFIX_DIR/retrieval.csv" \
  --details "$POSTFIX_DIR/retrieval_details.json" \
  --manifest "$POSTFIX_DIR/run_manifest.json" \
  --expected-supported 5 \
  --expected-refusals 0
```

## Sparse-gold diagnostic

```bash
CHUNK_METADATA="reports/week4_retrieval/generalization_validation50_20260721/retrieval_v1_1/rag_eligible_10k_chunks.csv"
EVAL_DIR="$POSTFIX_DIR/sparse_gold_evaluation"

python -m src.rag_eval_harness \
  --questions "$NOVEL5_DIR/novel5_questions.csv" \
  --retrieval "$POSTFIX_DIR/retrieval.csv" \
  --chunk-metadata "$CHUNK_METADATA" \
  --text-field chunk_text \
  --out "$EVAL_DIR"
```

```bash
cat "$EVAL_DIR/rag_eval_report.md"
```

The sparse-gold result is diagnostic only. Any retrieved alternative evidence
must be blindly adjudicated before authoritative graded metrics are reported.

