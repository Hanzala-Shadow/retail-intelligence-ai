# BGE Large Candidate-Pool Pilot — Final Decision

**Date:** 2026-07-18

**Decision: NO-GO for full-corpus BGE Large embedding.**

No key method passed both predeclared promotion gates. BGE Large did not improve Hit@5, and semantic-only retrieval became worse.

## Verified scope

- Frozen supported questions: 24
- Expected refusals excluded: 5
- Authorized question–chunk pairs: 1,100
- Unique authorized chunks: 1,100
- Previously embedded BGE Large candidates: 94
- Newly embedded candidates: 1,006
- Final BGE Large table rows: 6,006
- Missing authorized embeddings: 0
- Dimension, metadata, norm, text-hash and orphan errors: 0
- BGE Base passes: byte-identical
- BGE Large passes: byte-identical

## Frozen decision gates

- Minimum absolute MRR improvement: 0.03
- Minimum improved groups: 4 of 6
- Hard integrity and reproducibility gates apply first

## Key method comparison

| Method | Base Hit@5 | Large Hit@5 | Base MRR | Large MRR | MRR Δ | Improved groups | Gate |
|---|---:|---:|---:|---:|---:|---:|---|
| `semantic_only_full` | 70.83% | 62.50% | 33.47% | 32.64% | -0.83% | 2 | FAIL |
| `semantic_lexical_rrf_full` | 70.83% | 66.67% | 39.58% | 38.89% | -0.69% | 3 | FAIL |
| `cross_encoder_only_full` | 70.83% | 70.83% | 52.08% | 52.08% | +0.00% | 0 | FAIL |
| `equal_three_way_rrf_full` | 70.83% | 70.83% | 48.26% | 51.04% | +2.78% | 3 | FAIL |
| `hybrid_rank_plus_cross_encoder_rrf_full` | 70.83% | 70.83% | 45.14% | 48.26% | +3.12% | 3 | FAIL |

## Residual failures

### `semantic_only_full`

- Recovered by Large: 10K-V2-I7-003
- Lost under Large: 10K-V2-I7-004, 10K-V2-I8-004, 10K-V2-XC-002
- Large misses: 10K-V2-I7-004, 10K-V2-I8-001, 10K-V2-I8-002, 10K-V2-I8-003, 10K-V2-I8-004, 10K-V2-TC-001, 10K-V2-TC-003, 10K-V2-TC-004, 10K-V2-XC-002

### `semantic_lexical_rrf_full`

- Recovered by Large: 10K-V2-I7-003
- Lost under Large: 10K-V2-I7-004, 10K-V2-I8-004
- Large misses: 10K-V2-I7-004, 10K-V2-I8-001, 10K-V2-I8-002, 10K-V2-I8-003, 10K-V2-I8-004, 10K-V2-TC-001, 10K-V2-TC-003, 10K-V2-TC-004

### `cross_encoder_only_full`

- Recovered by Large: none
- Lost under Large: none
- Large misses: 10K-V2-I7-002, 10K-V2-I7-004, 10K-V2-I8-002, 10K-V2-I8-003, 10K-V2-TC-003, 10K-V2-TC-004, 10K-V2-XC-002

### `equal_three_way_rrf_full`

- Recovered by Large: none
- Lost under Large: none
- Large misses: 10K-V2-I7-002, 10K-V2-I7-004, 10K-V2-I8-002, 10K-V2-I8-003, 10K-V2-TC-001, 10K-V2-TC-003, 10K-V2-TC-004

### `hybrid_rank_plus_cross_encoder_rrf_full`

- Recovered by Large: none
- Lost under Large: none
- Large misses: 10K-V2-I7-002, 10K-V2-I7-004, 10K-V2-I8-002, 10K-V2-I8-003, 10K-V2-TC-001, 10K-V2-TC-003, 10K-V2-TC-004

## Semantic-only interpretation

- BGE Large recovered `10K-V2-I7-003`.
- It lost `10K-V2-I7-004`, `10K-V2-I8-004`, and `10K-V2-XC-002`.
- Hit@5 fell from 70.83% to 62.50%.
- `TC-003` moved from rank 35 to rank 9, but still missed top five.
- `TC-004` moved from rank 46 to rank 37, but remained a deep failure.

## Final conclusion

The larger encoder changes rankings but does not improve top-five coverage. The remaining failures are better addressed with temporal decomposition, broader retrieval, neighbor expansion, evidence aggregation, and human adjudication. A 3–4 day full-corpus BGE Large run is not supported by this controlled pilot.

## Machine-readable evidence

- `bge_large_pilot_final_comparison.json`
- `bge_large_pilot_method_comparison.csv`
- `bge_large_pilot_question_comparison.csv`
