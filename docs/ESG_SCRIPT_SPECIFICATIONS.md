# ESG Pipeline Script Specifications

Owner role: ESG Document Intelligence and QA Lead.

Purpose: finish the ESG/sustainability-report side of the Retail Intelligence Pipeline. The 10-K pipeline is already ahead on `Master_Phase_2`; the missing work is converting downloaded ESG PDFs into parsed text, ESG sections, chunks, PostgreSQL-ready records, and QA evidence.

Use this file as a handoff prompt for another Codex/chat session. The session should inspect the current repo first, then implement these specs without changing unrelated 10-K behavior.

## Current Repo Facts

- Repo: `Hanzala-Shadow/retail-intelligence-ai`
- Aziz branch: `Phase_2_Aziz`
- Integration branch to sync from: `origin/Master_Phase_2`
- Aziz branch was observed to be 22 commits behind `origin/Master_Phase_2`.
- Existing ESG-related scripts:
  - `src/drive_downloader.py`: downloads ESG PDFs from Google Drive into `data/01_raw/sustainability/{ticker}/`.
  - `src/pdf_parser.py`: parses PDFs with `pdfplumber`, but currently only reports parsed character counts. It must be extended to save parsed text and parse status.
  - `src/table_extractor.py`: indexes extracted table CSVs, if table CSVs exist.
- Missing ESG deliverables:
  - `src/section_splitter_esg.py`
  - `src/esg_chunker.py` or a safe ESG mode in `src/chunker.py`
  - `src/drive_to_db.py`
  - `src/esg_pipeline_qa.py`
  - `docs/ESG_PIPELINE.md`
  - `data/00_reference/esg_pipeline_qa.csv`

## Required Setup Before Coding

Run this first from repo root:

```powershell
git fetch origin
git checkout Phase_2_Aziz
git merge origin/Master_Phase_2
git status
```

Resolve conflicts by preserving the `Master_Phase_2` 10-K pipeline unless the conflict is clearly inside ESG-only code.

Do not commit secrets. `client_secret.json`, `token.json`, `.env`, and Drive credentials must remain untracked.

## Existing Data Contracts

### Input Tracker

Source file:

```text
data/00_reference/sustainability_report_tracker.csv
```

Columns:

```text
company_id,ticker,company_name,report_year,format,drive_file_link,status,notes
```

Known tracker issues to handle or report:

- Some `drive_file_link` values are only tickers, not real Drive URLs or file IDs.
- Some `status` values may be `downloaded`, `not found`, blank, or inconsistent capitalization.
- Some `report_year` values contain multiple years in one cell, e.g. `2025-2024-2023-2022`.
- `format` may be blank even when status is downloaded.

### Raw PDF Location

Downloaded ESG PDFs should live here:

```text
data/01_raw/sustainability/{ticker}/{pdf_filename}.pdf
```

### Parsed Text Location

New parsed ESG text files should be saved here:

```text
data/02_interim/esg_text/{ticker}/{pdf_stem}.txt
```

### ESG Section Location

New ESG section files should be saved here:

```text
data/03_sections/esg/{ticker}/{pdf_stem}__{section_instance_id}.txt
```

### ESG Chunk Location

New ESG chunk files should be saved here:

```text
data/04_chunks/esg/{ticker}/{pdf_stem}__{section_instance_id}__chunk_{0000}.txt
```

### ESG Index Files

Write these CSVs:

```text
data/00_reference/esg_parse_index.csv
data/00_reference/esg_sections_index.csv
data/00_reference/esg_chunks_index.csv
data/00_reference/esg_pipeline_qa.csv
data/00_reference/esg_source_registry.csv
```

Do not overwrite the 10-K index files unless explicitly requested. Existing 10-K files include `chunks_index.csv`, `sections_index.csv`, `chunk_qa_report.csv`, etc.

## Database Contract

Schema file:

```text
data/05_db/migrations/V1__Schema.sql
```

Tables relevant to ESG:

```sql
sustainability_reports(report_id, company_id, year, report_url, format, download_status)
documents(doc_id, company_id, doc_type, filepath, parse_status)
sections(section_id, doc_id, section_instance_id, section_code, section_title, section_text, char_count)
chunks(chunk_id, external_chunk_id, section_id, doc_id, company_id, section_instance_id, section_code, source_id, source_version_id, chunk_index, chunk_text, token_count)
```

Required ESG values:

- `documents.doc_type = 'sustainability'`
- For scanned PDFs with no usable text: `documents.parse_status = 'ocr_required'`
- For parsed PDFs: `documents.parse_status = 'parsed'`
- For downloaded reports: `sustainability_reports.download_status = 'downloaded'`
- For reports not published/found: `sustainability_reports.download_status = 'not_found'`

If the existing ORM or DB helper uses different allowed strings, use the existing helper constants, but keep output semantically equivalent.

## Script 1: Extend `src/pdf_parser.py`

Current problem: it extracts text but does not save parsed text or produce a durable manifest.

Required behavior:

- CLI:

```bash
python src/pdf_parser.py --root data/01_raw/sustainability --out data/02_interim/esg_text --index data/00_reference/esg_parse_index.csv --workers 1
python src/pdf_parser.py --ticker GAP --root data/01_raw/sustainability --out data/02_interim/esg_text --index data/00_reference/esg_parse_index.csv --workers 1
```

- Discover PDFs under `--root/{ticker}/`.
- Parse each PDF with `pdfplumber`.
- Save extracted text to `--out/{ticker}/{pdf_stem}.txt`.
- Write one CSV row per PDF to `esg_parse_index.csv`.

Required `esg_parse_index.csv` columns:

```text
ticker,pdf_file,source_pdf,parsed_text_file,status,error_message,page_count,char_count,table_count,content_hash,parsed_at
```

Status values:

- `parsed`: text extracted successfully.
- `ocr_required`: PDF had too little usable text.
- `failed`: parser crashed or file unreadable.

OCR rule:

- If total extracted text has fewer than 500 non-whitespace characters, mark `ocr_required`.
- Do not crash the pipeline for scanned PDFs.

Other requirements:

- Keep the memory-safe worker behavior already in `pdf_parser.py`.
- Set `ParsedDocument.doc_type = 'sustainability'`.
- Set `ParsedDocument.parser_used = 'pdfplumber'`.
- Use UTF-8 output.
- Create parent directories automatically.

QA for `pdf_parser.py`:

- Run on one known ticker:

```bash
python src/pdf_parser.py --ticker GAP --root data/01_raw/sustainability --out data/02_interim/esg_text --index data/00_reference/esg_parse_index.csv --workers 1
```

- Pass criteria:
  - `data/02_interim/esg_text/GAP/` contains at least one `.txt` file if GAP PDFs exist.
  - `esg_parse_index.csv` exists and has one row per processed PDF.
  - No row has blank `ticker`, `pdf_file`, `source_pdf`, or `status`.
  - `status` is only `parsed`, `ocr_required`, or `failed`.
  - `parsed` rows have `char_count >= 500`.
  - `ocr_required` rows have `char_count < 500`.

## Script 2: Create `src/section_splitter_esg.py`

Purpose: split parsed ESG report text into section files usable by the chunker and database.

CLI:

```bash
python src/section_splitter_esg.py --input data/02_interim/esg_text --out data/03_sections/esg --index data/00_reference/esg_sections_index.csv
python src/section_splitter_esg.py --ticker GAP --input data/02_interim/esg_text --out data/03_sections/esg --index data/00_reference/esg_sections_index.csv
```

Input:

```text
data/02_interim/esg_text/{ticker}/{pdf_stem}.txt
```

Output:

```text
data/03_sections/esg/{ticker}/{pdf_stem}__{section_instance_id}.txt
data/00_reference/esg_sections_index.csv
```

Required `esg_sections_index.csv` columns:

```text
ticker,pdf_stem,section_instance_id,section_code,section_title,section_file,source_start_char,source_end_char,provenance_version,page_start,page_end,char_count,word_count,split_method,confidence,source_size_bytes,source_mtime_utc,source_sha256
```

Required canonical section codes:

```text
ceo_letter
about_this_report
environmental
climate
energy
emissions
waste
water
social
human_capital
diversity_equity_inclusion
supply_chain_ethics
community
governance
ethics_compliance
data_summary
appendix
other
full_document
```

Heading detection requirements:

- Use regex/heuristics for line-level headings.
- Accept common variants:
  - Environmental, Environment, Planet
  - Climate, Climate Change, Climate Risk
  - Greenhouse Gas, GHG, Scope 1, Scope 2, Scope 3
  - Social, People, Associates, Employees, Human Capital
  - Diversity, Equity, Inclusion, DEI
  - Supply Chain, Responsible Sourcing, Human Rights
  - Governance, Ethics, Compliance, Board
  - Data Summary, ESG Data, Performance Data, Metrics
  - About This Report, Reporting Framework, GRI, SASB, TCFD
- Avoid table-of-contents false positives:
  - Ignore heading candidates in the first 10 percent of the document if many headings appear close together with page numbers.
  - Ignore lines ending with only a page number unless repeated later with body text.
- If no reliable sections are found, create one `full_document` section.
- Skip sections below 300 characters unless they are merged into adjacent `other`/parent section.
- Preserve each section as one exact contiguous source slice. Merge repeated
  `section_code` values only when they are adjacent with a whitespace-only gap.
- Assign repeated topics distinct instance IDs such as `community__0001` and
  `community__0002`.

Confidence:

- `high`: clear heading match and section body length is substantial.
- `medium`: heading match but weak length or ambiguous title.
- `low`: fallback or uncertain split.

QA for `section_splitter_esg.py`:

- Run:

```bash
python src/section_splitter_esg.py --ticker GAP --input data/02_interim/esg_text --out data/03_sections/esg --index data/00_reference/esg_sections_index.csv
```

- Pass criteria:
  - Every parsed text file gets at least one section row.
  - Every section row points to an existing `.txt` file.
  - No section file is empty.
  - At least 80 percent of parsed ESG PDFs produce more than one section, unless reports are very short.
  - `section_code` values are only from the canonical list.
  - No duplicate `(ticker, pdf_stem, section_instance_id)` rows.

## Script 3: Create `src/esg_chunker.py`

Purpose: chunk ESG section text without disturbing the existing 10-K chunker.

CLI:

```bash
python src/esg_chunker.py --input data/03_sections/esg --out data/04_chunks/esg --index data/00_reference/esg_chunks_index.csv
python src/esg_chunker.py --ticker GAP --input data/03_sections/esg --out data/04_chunks/esg --index data/00_reference/esg_chunks_index.csv
```

Chunking rules:

- Use `tiktoken` encoding `cl100k_base`.
- Target chunk size: 500 tokens.
- Overlap: 50 tokens.
- Minimum output chunk: 100 tokens for ESG.
- Maximum output chunk: 600 tokens.
- Preserve meaningful section files below 100 tokens as
  `chunk_type=short_evidence` when `25 <= token_count < 100`.
- Skip only obvious table-of-contents/navigation short sections and record them
  as excluded in summary output.
- Do not delete or overwrite `data/04_chunks/10k`.

Required `esg_chunks_index.csv` columns:

```text
chunk_id,source_id,source_version_id,ticker,doc_type,source_type,pdf_stem,section_instance_id,section_code,chunk_index,chunk_type,short_section_action,short_section_reason,merged_section_ids,token_count,char_count,chunk_file,source_section_file,source_start_char,source_end_char,page_start,page_end,citation_ready,citation_validation_status,citation_validation_version
```

Required values:

- `doc_type` is governed source metadata such as `sustainability` or
  `annual_report_with_esg`.
- `chunk_id = {source_id}__{section_instance_id}__chunk_{0000}`
- `citation_ready=true` only for `semantic_v1` with status
  `verified_exact` or `verified_whitespace_normalized`.

QA for `esg_chunker.py`:

- Run:

```bash
python src/esg_chunker.py --ticker GAP --input data/03_sections/esg --out data/04_chunks/esg --index data/00_reference/esg_chunks_index.csv
```

- Pass criteria:
  - `esg_chunks_index.csv` exists.
  - Every indexed `chunk_file` exists.
  - Every chunk has a governed `doc_type` and stable `source_id`.
  - Every normal chunk has `100 <= token_count <= 600`.
  - Every `short_evidence` chunk has `25 <= token_count < 100`.
  - No duplicate `chunk_id`.
  - 10-K chunk files and indexes are untouched.

## Script 4: Create `src/drive_to_db.py`

Purpose: reconcile tracker, parsed ESG files, sections, and chunks into PostgreSQL tables.

CLI:

```bash
python src/drive_to_db.py --dry-run
python src/drive_to_db.py --commit
```

Inputs:

```text
data/00_reference/companies.csv
data/00_reference/sustainability_report_tracker.csv
data/00_reference/esg_parse_index.csv
data/00_reference/esg_sections_index.csv
data/00_reference/esg_chunks_index.csv
```

Required behavior:

- Load companies by ticker and map to `company_id`.
- Insert/update `sustainability_reports` from tracker.
- Insert/update `documents` for each parsed/downloaded ESG PDF.
- Insert `sections` from `esg_sections_index.csv`.
- Insert `chunks` from `esg_chunks_index.csv`.
- Support `--dry-run` that prints planned row counts and anomalies without writing.
- Support `--commit` for actual writes.
- Must be idempotent: repeated runs should not duplicate rows.

Tracker mapping:

- `status = downloaded` -> `download_status = downloaded`
- `status = not found` or `not_found` -> `download_status = not_found`
- blank status -> do not silently insert as downloaded; mark as anomaly in QA.
- `drive_file_link` -> store in `report_url` even if it is only a ticker, but flag bad values in QA.

Year handling:

- If `report_year` contains one year, use that year.
- If it contains multiple years like `2025-2024-2023`, create one sustainability report row per year only if the same PDF set clearly contains multiple report files. If not clear, create a metadata row with the latest year and preserve the raw multi-year string in QA notes.
- Do not invent years not present in tracker or filenames.

Idempotency approach:

- If schema lacks uniqueness constraints, query existing rows before inserting.
- A document can be matched by `(company_id, doc_type, filepath)`.
- A section is matched by `(doc_id, section_instance_id)`; `section_code` remains
  a reusable taxonomy label and is not a physical-section key.
- A chunk can be matched by `(doc_id, section_id, chunk_index)`.

QA for `drive_to_db.py`:

- Run:

```bash
python src/drive_to_db.py --dry-run
```

- Pass criteria:
  - Dry run prints planned insert/update counts for all four ESG tables.
  - Dry run reports tracker anomalies.
  - No DB writes happen in dry run.
  - Commit mode can be run twice without increasing row counts unexpectedly.

Required DB validation queries after commit:

```sql
SELECT COUNT(*) FROM documents WHERE doc_type = 'sustainability';

SELECT parse_status, COUNT(*)
FROM documents
WHERE doc_type = 'sustainability'
GROUP BY parse_status;

SELECT COUNT(*)
FROM sustainability_reports sr
JOIN companies c ON c.company_id = sr.company_id
WHERE sr.download_status = 'downloaded';

SELECT d.doc_id, c.ticker, d.filepath
FROM documents d
JOIN companies c ON c.company_id = d.company_id
LEFT JOIN sections s ON s.doc_id = d.doc_id
WHERE d.doc_type = 'sustainability'
  AND d.parse_status = 'parsed'
GROUP BY d.doc_id, c.ticker, d.filepath
HAVING COUNT(s.section_id) = 0;

SELECT ch.chunk_id, ch.token_count
FROM chunks ch
JOIN documents d ON d.doc_id = ch.doc_id
WHERE d.doc_type = 'sustainability'
  AND NOT (
    (COALESCE(ch.chunk_type, 'normal') = 'normal' AND ch.token_count BETWEEN 100 AND 600)
    OR (ch.chunk_type = 'short_evidence' AND ch.token_count BETWEEN 25 AND 99)
  );
```

## Script 5: Create `src/esg_pipeline_qa.py`

Purpose: produce the final ESG pipeline proof file for Aziz and the sprint review.

CLI:

```bash
python src/esg_pipeline_qa.py --out data/00_reference/esg_pipeline_qa.csv
```

Inputs:

```text
data/00_reference/sustainability_report_tracker.csv
data/00_reference/esg_parse_index.csv
data/00_reference/esg_sections_index.csv
data/00_reference/esg_chunks_index.csv
data/00_reference/companies.csv
```

Output:

```text
data/00_reference/esg_pipeline_qa.csv
```

Required columns:

```text
company_id,ticker,company_name,tracker_status,report_year,format,drive_file_link,pdf_count,parsed_count,ocr_required_count,failed_parse_count,section_count,chunk_count,min_chunk_tokens,max_chunk_tokens,status,notes
```

Status rules:

- `complete`: downloaded, at least one parsed PDF, at least one section, at least one valid chunk.
- `ocr_required`: downloaded, PDFs exist, but all parsed output is below OCR threshold.
- `parse_failed`: downloaded, PDFs exist, parser failed.
- `missing_pdf`: tracker says downloaded but no local PDF exists.
- `not_found`: tracker says not found.
- `tracker_needs_cleanup`: blank/invalid tracker status or impossible metadata.
- `incomplete`: any other partial state.

QA checks performed by this script:

- Tracker rows with blank status.
- `downloaded` rows with no local PDF.
- `downloaded` rows with blank `format`.
- `drive_file_link` values that are just tickers or blank.
- Parsed rows with missing text files.
- Section index rows with missing section files.
- Chunk index rows with missing chunk files.
- Chunks below 100 or above 600 tokens.
- Tickers in tracker missing from companies.csv.
- Companies with ESG chunks but no tracker row.

Pass criteria:

- Script exits with code 0 after writing CSV.
- It prints summary counts by status.
- It prints the highest-priority fixes first:
  1. downloaded but no local PDF
  2. parsed but zero sections
  3. sections but zero chunks
  4. invalid chunk token counts
  5. tracker cleanup issues

## Script 6: Create `docs/ESG_PIPELINE.md`

Purpose: short human documentation for the final contribution.

Required sections:

```text
# ESG Document Intelligence Pipeline
## Purpose
## Inputs
## Outputs
## Script Order
## Status Values
## OCR Handling
## QA Report
## Known Limitations
## How to Re-run One Ticker
## How to Re-run Full ESG Pipeline
```

Required command sequence:

```bash
python src/drive_downloader.py --dry-run
python src/drive_downloader.py
python src/pdf_parser.py --root data/01_raw/sustainability --out data/02_interim/esg_text --index data/00_reference/esg_parse_index.csv --workers 1
python src/section_splitter_esg.py --input data/02_interim/esg_text --out data/03_sections/esg --index data/00_reference/esg_sections_index.csv
python src/esg_chunker.py --input data/03_sections/esg --out data/04_chunks/esg --index data/00_reference/esg_chunks_index.csv
python src/esg_pipeline_qa.py --out data/00_reference/esg_pipeline_qa.csv
python src/drive_to_db.py --dry-run
python src/drive_to_db.py --commit
```

## Preferred Implementation Order

1. Merge `origin/Master_Phase_2` into `Phase_2_Aziz`.
2. Extend `pdf_parser.py` to save text and `esg_parse_index.csv`.
3. Implement `section_splitter_esg.py`.
4. Implement `esg_chunker.py`.
5. Implement `esg_pipeline_qa.py`.
6. Implement `drive_to_db.py`.
7. Add `docs/ESG_PIPELINE.md`.
8. Run one ticker end-to-end, preferably `GAP`.
9. Run 5 diverse tickers:
   - `GAP`
   - `TJX`
   - `AEO`
   - `ETSY`
   - `CROX`
10. Run full ESG pipeline.

## Final Acceptance Checklist

The ESG work is done only when all are true:

- `data/00_reference/esg_parse_index.csv` exists and covers every local ESG PDF.
- `data/00_reference/esg_sections_index.csv` exists and every parsed PDF has at least one section.
- `data/00_reference/esg_chunks_index.csv` exists and all chunks are `doc_type=sustainability`.
- `data/00_reference/esg_pipeline_qa.csv` exists and clearly classifies every tracker row.
- No ESG chunk has fewer than 100 tokens or more than 600 tokens.
- Scanned PDFs are marked `ocr_required`, not treated as failures.
- Running `drive_to_db.py --dry-run` reports no critical blockers.
- Running `drive_to_db.py --commit` twice is idempotent.
- 10-K outputs are not overwritten or damaged.
- `docs/ESG_PIPELINE.md` explains how another person can rerun ESG processing.

## Role Framing for Aziz

Use this wording externally:

> I led the ESG document intelligence pipeline for a retail AI system covering nearly 200 public companies, transforming unstructured sustainability PDFs into parsed, sectioned, tokenized, database-ready text with quality checks for downstream retrieval and classification.

This is the CS/AI-relevant version of the role: document parsing, NLP preprocessing, data quality, evaluation, database integration, and reproducible pipeline design.
