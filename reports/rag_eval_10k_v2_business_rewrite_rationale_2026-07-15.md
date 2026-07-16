# 10-K RAG Benchmark V2 Business Rewrite Rationale

Date: 2026-07-15

CSV updated: `data/00_reference/rag_eval_questions_10k_v2.csv`

## Why We Changed It

The benchmark is meant to test whether a retail intelligence system can answer the kinds of questions a business analyst would naturally ask. The prior version was structurally strong, but some prompts still sounded like filing-review or evaluation prompts. That can make retrieval look better than it is because the question wording may steer the system toward a filing section or benchmark artifact instead of testing natural discovery.

The rewrite makes the question text more realistic while preserving the benchmark evidence. A business analyst usually asks about operating exposure, margin drivers, customer impact, financial obligations, asset write-down risk, supply-chain disruption, peer differences, or changes over time. They generally do not ask by item number, ticker symbol, database artifact, SEC URL, or benchmark-internal language.

## What Changed

- Rewrote all 29 question prompts in business-user language.
- Removed evaluation jargon such as "benchmark" from user-facing question text.
- Kept full company names in the question text, including `RideNow Group, Inc.` instead of ticker-only wording.
- Preserved the REF-003 replacement as a real out-of-corpus company question about Shein Group.
- Preserved the REF-005 replacement as a real out-of-scope year question about Walmart Inc.'s 2020 annual filing.
- Kept cross-company questions broad enough to test retrieval, with at least two rows using companies outside the single-source set.
- Kept time-change coverage outside MD&A through eBay risk-disclosure change and Petco impairment/accounting change questions.
- Lightly cleaned expected-answer wording where ticker shorthand made the answer less business-readable.

## What Stayed The Same

- Question IDs
- Question groups
- Expected tickers and years
- Required document type
- Required section metadata
- Supporting chunk IDs
- Supporting accession numbers
- Supporting source files
- Supporting file hashes
- Supporting token counts
- Supporting passages
- Refusal flags and refusal reasons

## Business-Analysis Standard Applied

Each prompt should be something a business, strategy, finance, operations, or risk analyst could plausibly type into a retail intelligence tool. The question should ask for the business meaning of the filing evidence without telling the retrieval system which section to search.

Examples of the direction:

- Filing-style: "What non-cancellable commitments does Lowe's Companies, Inc. disclose?"
- Business-style: "What future purchase or service commitments has Lowe's Companies, Inc. made that could affect cash obligations?"

- Filing-style: "How do Asbury Automotive Group, Inc. and Ollie's Bargain Outlet Holdings, Inc. differ in their treatment of impairment testing evidence?"
- Business-style: "How do Asbury Automotive Group, Inc. and Ollie's Bargain Outlet Holdings, Inc. explain risks around asset values and possible write-downs?"

## Readiness View

The updated CSV is better aligned with the main requirement: it now tests natural business retrieval rather than section-aware filing lookup. The metadata remains strong enough for evaluation, while the question text is less likely to give the system artificial hints.
