# 10-K RAG Benchmark V2 Validation Report

Date: 2026-07-15

Source package: `SAMPLES_DB/diverse_500_chunks_20260714T235643Z.tar.gz`

Output benchmark: `data/00_reference/rag_eval_questions_10k_v2.csv`

Validation status: **PASS**

## Feedback Revision Applied

This revision incorporates the benchmark feedback received after the first V2 draft: question text now reads like business analyst queries, item-number references were removed from question text, full company names are used instead of ticker-only wording, two implementation-style refusal tests were replaced with business refusals, two cross-company questions now use companies outside the single-source set, and two time-change questions now test non-MD&A sections.

| Feedback check | Result |
|---|---|
| Item-number references in question text | 0 |
| Cross-company rows using only non-single-source companies | 10K-V2-XC-001, 10K-V2-XC-002 |
| Non-MD&A time-change rows | 10K-V2-TC-003 (Item_1A/Item_1A); 10K-V2-TC-004 (Item_8/Item_8) |
| Replacement refusal rows | 10K-V2-REF-003 and 10K-V2-REF-005 |

## Safety Boundary

This work touched only the proposed 10-K benchmark and derived review artifacts. It did not touch ESG pipeline code, ESG indexes, generated ESG files, the database, migrations, Drive, Git branches, commits, or pushes.

## Final Group Distribution

| Question group | Count |
|---|---|
| Item_1 | 4 |
| Item_1A | 4 |
| Item_7 | 4 |
| Item_8 | 4 |
| cross_company | 4 |
| refusal | 5 |
| time_change | 4 |

Supported questions: 24
Refusal questions: 5
Total unique question IDs: 29

## Source Coverage

Unique referenced chunks: 27
Total chunk references, including multi-source reuse: 30

| Coverage type | Values |
|---|---|
| Companies | `ABG`, `ASO`, `BJ`, `BRLT`, `CROX`, `CURV`, `DKS`, `EBAY`, `FLWS`, `HD`, `KSS`, `LE`, `LESL`, `LOW`, `LULU`, `NWL`, `OLLI`, `ORBS`, `RDNW`, `REAL`, `SNBR`, `TGT`, `VFC`, `WOOF`, `WSM` |
| Filing years | `2024`, `2025`, `2026` |
| Sections | `Item_1`, `Item_1A`, `Item_7`, `Item_8` |
| Token range | `65` to `500` tokens |
| Under-500-token chunks | 3 unique referenced chunks |
| Exactly-500-token chunks | 24 unique referenced chunks |

### Company Counts

| Ticker | Referenced unique chunks |
|---|---|
| ABG | 1 |
| ASO | 1 |
| BJ | 1 |
| BRLT | 1 |
| CROX | 1 |
| CURV | 1 |
| DKS | 1 |
| EBAY | 2 |
| FLWS | 1 |
| HD | 1 |
| KSS | 1 |
| LE | 1 |
| LESL | 1 |
| LOW | 1 |
| LULU | 1 |
| NWL | 1 |
| OLLI | 1 |
| ORBS | 1 |
| RDNW | 1 |
| REAL | 1 |
| SNBR | 1 |
| TGT | 1 |
| VFC | 1 |
| WOOF | 2 |
| WSM | 1 |

### Year Counts

| Filing year | Referenced unique chunks |
|---|---|
| 2024 | 9 |
| 2025 | 7 |
| 2026 | 11 |

### Section Counts

| Section | Referenced unique chunks |
|---|---|
| Item_1 | 4 |
| Item_1A | 10 |
| Item_7 | 7 |
| Item_8 | 6 |

## Validation Results

| Check | Result |
|---|---|
| Exactly 29 rows | PASS |
| Exactly 29 unique question IDs | PASS |
| Exactly 24 supported + 5 refusal questions | PASS |
| Required group counts | PASS |
| Every referenced chunk exists in MANIFEST.csv | PASS |
| Every referenced text file exists in archive and matches file_sha256 | PASS |
| No empty questions or expected answers | PASS |
| No duplicate question text | PASS |
| Supporting passages found in referenced chunk text | PASS |
| Durable provenance beyond numeric chunk IDs populated | PASS |
| Refusal rows have no fabricated supporting chunks | PASS |
| review_status=draft_for_team_review for all rows | PASS |
| No item-number references in question text | PASS |
| At least two cross-company questions use non-single-source companies | PASS |
| At least two time-change questions use non-MD&A sections | PASS |

No validation errors were found.

## Evidence Grounding Method

Every non-refusal row is supported only by text from `SAMPLES_DB/diverse_500_chunks_20260714T235643Z.tar.gz`. The `supporting_passages` column contains normalized excerpts validated as substrings of the referenced chunk text. Expected answers were drafted from those excerpts and do not use SEC URLs, live database state, or unsupported external assumptions.

Multi-source rows preserve source ordering with `|` delimiters across chunk IDs, accession numbers, tickers, filing years, section codes, chunk indexes, source files, file hashes, token counts, and supporting passages.

## Comparison With Existing Benchmark

Existing benchmark: `data/00_reference/rag_eval_questions_10k.csv`

| Dimension | Existing benchmark | V2 revised benchmark |
|---|---|---|
| Rows | 29 | 29 |
| Supported questions | 24 | 24 |
| Refusal questions | 5 | 5 |
| Exact duplicate question text overlap | 0 | 0 |
| Review status | ready_for_review=29 | draft_for_team_review=29 |
| Source artifact | mixed earlier artifacts | SAMPLES_DB/diverse_500_chunks_20260714T235643Z.tar.gz |
| Durable provenance columns | not present in old structure | accession, ticker, year, section, chunk index, source file, file hash, token count |

V2 keeps the agreed benchmark shape but now better tests real retrieval behavior: users do not pre-identify the section in question text, cross-company coverage is broader, and time-change coverage includes risk/accounting evidence outside MD&A.

## Remaining Limitations

- V2 is proposal-only and has not been human-approved by the team.
- Automated validation confirms file/hash/provenance integrity and passage grounding; it does not replace human review of business usefulness or answer quality.
- The source package is a 500-chunk sample from `rag_eligible_10k_chunks`, not the full 10-K corpus.
- Some questions intentionally use table-derived or table-adjacent evidence and are labeled in `notes`.
- Numeric `chunk_id` values remain useful for current database lookup, but V2 should be treated as durable through the added accession/file/hash provenance fields.

## Readiness Decision

V2 revised is ready for team review if the team accepts draft, sample-backed benchmark questions. It should not be treated as human-approved until reviewers check the questions, expected answers, and refusal choices.
