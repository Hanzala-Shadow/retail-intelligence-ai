# ESG Pipeline Handoff Documentation

Prepared for: team handoff  
Owner: Aziz  
Last updated: 2026-07-14  
Status: documentation complete for the current local ESG pipeline state

## Purpose

This document explains how the ESG document pipeline currently works, what each stage produces, how section splitting and chunking are performed, what QA gates are used, and which company-level exceptions were observed during the latest audit.

The pipeline converts sustainability/ESG PDFs into:

1. Parsed report text.
2. Page maps for citation support.
3. Canonical ESG sections.
4. Retrieval-sized chunks.
5. QA reports.
6. Database-load-ready records.

The current priority is data quality. The pipeline may produce files successfully, but a report should only be treated as RAG-ready after QA confirms parse quality, section/chunk quality, and citation metadata.

## Current Pipeline Snapshot

Latest current audit source:

- `reports/esg_current_quality_audit_2026-07-14/ESG_CURRENT_QUALITY_AUDIT.md`
- `data/00_reference/esg_pipeline_qa.csv`

Current metrics:

| Metric | Current value |
|---|---:|
| Tracker rows in QA | 524 |
| Parsed documents | 473 |
| Sections | 6,024 |
| Chunks | 32,890 |
| QA complete rows | 431 |
| QA needs-review rows | 20 |
| QA missing-PDF rows | 1 |
| QA not-found rows | 72 |
| Duplicate chunk IDs | 0 |
| Invalid token-count chunks | 0 |
| Citation-not-ready chunks | 33 |
| Documents with CID text signals | 9 |

Current high-level conclusion:

- The pipeline is structurally working.
- The DB dry-run layer has been clean in recent validation.
- The remaining work is targeted QA: citation metadata gaps, low text quality, under-sectioned documents, one missing PDF reference, and not-found/partial coverage explanation.

## Main Source Inputs

| Input | Purpose |
|---|---|
| `data/00_reference/companies.csv` | Company master list. |
| `data/00_reference/sustainability_report_tracker.csv` | ESG tracker with company/year/source status and Drive/report links. |
| `data/01_raw/sustainability/{ticker}/*.pdf` | Local raw ESG/sustainability PDFs downloaded from Drive. |
| Optional OCR staged PDFs | Used only when raw PDFs are scanned or have corrupted text extraction. |

Important rule:

- The tracker/source universe must reconcile back to the starting company list.
- If a company/report is removed, missing, duplicated, or not found, the company and reason must be documented.

## Main Outputs

| Output | Purpose |
|---|---|
| `data/02_interim/esg_text/{ticker}/{pdf_stem}.txt` | Parsed text for one ESG PDF. |
| `data/02_interim/esg_text/{ticker}/{pdf_stem}.pages.csv` | Page-to-character map for citations. |
| `data/03_sections/esg/{ticker}/{pdf_stem}__{section_instance_id}.txt` | One contiguous ESG section occurrence. |
| `data/04_chunks/esg/{ticker}/{pdf_stem}__{section_instance_id}__chunk_0000.txt` | RAG-sized chunk text. |
| `data/00_reference/esg_parse_index.csv` | Parse status, provenance, page counts, text-quality flags. |
| `data/00_reference/esg_sections_index.csv` | Section metadata and source spans. |
| `data/00_reference/esg_chunks_index.csv` | Chunk metadata, token counts, page spans, citation readiness. |
| `data/00_reference/esg_source_registry.csv` | Stable source identity, type, canonical owner, scope, and retrieval policy overrides. |
| `data/00_reference/esg_pipeline_qa.csv` | Report-level QA status and operational action. |

## Standard Execution Order

Run from the repo root:

```bash
python src/drive_downloader.py --resume
python src/pdf_parser.py --resume --root data/01_raw/sustainability --out data/02_interim/esg_text --index data/00_reference/esg_parse_index.csv --workers 1
python src/section_splitter_esg.py --resume --input data/02_interim/esg_text --out data/03_sections/esg --index data/00_reference/esg_sections_index.csv
python src/esg_chunker.py --input data/03_sections/esg --out data/04_chunks/esg --index data/00_reference/esg_chunks_index.csv
python src/esg_pipeline_qa.py --out data/00_reference/esg_pipeline_qa.csv
python src/drive_to_db.py --dry-run
```

Only after dry-run and QA are acceptable:

```bash
python src/drive_to_db.py --commit
```

Use `--force` only when intentionally rebuilding a selected scope after code/data changes:

```bash
python src/pdf_parser.py --ticker TDUP --force
python src/section_splitter_esg.py --ticker TDUP --force
python src/esg_chunker.py --ticker TDUP --force
```

## Resume And Checkpoint Behavior

The pipeline is designed to survive SSH/EC2/network interruptions.

Resume behavior:

- Resume is the default for ESG stages.
- Each stage checks whether outputs exist and whether index rows match the current source fingerprint.
- Source fingerprints include size, UTC modification time, and SHA-256 where applicable.
- Changed raw PDFs or changed OCR replacements automatically reprocess.
- Output files are written through `.tmp` files and then atomically replaced.
- Indexes are checkpointed after each completed item by default.

Important safety behavior:

- ESG scripts do not overwrite 10-K section/chunk paths.
- Chunker stale cleanup is section-scoped. If a re-split document no longer produces a previous section code, only that missing section's chunks/index rows are removed.

## PDF Parsing Approach

Parser: `src/pdf_parser.py`

Library:

- Uses `pdfplumber`.

Core logic:

1. Open each PDF under `data/01_raw/sustainability/{ticker}/`.
2. Extract text page by page with `page.extract_text_simple()`.
3. Keep a page in the parsed output only if it has more than `MIN_PAGE_CHARS = 20` stripped characters.
4. Build one full text file.
5. Build a page map with `page`, `char_start`, `char_end`, and `char_count`.
6. Count tables when `pdfplumber` can identify them.
7. Write one parse-index row per PDF.

Parse status logic:

| Status | Meaning |
|---|---|
| `parsed` | Extracted text has at least `OCR_MIN_NONSPACE_CHARS = 500` non-whitespace characters. |
| `ocr_required` | Extracted text is below 500 non-whitespace characters. Usually scanned/image-heavy. |
| `failed` | Parser could not process the PDF. |

Text-quality fields:

- `quality_flags`
- `possible_wrong_doc_type`
- `readable_word_count`
- `readable_word_ratio`
- `chars_per_page`
- `garbled_char_count`

Quality flags:

| Flag | Trigger |
|---|---|
| `possible_10k` | SEC 10-K markers appear in a sustainability-folder PDF. |
| `garbled_text` | Common bad-encoding/OCR artifact sequences exceed threshold. |
| `low_readable_word_ratio` | Text is long enough but too few tokens look like normal readable words. Threshold: readable ratio below 0.45. |
| `low_text_per_page` | At least 5 pages and fewer than 250 extracted characters per page. |

Important interpretation:

- `ocr_required` is a parser status, not an OCR attempt.
- Quality flags are warnings. They do not automatically delete or exclude a document.
- RAG eligibility should use QA status and `rag_action`, not only parse success.

## OCR Approach

OCR helper: `src/ocr_pdf.py`

Libraries:

- `pypdfium2` to render PDF pages.
- `pytesseract` for OCR.
- `pypdf` to write searchable PDFs.
- `Pillow` for preprocessing.

Default OCR settings:

| Parameter | Value |
|---|---:|
| Render scale | `3.2` |
| Tesseract config | `--oem 3 --psm 3` |
| Minimum OCR word confidence | `45` |
| PDF text mode | `ordered` |
| PDF base | `original` |

OCR text cleanup behavior:

- Renders pages.
- Converts page image to grayscale.
- Applies autocontrast and sharpening.
- Uses Tesseract word-level confidence output.
- Drops low-confidence tokens below the confidence threshold.
- Reconstructs lines in reading order.
- Applies conservative cleanup for common OCR artifacts.
- Writes a sidecar `.txt` and `.pages.csv`.
- Optionally writes a searchable PDF.

When to use `--pdf-base image`:

- Use it when the source PDF has a corrupted embedded text layer, such as repeated `(cid:...)` artifacts.
- It first renders the report to an image-only PDF, then adds the OCR text layer.

Recommended OCR command pattern:

```bash
python src/ocr_pdf.py --input "data/01_raw/sustainability/TICKER/report.pdf" --output-text "data/02_interim/ocr_text/TICKER/report.txt" --output-pdf "data/02_interim/ocr_staging/TICKER/report.pdf" --pdf-text-mode ordered
```

For corrupted embedded text:

```bash
python src/ocr_pdf.py --input "data/01_raw/sustainability/TICKER/report.pdf" --output-text "data/02_interim/ocr_text/TICKER/report.txt" --output-pdf "data/02_interim/ocr_staging/TICKER/report.pdf" --pdf-text-mode ordered --pdf-base image
```

Shared-run rule:

- After OCR, upload/replace the searchable PDF in Drive using the same filename as the original report.
- Then rerun downloader/parser for that ticker so the raw PDF path remains the canonical source identity.

## Section Splitting Logic

Section splitter: `src/section_splitter_esg.py`

Goal:

- Convert variable ESG report structures into a stable set of canonical ESG section codes.

Canonical section codes:

- `ceo_letter`
- `about_this_report`
- `environmental`
- `climate`
- `energy`
- `emissions`
- `waste`
- `water`
- `social`
- `human_capital`
- `diversity_equity_inclusion`
- `supply_chain_ethics`
- `community`
- `governance`
- `ethics_compliance`
- `data_summary`
- `appendix`
- `other`
- `full_document`

Minimum section size:

- `MIN_SECTION_CHARS = 300`

High-level algorithm:

1. Read parsed report text.
2. Scan line by line for heading candidates.
3. Normalize candidate headings by removing page-number leaders, numbering prefixes, repeated spacing, and trailing punctuation.
4. Reject lines that look like body sentences rather than headings.
5. Match accepted headings against ESG regex patterns.
6. Map each matched heading to a canonical section code.
7. Split text at accepted headings.
8. Merge duplicate section codes into a single section.
9. Merge or carry very short sections so headings are not separated from their body text.
10. Fall back to `full_document` if reliable headings are not found.

Heading candidate filters:

- Rejects very long lines above 180 characters.
- Rejects lines with more than 10 word-like tokens.
- Rejects lines ending in open-ended words such as `and`, `of`, `to`, `with`, etc.
- Rejects many obvious body-sentence patterns.
- Rejects lines beginning like body prose, such as `we`, `this`, `these`, `in 2024`, etc.
- Rejects sentence-like lines ending with a period when they are long enough to look like prose.
- Rejects digit-heavy lines.
- Rejects comma-heavy lines.

False-positive controls:

- Broad terms such as `about`, `materials`, `people`, and `index` are filtered with extra rules.
- Example: `material weakness` should not become an environmental/materials heading.
- Example: `people and wildlife` should not become a human-capital heading.
- Example: a generic `index` heading is only accepted for appendix if it is tied to GRI/SASB/TCFD/SDG/ESG/reporting context.

Section confidence:

| Confidence | Typical meaning |
|---|---|
| `high` | Matched recognized heading and section body is large enough. |
| `medium` | Matched heading but section is shorter or less certain. |
| `low` | Fallback/full-document or weak split evidence. |

Split methods:

| Method | Meaning |
|---|---|
| `heading_regex` | Split was made from recognized ESG heading patterns. |
| `full_document_fallback` | No reliable headings; whole document or large remainder used as one section. |

Section index metadata:

- `ticker`
- `pdf_stem`
- `section_instance_id`
- `section_code`
- `section_title`
- `section_file`
- `source_start_char`
- `source_end_char`
- `provenance_version`
- `page_start`
- `page_end`
- `char_count`
- `word_count`
- `split_method`
- `confidence`
- source fingerprint fields

## Chunking Logic

Chunker: `src/esg_chunker.py`

Tokenizer:

- `tiktoken`
- Encoding: `cl100k_base`

Chunking parameters:

| Parameter | Value |
|---|---:|
| `CHUNK_SIZE` | 500 tokens |
| `OVERLAP` | 50 tokens |
| `MIN_CHUNK_TOKENS` | 100 tokens |
| `MAX_CHUNK_TOKENS` | 600 tokens |
| `DOC_TYPE` | `sustainability` |

Chunking behavior:

1. Read each section file.
2. Tokenize with `cl100k_base`.
3. If the whole section is below 100 tokens, skip it as a short section.
4. If the section is between 100 and 600 tokens, keep it as one chunk.
5. If larger than 600 tokens, split into 500-token windows with 50-token overlap.
6. Avoid creating a final tiny remainder when possible by merging within the 600-token maximum.
7. Validate every created chunk is between 100 and 600 tokens.
8. Write chunk files and update `esg_chunks_index.csv`.

Chunk ID format (source type is metadata and may change without changing the
logical chunk identity):

```text
{source_id}__{section_instance_id}__chunk_{0000}
```

Chunk metadata:

- `chunk_id`
- `source_id`
- `source_version_id`
- `ticker`
- `doc_type`
- `doc_quality_status`
- `rag_action`
- `quality_flags`
- `pdf_stem`
- `section_instance_id`
- `section_code`
- `chunk_index`
- `token_count`
- `char_count`
- `chunk_file`
- `source_section_file`
- source fingerprint fields
- `source_start_char`
- `source_end_char`
- `page_start`
- `page_end`
- `citation_ready`
- `citation_validation_status`
- `citation_validation_version`

Citation rule:

- Chunks are citation-ready only when the complete text matches the declared
  parsed-text slice, the slice is contained by its exact section instance, the
  parsed-text fingerprint matches, and the page map resolves.
- Require `citation_validation_version=semantic_v1` and status
  `verified_exact` or `verified_whitespace_normalized`.
- Prefix-only matches, invalid bounds, fingerprint mismatches, and missing page
  maps are not citation-ready.

## QA Logic

QA script: `src/esg_pipeline_qa.py`

QA reconciles:

- Tracker rows.
- Company master list.
- Local PDFs.
- Parse index.
- Section index.
- Chunk index.
- Token bounds.
- Citation metadata.
- Document quality flags.
- RAG action.

QA statuses:

| Status | Meaning |
|---|---|
| `complete` | Parsed, sectioned, chunked, valid token sizes. |
| `needs_review` | Data exists, but some quality/citation issue requires human review. |
| `missing_pdf` | Tracker expects a PDF but local raw PDF is missing. |
| `not_found` | Tracker marks no report found. |
| `ocr_required` | Parser found too little usable text. |
| `parse_failed` | Parser failed. |
| `tracker_needs_cleanup` | Tracker link/status needs cleanup. |
| `incomplete` | Some expected downstream stage is missing. |

QA priority checks:

1. Possible wrong document type.
2. Downloaded tracker rows with no local PDF.
3. Parsed PDFs with zero sections.
4. Sectioned PDFs with zero chunks.
5. Invalid chunk token counts.
6. Chunks missing citation metadata.
7. Tracker cleanup issues.

RAG action values:

| `rag_action` | Meaning |
|---|---|
| `index_as_esg` | Eligible unless later audit excludes it. |
| `manual_review_before_indexing` | Do not index until reviewed/fixed. |
| `exclude_from_esg_index` | Should not enter ESG vector index. |
| `not_applicable` | Usually not-found/no report cases. |

Recommended RAG gate:

- Index only rows with `status=complete`, `doc_quality_status=ok`,
  `rag_action=index_as_esg`, `citation_ready=true`,
  `citation_validation_version=semantic_v1`, and a verified validation status.
- Keep non-eligible rows in the database for lineage, but exclude them from vector search until fixed.

## Current Company-Level Exceptions

The counts below came from the pre-`semantic_v1` audit. Keep them as historical
evidence, but do not publish them as current citation-quality results until the
comprehensive audit is rerun after the provenance migration.

### Governed Source Decisions

| Source | Decision | Reason |
|---|---|---|
| ETSY 2024 | Keep as `annual_report_with_esg`, excerpt, supplementary. | Valid ESG disclosure, but not a standalone sustainability report. |
| VSXY 2026 | Retain as `program_impact_report`; exclude from the company-wide ESG index. | It covers the Global Fund for Women’s Cancers rather than company-wide ESG. |
| SHOO 2021 | Hold from indexing until page-image OCR. | Pages are readable visually, but the embedded font extracts as cipher-like text. |
| APC/ARKO 2021–2024 | ARKO is canonical; retain APC as excluded intake/alias history. | Each APC file is byte-identical to the ARKO report, which identifies itself as ARKO Corp. |

These decisions are encoded in `data/00_reference/esg_source_registry.csv`.

### Missing PDF Reference

| Ticker | Year | Issue | Action |
|---|---:|---|---|
| ETSY | 2021 | QA has `missing_pdf`; tracker references a report/year not present in the current local Drive PDF corpus. | Reconcile tracker vs Drive. Either add the PDF, correct the tracker row, or mark not found with a reason. |

### Low Text Quality / Manual Review Before Indexing

| Ticker | PDF | Issue | Evidence | Suggested action |
|---|---|---|---|---|
| AMZN | `AMZN-Amazon-2022.pdf` | Low text per page | 8 sections, 12 chunks, QA note `low text per page` | Spot-check extracted text; consider OCR if the PDF is image-heavy or extraction is incomplete. |
| NGVC | `NGVC-NATURAL GROCERS VITAMIN CTGE-2021.pdf` | Low text per page | 2 sections, 3 chunks | Spot-check report; confirm whether report is short or parse missed text. |
| NGVC | `NGVC-NATURAL GROCERS VITAMIN CTGE-2022.pdf` | Low text per page | 6 sections, 6 chunks | Spot-check report; confirm whether report is short or parse missed text. |
| SHOO | `SHOO-MADDEN STEVEN LTD-2021.pdf` | Cipher-like embedded-text extraction | readable ratio 0.0816; 1 section; 127 chunks | Run page-image OCR and keep it out of the index until the repaired text passes quality checks. |

### Large Under-Sectioned Documents

| Ticker | PDF | Issue | Evidence | Suggested action |
|---|---|---|---|---|
| SHOO | `SHOO-MADDEN STEVEN LTD-2021.pdf` | Large full-document fallback caused by bad extracted text | 79,395 chars; 1 section; 127 chunks | OCR first; section tuning on cipher-like text would not solve the source problem. |

### Chunks Missing Citation Metadata

These rows have chunks without page/span metadata. They may still have text, but they are risky for citation-critical RAG.

| Ticker | PDF | Not-ready chunks | Action |
|---|---|---:|---|
| AAPL | `AAPL-APPLE INC-2025.pdf` | 1 | Regenerate page/span mapping or exclude affected chunks. |
| COST | `COST-COSTCO WHOLESALE CORP-2021.pdf` | 2 | Regenerate page/span mapping or exclude affected chunks. |
| COST | `COST-COSTCO WHOLESALE CORP-2022.pdf` | 4 | Regenerate page/span mapping or exclude affected chunks. |
| DECK | `DECK-DECKERS OUTDOOR CORP-2021.pdf` | 9 | Regenerate page/span mapping or exclude affected chunks. |
| DECK | `DECK-DECKERS OUTDOOR CORP-2022.pdf` | 5 | Regenerate page/span mapping or exclude affected chunks. |
| DECK | `DECK-DECKERS OUTDOOR CORP-2024.pdf` | 1 | Regenerate page/span mapping or exclude affected chunks. |
| DLTR | `DLTR-DOLLAR TREE INC-2022.pdf` | 1 | Regenerate page/span mapping or exclude affected chunks. |
| ETSY | `ETSY-Etsy-2023.pdf` | 1 | Regenerate page/span mapping or exclude affected chunks. |
| GROV | `GROV-GROVE COLLABORATIVE HLDG INC-2022.pdf` | 1 | Regenerate page/span mapping or exclude affected chunks. |
| LAD | `LAD-LITHIA MOTORS INC -CL A-2021.pdf` | 2 | Regenerate page/span mapping or exclude affected chunks. |
| MOV | `MOV-MOVADO GROUP INC-2021.pdf` | 1 | Regenerate page/span mapping or exclude affected chunks. |
| MOV | `MOV-MOVADO GROUP INC-2023.pdf` | 1 | Regenerate page/span mapping or exclude affected chunks. |
| ORLY | `ORLY-O'REILLY AUTOMOTIVE INC-2024.pdf` | 1 | Regenerate page/span mapping or exclude affected chunks. |
| SHOO | `SHOO-MADDEN STEVEN LTD-2024.pdf` | 2 | Regenerate page/span mapping or exclude affected chunks. |
| VSXY | `VSXY-Victorias Secret-2023.pdf` | 1 | Regenerate page/span mapping or exclude affected chunks. |

### Medium-Priority Pattern Exceptions

These are not necessarily blockers, but they should be sampled before final embedding.

| Pattern | Companies/examples | Why it matters |
|---|---|---|
| Low section density | AAPL, AMZN, DECK, DELL and others | Large reports may have too much text concentrated into too few sections, indicating missed headings. |
| Borderline readable word ratio | AEO, BRLT, BURL, CAL, COLM and others | Extracted text may be usable but should be spot-checked. |
| Minor text artifacts/CID samples | BBY, COST, CRI and others | Low-level artifacts may not block indexing, but should be sampled. |
| Full-document fallback | Smaller number of rows including SHOO | Fallback means heading detection did not confidently split the report. |
| Not-found rows | 72 QA rows | Need systematic explanation: no report published, partial filings, website-only ESG info, or tracker cleanup. |

## Website ESG Information

Some companies do not have formal ESG/sustainability PDFs but have ESG information on investor or corporate websites. The tracker already contains examples in `sustainability_report_tracker.csv`, including:

- DDS: notes mention ESG-like information from an investor static file.
- RVLV: notes mention the Revolve social-impact website.
- RH: notes mention an ESG page on the website.

Recommended handling:

- Treat website ESG pages as supplemental sources, not as normal PDF sustainability reports.
- Store them with a separate source type such as `web_esg_page`.
- Record URL, accessed date, title, ticker, company, extraction method, content hash, and status.
- Do not silently mix copied website text into PDF-derived report chunks without source metadata.
- Use website ESG only after the main PDF audit is stable or when explaining not-found/partial-coverage cases.

## PDF Image Logging

Meeting guidance:

- Images should not be analyzed immediately.
- But when parsing PDFs, images/figures/diagrams should ideally be saved rather than discarded.
- Images may contain numbers, charts, diagrams, or sustainability messaging.
- Later, image presence/type may become an analysis extension.

Current repo status:

- The repo has `pdfplumber`, `pypdfium2`, `Pillow`, and OCR dependencies.
- The current parser reads text and clears image caches for memory safety.
- It does not yet write a dedicated image extraction output.

Feasible extension:

- Add an image extraction script or parser option later.
- Suggested folder:

```text
data/02_interim/esg_images/{ticker}/{pdf_stem}/page_005_img_001.png
```

- Suggested index:

```text
data/00_reference/esg_image_index.csv
```

Suggested image index fields:

- `ticker`
- `pdf_file`
- `pdf_stem`
- `page`
- `image_index`
- `image_file`
- `width`
- `height`
- `format`
- `source_bbox`
- `sha256`
- `extracted_at`

Recommended priority:

1. Finish ESG data-quality audit.
2. Fix current high-priority exceptions.
3. Only then add image logging if time remains.

## Practical Definition Of Done

The ESG pipeline should be considered ready for team handoff when:

1. `src/esg_pipeline_qa.py` has been rerun.
2. DB dry-run reports zero anomalies.
3. `parsed_count > 0` rows have nonzero sections.
4. Sectioned rows have nonzero chunks except intentionally short sections.
5. Invalid token-count chunks are zero.
6. Duplicate chunk IDs are zero.
7. Citation-not-ready chunks are either fixed or excluded from citation-critical RAG.
8. `needs_review` rows are listed with explicit reasons.
9. `not_found` rows have systematic explanations.
10. Any removed/missing/duplicate company is documented with company name, ticker, year, and reason.

## Team Communication Summary

Short update to share:

```text
I documented the ESG pipeline handoff, including the parsing approach, OCR handling, section taxonomy and splitting logic, chunking parameters, QA gates, and current company-level exceptions. The current audit shows 473 parsed documents, 6,024 sections, 32,890 chunks, 0 duplicate chunk IDs, and 0 invalid token-count chunks. Remaining manual-review items are mainly citation metadata gaps, low text quality/under-sectioning for a few reports, one ETSY missing-PDF tracker mismatch, and not-found coverage explanations.
```
