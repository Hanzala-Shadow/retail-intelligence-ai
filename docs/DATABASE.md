# Database Schema

Schema migrations live in `data/05_db/migrations/`.

Apply them in order:

1. `V1__Schema.sql`
2. `V2__Chunking_DB_Fixes.sql`
3. `V3__RAG_Metadata.sql`

## companies

Core company reference table.

- `company_id`
- `ticker`
- `cik`
- `name`
- `sector`
- `exchange`
- `created_at`
- `updated_at`

## annual_filings

SEC 10-K filing tracker.

- `filing_id`
- `company_id`
- `year`
- `accession_number`
- `filing_date`
- `download_status`
- `drive_file_id`
- `created_at`
- `updated_at`

## sustainability_reports

One row per ESG/sustainability tracker report.

- `report_id`
- `company_id`
- `year`
- `report_url`
- `drive_file_id`
- `format`
- `download_status`
- `created_at`
- `updated_at`

Unique constraint:

- `(company_id, year)`

## documents

One row per source document.

- `doc_id`
- `company_id`
- `doc_type`
- `filepath`
- `parse_status`
- `quality_flags`
- `possible_wrong_doc_type`
- `doc_quality_status`
- `rag_action`
- `created_at`
- `updated_at`

Important values:

- `doc_type = sustainability`
- `doc_type = annual_report_with_esg`
- `doc_quality_status = ok`
- `doc_quality_status = needs_review`
- `doc_quality_status = exclude_from_esg_rag`
- `rag_action = index_as_esg`
- `rag_action = manual_review_before_indexing`
- `rag_action = exclude_from_esg_index`

Unique constraint:

- `filepath`

## sections

One row per extracted document section.

- `section_id`
- `doc_id`
- `section_code`
- `section_title`
- `section_text`
- `char_count`
- `source_start_char`
- `source_end_char`
- `page_start`
- `page_end`
- `created_at`
- `updated_at`

Unique constraint:

- `(doc_id, section_code)`

## chunks

One row per retrieval chunk.

- `chunk_id`
- `section_id`
- `doc_id`
- `company_id`
- `doc_type`
- `section_code`
- `doc_quality_status`
- `rag_action`
- `quality_flags`
- `source_start_char`
- `source_end_char`
- `page_start`
- `page_end`
- `citation_ready`
- `chunk_index`
- `chunk_text`
- `token_count`
- `created_at`
- `updated_at`

Unique constraint:

- `(section_id, chunk_index)`

## RAG Query Filter

For ESG-only retrieval, use only chunks where:

```sql
doc_type = 'sustainability'
AND doc_quality_status = 'ok'
AND rag_action = 'index_as_esg'
AND citation_ready = TRUE
```

Do not use chunks where:

```sql
rag_action = 'exclude_from_esg_index'
OR doc_quality_status = 'exclude_from_esg_rag'
```
