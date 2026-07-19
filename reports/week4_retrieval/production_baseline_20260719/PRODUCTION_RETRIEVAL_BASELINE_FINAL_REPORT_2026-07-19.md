# Production 10-K Retrieval Baseline — Final Report

**Date:** 2026-07-19  
**Branch:** `Phase_4_Hanzala_Production_Retrieval`  
**Implementation commits:** `db710cb`, `c483839`

## Executive verdict

The locked BGE Base production retrieval path is implemented, tested, live-smoke validated, and reproducible. It is suitable as the production baseline subject to one explicitly documented benchmark-contract exception.

The production run achieved **Hit@5 70.83%**, **MRR@5 52.08%**, **Recall@5 66.67%**, and **nDCG@5 53.03%** across the 24 supported questions. Two complete executions produced byte-identical retrieval output and byte-identical per-question evaluation output.

The evaluator's formal overall gate is `FAIL`, but this is not a retrieval failure. Wrong-document retrieval passed with zero violations and gold ticker/year/section integrity passed. The sole failure is an existing elided supporting quote for `10K-V2-XC-004`, chunk `280029`; the quote contains an ellipsis and therefore cannot be found as an exact contiguous substring. That gold chunk was retrieved at rank 1.

The model-selection decision is `NOT_EVALUATED` because this production baseline intentionally evaluated only BGE Base. The earlier controlled BGE Large pilot remains a `NO-GO` for full-corpus embedding.

## Locked production policy

| Component | Locked value |
|---|---|
| Corpus | `public.rag_eligible_10k_chunks` |
| Corpus rows | 89,335 |
| Embedding table | `public.benchmark_embeddings_bge_base_en_v15` |
| Embedding coverage | 89,335 / 89,335 |
| Bi-encoder | `BAAI/bge-base-en-v1.5` |
| Bi-encoder revision | `a5beb1e3e68b9ab74eb54cfd186867f64f240e1a` |
| Embedding dimension | 768 |
| Candidate retrieval | Top 20 semantic candidates per approved source |
| Section routing | Required hard filter |
| Cross-encoder | `cross-encoder/ms-marco-MiniLM-L6-v2` |
| Final output | Top 5 evidence chunks |
| Multi-source merge | Deterministic round-robin |

Routing uses ticker, filing year, document type, accession number, and required section. Gold `supporting_chunk_ids` are never used for candidate selection; they are used only by the evaluation harness after retrieval.

## Implementation and verification

Commit `db710cb` added the locked query entry point, focused tests, dependency lock file, and production documentation. Commit `c483839` added the frozen 24-question baseline runner and its contract tests.

Validation completed:

- Query API and evaluation regression suite: 62 tests passed.
- All 24 supported benchmark questions resolved to live eligible source chunks.
- Five refusal questions were excluded from retrieval scoring.
- Live corpus and BGE Base embedding counts both equal 89,335.
- Live ASO smoke request returned five correctly routed Item 1 chunks and retrieved the known gold chunk at rank 4.
- Smoke runtime including cold model loads: 14.82 seconds; peak RSS approximately 1.01 GiB.

## Official baseline execution

| Property | Pass 1 | Pass 2 |
|---|---:|---:|
| Supported questions | 24 | 24 |
| Retrieval rows | 120 | 120 |
| Wall time | 1m 36.27s | 1m 36.05s |
| Peak RSS | 1,053,372 KiB | 1,053,672 KiB |
| Exit status | 0 | 0 |

Both retrieval CSV files have SHA-256:

`a2474b1d4bdd0660b948849f954828b5d880ab7e8cb059ee587ea4638dc52dd6`

The files are byte-identical. Both contain contiguous ranks 1–5, five unique chunks per question, 24 unique supported question IDs, and exactly 120 rows. The two evaluator per-question CSV files are also byte-identical.

The metadata catalogue contained all 89,335 eligible chunks with `chunk_id`, `doc_type`, `ticker`, `filing_year`, `section_code`, and original `chunk_text`. Its SHA-256 is:

`42d85c8572fb4bfbce2b567813f90fe4d5614c1dc52e4748dc62237602afc504`

## Retrieval metrics

| Group | Hit@5 | MRR@5 | Recall@5 | nDCG@5 |
|---|---:|---:|---:|---:|
| Item 1 | 100.00% | 68.75% | 100.00% | 76.54% |
| Item 1A | 100.00% | 87.50% | 100.00% | 90.77% |
| Item 7 | 50.00% | 33.33% | 50.00% | 37.50% |
| Item 8 | 50.00% | 33.33% | 50.00% | 37.50% |
| Cross-company | 75.00% | 75.00% | 50.00% | 52.59% |
| Time-change | 50.00% | 14.58% | 50.00% | 23.27% |
| **Overall** | **70.83%** | **52.08%** | **66.67%** | **53.03%** |

These headline results exactly match the earlier BGE Base `cross_encoder_only_full` Hit@5 and MRR figures, showing that the locked production path preserved the strongest validated BGE Base result.

## Residual top-five misses

Seven questions did not retrieve a supporting gold chunk in the final top five:

- `10K-V2-I7-002`
- `10K-V2-I7-004`
- `10K-V2-I8-002`
- `10K-V2-I8-003`
- `10K-V2-XC-002`
- `10K-V2-TC-003`
- `10K-V2-TC-004`

These failures are concentrated in Item 7, Item 8, cross-company comparison, and time-change reasoning. They should be addressed later through temporal decomposition, broader retrieval, neighbor expansion, evidence aggregation, and human adjudication—not by a full-corpus BGE Large run.

## Hard-gate interpretation

| Gate | Result | Evidence |
|---|---|---|
| Wrong document type | PASS | 0 violations across 120 retrieved chunks |
| Gold ticker/year/section integrity | PASS | 90 positional metadata checks, 0 mismatches |
| Supporting passage present | FAIL | One elided quote for `10K-V2-XC-004`, chunk `280029` |
| Overall evaluator gate | FAIL | Inherited from the passage-validation failure |

The quote defect must not be silently edited to force a pass. It requires benchmark-owner review and either replacement with an exact contiguous quote or an explicit policy for elided quotations. Until then, report the production retrieval result as technically validated with one known benchmark-contract exception.

## BGE Large decision carried forward

The controlled 1,100-candidate BGE Large pilot did not justify full-corpus embedding. Semantic-only Hit@5 fell from 70.83% to 62.50%, semantic-plus-lexical Hit@5 fell to 66.67%, and no key method passed both the predeclared MRR and improved-group gates. The production default therefore remains BGE Base plus the pinned cross-encoder.

## Evidence artifacts

The evidence package is under:

`reports/week4_retrieval/production_baseline_20260719/`

It contains both retrieval passes, manifests, logs, both evaluator outputs, the metadata catalogue, and `SHA256SUMS`. All recorded checksums verified successfully. The large metadata export and runtime logs should be retained outside Git unless repository policy explicitly permits large generated evidence files.

## Final disposition

**GO for the locked BGE Base production baseline, with one documented benchmark-contract exception.**

Do not promote BGE Large, do not modify the frozen gold contract without review, and do not claim that all residual top-five misses are solved. The next controlled activity is benchmark-owner resolution of the single elided quote, followed by re-evaluation without changing the retrieval outputs.
