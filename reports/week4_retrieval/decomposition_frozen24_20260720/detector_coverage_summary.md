# Frozen-24 Question-Only Detector Coverage — Untuned Audit

**Status:** VERIFIED COMPLETE

**Evaluation label:** in-sample

**Mode:** question-only detector coverage using approved corpus metadata; no retrieval or model execution.

## Controls

- Detector input contained question text only.
- Expected routing fields were used only for post-detection scoring.
- Expected answers, supporting chunk IDs, passages and chunk indexes were excluded.
- Database access was read-only and limited to eligible filing/company metadata.
- BGE Base and the cross-encoder were not loaded.
- Pass 1 and pass 2 were byte-identical.

## Overall result

| Metric | Result |
|---|---:|
| Supported questions | 24 |
| Exact entities | 14/24 |
| Exact raw filing-year sets | 3/24 |
| Fully resolved | 3/24 |
| Exact routed sections | 1/24 |
| Exact full routes | 1/24 |

Only `10K-V2-TC-003` achieved an exact question-only route.

## Group results

| Group | Questions | Entity exact | Resolved | Exact route |
|---|---:|---:|---:|---:|
| Item_1 | 4 | 2 | 0 | 0 |
| Item_1A | 4 | 1 | 0 | 0 |
| Item_7 | 4 | 4 | 0 | 0 |
| Item_8 | 4 | 3 | 0 | 0 |
| cross_company | 4 | 1 | 0 | 0 |
| time_change | 4 | 3 | 3 | 1 |

## Untuned findings

- Nineteen questions omitted a filing year and returned `YEAR_UNRESOLVED`.
- Two questions exposed content-year versus filing-year mismatch.
- `10K-V2-TC-001` treated content years 2024/2025 as two filings, while its declared route is the single 2026 filing.
- `10K-V2-TC-002` treated the Timberland brand as ticker `TBHC` and mapped revenue to `Item_8` rather than declared `Item_7`.
- Generic aliases caused false entities: `digital → DBGI`, `group → GPI`, and `timberland → TBHC`.
- Some company aliases were not resolved, including BOBS and GRWG in the cross-company questions.
- `10K-V2-TC-004` had filing years but no supported impairment claim mapping and returned `SECTION_UNRESOLVED`.
- These are untuned detector-coverage findings, not retrieval-ranking failures.

## Question inventory

| Question | Entities | Years | Claims | Error | Year interpretation | Exact route |
|---|---|---|---|---|---|---:|
| 10K-V2-I1-001 | ASO | — | — | YEAR_UNRESOLVED | filing_year_omitted | false |
| 10K-V2-I1-002 | DBGI, LULU | — | — | YEAR_UNRESOLVED | filing_year_omitted | false |
| 10K-V2-I1-003 | BJ, DBGI | — | — | YEAR_UNRESOLVED | filing_year_omitted | false |
| 10K-V2-I1-004 | REAL | — | — | YEAR_UNRESOLVED | filing_year_omitted | false |
| 10K-V2-I1A-001 | BRLT, GPI | — | — | YEAR_UNRESOLVED | filing_year_omitted | false |
| 10K-V2-I1A-002 | DBGI | — | — | YEAR_UNRESOLVED | filing_year_omitted | false |
| 10K-V2-I1A-003 | GPI, RDNW | — | — | YEAR_UNRESOLVED | filing_year_omitted | false |
| 10K-V2-I1A-004 | CROX | — | business | YEAR_UNRESOLVED | filing_year_omitted | false |
| 10K-V2-I7-001 | CURV | 2024 | — | SECTION_UNRESOLVED | content_year_filing_year_mismatch | false |
| 10K-V2-I7-002 | SNBR | — | gross margin | YEAR_UNRESOLVED | filing_year_omitted | false |
| 10K-V2-I7-003 | NWL | — | — | YEAR_UNRESOLVED | filing_year_omitted | false |
| 10K-V2-I7-004 | WSM | — | — | YEAR_UNRESOLVED | filing_year_omitted | false |
| 10K-V2-I8-001 | HD | — | — | YEAR_UNRESOLVED | filing_year_omitted | false |
| 10K-V2-I8-002 | LOW | — | — | YEAR_UNRESOLVED | filing_year_omitted | false |
| 10K-V2-I8-003 | ABG, GPI | — | risk | YEAR_UNRESOLVED | filing_year_omitted | false |
| 10K-V2-I8-004 | LESL | — | — | YEAR_UNRESOLVED | filing_year_omitted | false |
| 10K-V2-XC-001 | DBGI, KSS | — | — | YEAR_UNRESOLVED | filing_year_omitted | false |
| 10K-V2-XC-002 | LE, TGT | — | — | YEAR_UNRESOLVED | filing_year_omitted | false |
| 10K-V2-XC-003 | RCKY | — | — | YEAR_UNRESOLVED | filing_year_omitted | false |
| 10K-V2-XC-004 | ATER | — | risk | YEAR_UNRESOLVED | filing_year_omitted | false |
| 10K-V2-TC-001 | ORBS | 2024, 2025 | revenue | — | content_year_filing_year_mismatch | false |
| 10K-V2-TC-002 | TBHC, VFC | 2024 | revenue | — | matches_declared_filing_year | false |
| 10K-V2-TC-003 | EBAY | 2024, 2026 | risk | — | matches_declared_filing_year | true |
| 10K-V2-TC-004 | WOOF | 2024, 2026 | — | SECTION_UNRESOLVED | matches_declared_filing_year | false |

## Decision

The untuned question-only detector is not sufficient as a general frozen-24 router. Routed decomposition evaluation must therefore remain separate and use explicitly declared approved non-gold source metadata. Any later detector-rule changes are in-sample tuning and must be evaluated against this preserved baseline.
