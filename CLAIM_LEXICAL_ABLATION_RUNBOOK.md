# Generic Claim-Lexical Ablation Runbook

This package adds one generic candidate strategy while preserving the current
section-adaptive semantic implementation as the frozen control.

- Control: `section_adaptive_semantic_control`
- Candidate: `section_adaptive_plus_claim_lexical`
- Models remain fixed.
- Lexical retrieval uses only the decomposed claim key and the already resolved
  ticker, filing, document type, accession, and section filters.
- Gold passages, expected answers, gold chunk IDs, and evaluation labels are not
  available to retrieval.
- This 50-question run is development evidence only. It cannot directly change
  production.

## Install and verify

```bash
cd ~/projects/retail-intelligence-production-retrieval
tar -xzf /tmp/generic_claim_lexical_ablation_20260721.tar.gz
source ~/projects/retail-intelligence-ai/venv/bin/activate
python -m py_compile \
  scripts/run_generic_multiview_requirement_benchmark.py \
  tests/test_generic_multiview_requirement_benchmark.py
python -m pytest -q tests/test_generic_multiview_requirement_benchmark.py
python -m pytest -q
```

## Run the frozen comparison

```bash
AUTHORING_DIR="reports/week4_retrieval/generalization_validation50_20260721/generalization_validation50_20260721"
QRELS="reports/week4_retrieval/generalization_validation50_20260721/human_confirmed_graded_qrels_v1/graded_requirement_qrels.csv"
RUN_DIR="reports/week4_retrieval/generalization_validation50_20260721/generic_claim_lexical_v1"
mkdir -p "$RUN_DIR"

PGDATABASE="retail_pipeline" \
scripts/with_temporary_readonly_pg_role.sh -- \
python scripts/run_generic_multiview_requirement_benchmark.py \
  --questions "$AUTHORING_DIR/validation50_questions.csv" \
  --output "$RUN_DIR/retrieval.csv" \
  --details "$RUN_DIR/retrieval_details.json" \
  --manifest "$RUN_DIR/run_manifest.json" \
  --expected-supported 50 \
  --expected-refusals 0

python scripts/run_graded_requirement_evaluation.py \
  --questions "$AUTHORING_DIR/validation50_questions.csv" \
  --retrieval "$RUN_DIR/retrieval.csv" \
  --qrels "$QRELS" \
  --output-dir "$RUN_DIR/graded_evaluation"

cat "$RUN_DIR/graded_evaluation/graded_requirement_eval.md"
```

## Promotion rule

Do not promote the candidate unless all of these hold after blind supplemental
adjudication of every previously unjudged top-5 candidate:

- hard gates pass;
- Direct Hit@5 remains 1.00;
- Judged@5 is 1.00;
- overall complete requirement coverage improves by at least 0.05;
- cross-company complete coverage reaches at least 0.70;
- overall MRR and nDCG regress by no more than 0.02;
- no question-group MRR regresses by more than 0.02;
- the independent sealed 100-question set confirms the selected policy.

If `Judged@5` is below 1.00 in the first evaluation, the result is incomplete,
not a failure. Blind and adjudicate only the new unjudged top-5 candidates, merge
those judgments into a versioned qrels file, and rerun the same evaluation.
