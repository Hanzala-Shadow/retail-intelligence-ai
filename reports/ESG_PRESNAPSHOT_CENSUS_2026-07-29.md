# ESG pre-snapshot census — Phase 0

**Date:** 2026-07-29  
**Repo:** `C:\Users\Aziz\Documents\ChatGPT Codex\retail-intelligence-ESG-works`  
**Gate:** Phase 0 complete. Stop before Phase 1.

## Limit first

The direct `c805af1` commit diff and current Git status were **not opened**. The
task instructions said not to run Git operations. This means the brief's claim
of 7 modified and 1 deleted tracked files is not fully re-verified here. The
seven modified file names can be tied to the pre-merge backup. The deleted file
name remains unverified.

The `c805af1` estimate below uses the current changed code paths, their tests,
the launch brief's diff summary, and the live 2,537 page texts. It is an
estimate, not the later reparse measurement.

## Gate result

Snapshot-first is still the safe next step.

- Source PDF, parsed-text, section-text, and cross-index hashes passed.
- All 3,329 indexed chunk files exist and decode as strict UTF-8.
- Raw chunk bytes equal their UTF-8 re-encoding for 3,329/3,329 files.
- No chunk is empty or whitespace-only.
- Chunk IDs are unique: 3,329/3,329.
- Two text hashes are duplicated across four chunk rows. Aziz must decide
  whether the source-year differences are useful enough to keep both.
- The 50 live stems have no multi-year filename. The Drive manifest has 24.
- The old first-token rule differs from canonical `max(years)` on 22 of the 24,
  not all 24 as the brief says. The two descending GES names already start with
  the maximum year.
- Phase 1 was not started.

## 0.1 Internal consistency

### Measured corpus shape

| Item | Measured value |
|---|---:|
| Raw PDFs under `data/01_raw/sustainability/` | 726 |
| Drive manifest rows | 763 |
| Drive rows with a same-name PDF in this repo | 726 |
| Parse-index rows / unique PDF files | 53 / 53 |
| Live parsed documents used by chunks | 50 |
| Extra parsed documents not used by chunks | 3 |
| Parsed `.txt` files | 53 |
| Section-index rows / section `.txt` files | 1,980 / 1,980 |
| Chunk-index rows / chunk `.txt` files | 3,329 / 3,329 |
| Unique chunk IDs | 3,329 |
| Vector-manifest rows | 3,329 |
| Layout-QA page rows | 2,537 |

The three extra parsed documents are `AAPL-APPLE INC-2014`,
`AN-AUTONATION INC-2022`, and `ARKO-ARKO CORP-2023`. They have no live chunks.

### Hash and metadata checks

| Check | Pass | Fail |
|---|---:|---:|
| Parse `source_size_bytes` vs source PDF | 53 | 0 |
| Parse `source_mtime_utc` vs source PDF | 53 | 0 |
| Parse `source_sha256` vs source PDF | 53 | 0 |
| Parse-source size, time, and hash vs selected PDF | 53 | 0 |
| Parse `content_hash` vs parsed `.txt` raw bytes | 53 | 0 |
| Parse `char_count` vs parsed text | 53 | 0 |
| Page-map file present | 53 | 0 |
| Chunk path in index vs disk, both directions | 3,329 | 0 |
| Chunk strict UTF-8 decode | 3,329 | 0 |
| Chunk raw bytes vs UTF-8 re-encode | 3,329 | 0 |
| Chunk `char_count` vs file | 3,329 | 0 |
| Chunk `token_count` vs `cl100k_base` recount | 3,329 | 0 |
| Chunk section hash vs section file | 3,329 | 0 |
| Chunk parsed hash vs parsed file | 3,329 | 0 |
| Section-index parsed-source hash vs parsed file | 1,980 | 0 |
| Layout-QA parsed hash vs parsed file | 2,537 | 0 |

The parse index calls the parsed-text digest `content_hash`. The chunks and
layout files call the same digest `parsed_text_sha256`. The bytes agree.

Hashing follows the raw-byte rule already used by the chunker at
`src/esg_chunker.py:641-647`. UTF-8 output is written with `newline=""` at
`src/esg_chunker.py:652-659`.

### Version and state checks

- `src/esg_layout_qa.py:47` has `AUDIT_VERSION = "layout_v7"`.
- `scripts/build_esg_vector_manifest.py:61` has
  `LAYOUT_AUDIT_VERSION = "layout_v7"`.
- All 2,537 layout rows carry `layout_v7`.
- All 3,329 chunk rows carry `citation_validation_status=verified_exact` and
  `citation_validation_version=semantic_v1`.
- Retrieval state is 2,612 eligible, 715 held for VLM, and 2 held for document
  review.
- RAG action is 3,327 `index_as_esg` and 2 `exclude_from_esg_index`.

### Pre-existing reference-tree state

The following seven current files are byte-identical to their copies in
`backups/pre_merge_3dbe613_20260729/`. The brief says these are the seven
modified tracked files from the 2026-07-25 run:

1. `data/00_reference/esg_chunks_index.csv`
2. `data/00_reference/esg_drive_manifest.csv`
3. `data/00_reference/esg_page_layout_qa.csv`
4. `data/00_reference/esg_parse_index.csv`
5. `data/00_reference/esg_pipeline_qa.csv`
6. `data/00_reference/esg_sections_index.csv`
7. `data/00_reference/vector_index_manifest.csv`

The brief also says one tracked file is deleted. Its path is unverified because
Git status was not run.

`data/00_reference/esg_sample_docs.csv` is present, has 50 rows, and starts
with a UTF-8 BOM. The brief says it is untracked. Only 6 stems overlap the 50
live stems; 44 are sample-only and 44 are live-only. This makes it unsafe as the
scope file for a live reparse.

All eight current CSVs in the pre-merge backup set, including
`esg_sample_docs.csv`, match their backup SHA-256 values. This confirms they did
not change after that backup. It does not replace Git status.

### New consistency finding: stale section source times

`esg_sections_index.csv` has 103 rows whose `source_mtime_utc` no longer matches
the parsed file on disk, while file size and SHA-256 still match:

| Parsed document | Affected section rows |
|---|---:|
| `ABG-ASBURY AUTOMOTIVE GROUP INC-2023` | 55 |
| `AZO-AUTOZONE INC-2022` | 48 |

This is metadata drift, not text drift. The section resume check uses time and
hash together at `src/section_splitter_esg.py:1104-1105`, so these two documents
may be treated as stale even though their bytes match. Report only; do not fix.

## 0.2 Multi-year filename census

The old rule takes the first year token at
`scripts/build_esg_vector_manifest.py:77-79`. The canonical rule returns the
maximum distinct in-range year at `src/esg_year.py:48-65`.

The 50 live stems contain 0 multi-year names. Risk is latent until scope grows.

| # | Drive filename | Old first token | Canonical max | Difference |
|---:|---|---:|---:|---:|
| 1 | `AAP-ADVANCE AUTO PARTS INC-2017-2018.pdf` | 2017 | 2018 | +1 |
| 2 | `ACI-ALBERTSONS COS INC-2021-2022.pdf` | 2021 | 2022 | +1 |
| 3 | `COST-COSTCO WHOLESALE CORP-2020-2021.pdf` | 2020 | 2021 | +1 |
| 4 | `COST-COSTCO WHOLESALE CORP-2021-2022.pdf` | 2021 | 2022 | +1 |
| 5 | `COST-COSTCO WHOLESALE CORP-2022-2023.pdf` | 2022 | 2023 | +1 |
| 6 | `COST-COSTCO WHOLESALE CORP-2023-2024.pdf` | 2023 | 2024 | +1 |
| 7 | `COST-COSTCO WHOLESALE CORP-2024-2025.pdf` | 2024 | 2025 | +1 |
| 8 | `GAP-GAP INC-2015-2016.pdf` | 2015 | 2016 | +1 |
| 9 | `GES-GUESS INC-2016-2017.pdf` | 2016 | 2017 | +1 |
| 10 | `GES-GUESS INC-2018-2019.pdf` | 2018 | 2019 | +1 |
| 11 | `GES-GUESS INC-2020-2021.pdf` | 2020 | 2021 | +1 |
| 12 | `GES-GUESS-2021-2020.pdf` | 2021 | 2021 | 0 |
| 13 | `GES-GUESS-2023-2022.pdf` | 2023 | 2023 | 0 |
| 14 | `KTB-KONTOOR BRANDS INC-2021-2022.pdf` | 2021 | 2022 | +1 |
| 15 | `NKE-NIKE INC -CL B-2014-2015.pdf` | 2014 | 2015 | +1 |
| 16 | `NKE-NIKE INC -CL B-2016-2017.pdf` | 2016 | 2017 | +1 |
| 17 | `URBN-URBAN OUTFITTERS INC-2024-2025.pdf` | 2024 | 2025 | +1 |
| 18 | `URBN-Urban Outfitters-2021-2022.pdf` | 2021 | 2022 | +1 |
| 19 | `URBN-Urban Outfitters-2023-2024.pdf` | 2023 | 2024 | +1 |
| 20 | `VFC-VF CORP-2021-2022.pdf` | 2021 | 2022 | +1 |
| 21 | `VFC-VF CORP-2022-2023.pdf` | 2022 | 2023 | +1 |
| 22 | `VFC-VF CORP-2023-2024.pdf` | 2023 | 2024 | +1 |
| 23 | `VFC-VF CORP-2024-2025.pdf` | 2024 | 2025 | +1 |
| 24 | `WMK-WEIS MARKETS INC-2024-2025.pdf` | 2024 | 2025 | +1 |

Measured summary: 24 multi-year names, 22 changed values, 2 unchanged values.
Every row is outside the live 50-document corpus.

## 0.3 `c805af1` blast-radius estimate

### Code path

The current remediation stage:

- tries `pdfium_text` before OCR at
  `src/pipeline_ocr_remediation_stage.py:40-41` and `:343-344`;
- scores recognizable tokens at `:70-99` and `:121-128`;
- accepts a candidate only when it is clean and improves the score at
  `:240-245`;
- is scoped by ticker/stem/file in its `run` entry at `:308`;
- is scope-only in `scripts/run_esg_pipeline_fast.ps1:152-154`;
- is skipped by an unscoped `-Stage all` run at
  `scripts/run_esg_pipeline_fast.ps1:247`.

`scripts/run_esg_sample_reparse.ps1:46-58` runs the parser only. It does not run
page remediation itself. The `pdf_parser.py` change exposes shared text
normalization at `src/pdf_parser.py:1483`; the current parser calls that helper
from its existing extraction paths.

### Measured estimate against current page text

Using the old letter-share test described by the brief and the current
recognizable-token test over all 2,537 live page-map records:

| Estimate | Value |
|---|---:|
| Pages flagged by the current full detector | 44 |
| Documents with at least one flagged page | 16 |
| Empty pages | 10 |
| Garbled/low-readable pages | 32 |
| CID-artifact pages | 1 |
| Replacement-character pages | 1 |
| Flagged pages that overlap live chunks | 34 |
| Unique live chunks overlapping those pages | 46 |
| Pages freed by letter-share → recognizable-token change | 0 |
| Pages newly flagged by that readability change | 1 |

The surprising result is that the readability change is inert for the stated
digit-heavy false-positive problem in this live sample. No page moves from
old-flagged to clean. Long unbroken tokens and other signals still flag the
candidate pages. One spaced one-letter OCR-speckle page becomes newly flagged.

The recovery-order change is not inert if scoped remediation is run. Up to 44
pages across 16 documents would try the embedded PDFium text layer before OCR.
Ten empty pages do not overlap a current chunk. The other candidate pages touch
46 current chunks. `PSMT-PRICESMART INC-2021` accounts for 27 of the 44 pages.

**Estimate:** a parser-only sample reparse is likely to get no output change
from `c805af1`. A later, correctly scoped remediation pass has an upper bound of
44 candidate pages and 46 current chunks. The actual changed-page and changed-
chunk counts are unverified until the allowed reparse/remediation diff. No PDF
extraction or OCR was run in this phase.

## Findings ranked by severity

### High

1. **The baseline still does not exist.** `chunk_text_sha256`, `dataset_id`, and
   `SHA256SUMS` remain absent, as expected before Phase 1.
2. **The sample scope file does not describe the live corpus.** It is BOM'd,
   has 50 rows, and overlaps only 6 of the 50 live stems. Its untracked status is
   from the brief and was not re-verified with Git.
3. **Year rules differ on 22 latent files.** The next scope increase can assign
   the wrong report year unless all consumers use `src/esg_year.py`.

### Medium

4. **Two duplicate chunk-text hash groups exist.** Four rows contain only two
   unique texts. Both pairs are CRMT 2023 vs CRMT 2024 and are eligible for ESG
   indexing:
   - `11c4addb…df57`: `ethics_compliance__0003`, pages 29 vs 30, 2,796 chars.
   - `94a2c874…d22c`: `human_capital__0006`, pages 30 vs 31, 520 chars.

   The different source years are useful metadata, but the text is byte-for-byte
   identical. Exclusion is Aziz's decision.
5. **103 section rows have stale source times** across ABG 2023 and AZO 2022.
   Their size and hash still match.
6. **Token counts have no tokenizer field.** The 3,329 values exactly match
   `cl100k_base`, configured at `src/esg_chunker.py:19`, but this is not a BGE
   tokenizer. BGE truncation remains a separate live risk.
7. **Chunker and sectioner versions are not recorded.** No explicit version
   field or constant was found in `src/` or `scripts/`.

### Low / context

8. `LAYOUT_AUDIT_VERSION` and `AUDIT_VERSION` agree on `layout_v7`; no drift.
9. The other clone's stated 3,764 chunks exceed this corpus by 435. This was
   not checked because the other clone is out of scope. No cause was chased.

## Acceptance status at this gate

| Phase 1 acceptance item | Current Phase 0 state |
|---|---|
| 3,329/3,329 chunks carry `chunk_text_sha256` | Not started; field absent |
| Zero empty chunk hashes | Not started; source files have 0 empty texts |
| `sha256sum -c SHA256SUMS` | Not run; file absent |
| Full suite ≥187 passed / 0 failed | Not run in Phase 0 |
| No changes under `data/04_chunks/` | No writes were made there; Git status not run |
| Sidecar states data/code mismatch | Not started; Phase 1 only |

## What changed in this phase

Created only this report:

- `reports/ESG_PRESNAPSHOT_CENSUS_2026-07-29.md`

No file under `data/` or `src/` was changed.

## What was not done

- No parser, sectioner, chunker, OCR, remediation, or rebuild command ran.
- No file under `data/04_chunks/` was changed.
- No P1 enrichment ran.
- No Git command ran.
- No test suite ran. Phase 0 is a read census, and test tools may write caches.
- No file in `retail-intelligence-ai` was changed or used as corpus evidence.
- Phase 1 did not start. Aziz must choose snapshot-first and the `dataset_id`.

## Gate question for Aziz

Approve Phase 1 snapshot-first, and choose the proposed dataset ID
`esg-snapshot-d5ff461-2026-07-29`, or provide a different ID.
