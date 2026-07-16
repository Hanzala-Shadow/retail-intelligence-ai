# ESG Phase 3 Integration Handoff

Date: 2026-07-16

## Scope

This branch integrates the reviewed ESG parsing, sectioning, chunking,
provenance, source-governance, QA, and vector-eligibility contracts into the
current `Master_Phase_3` baseline. It preserves the existing 10-K RAG policy,
pgvector, embedding benchmark, image extraction, answer validation, and
regression-test work already present on Master.

## Migration Contract

- `V13__ESG_Provenance.sql` adds physical section-instance identity, stable
  external chunk/source identities, and citation-validation metadata while
  preserving existing numeric database IDs.
- `V14__ESG_Short_Evidence_Chunks.sql` records short-evidence and merge policy
  metadata.
- PDF page bounds remain nullable. ESG PDF evidence uses page and character
  bounds; HTML filings do not receive invented page numbers.
- Both migrations applied successfully twice to a disposable embedded
  PostgreSQL database after the schema-producing V1-V6 migrations. Master’s
  V7-V12 data-dependent 10-K migrations were not replayed against the empty
  database; their existing frozen-corpus validation remains authoritative.
- The migrations have not been applied to EC2 or any shared database.

## Validation

- Full integrated Python suite: 79 passed.
- Python compilation: passed.
- Fast runner scoped `-WhatIf`: passed and preserved the quoted PDF filename.
- `git diff --check`: passed.
- Existing-corpus provenance validation: 478 documents, 23,516 section
  instances, and 39,269 chunks; all chunk IDs unique, all citations
  `verified_exact`, zero unchunked sections, and zero provenance errors.
- Legacy diagnostic vector reconciliation: 39,269 rows, zero missing IDs, zero
  obsolete IDs, and zero duplicate IDs. The strict production build correctly
  remains blocked until the new full-corpus layout audit is generated.
- Benchmark contract: 24 benchmark questions across six groups plus five
  refusal questions. The 24 benchmark rows reflect Ayse's 2026-07-16 approval;
  refusal rows remain marked for team review.

## Generated Outputs

This integration intentionally does not rewrite generated ESG parse, section,
chunk, QA, accepted-company, image, or vector-manifest files. The currently
tracked Phase 3 accepted-company manifest predates the latest OCR, source, and
QA corrections and must not be treated as the final ESG eligibility authority.

After review, run the guarded pipeline in resume mode from the integration
commit, rebuild the ESG vector manifest and accepted-company manifest, and
reconcile all counts before database loading or embedding generation.

## Remaining Gate

The branch is ready for code and migration review. Full-corpus regeneration,
local PostgreSQL staging load with production-like data, server migration
approval, database loading, and embeddings remain separate controlled steps.
