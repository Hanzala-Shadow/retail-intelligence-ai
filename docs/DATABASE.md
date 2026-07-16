# Database Schema

Schema migrations live in `data/05_db/migrations/`.

Apply them in order:

1. `V1__Schema.sql`
2. `V2__Chunking_DB_Fixes.sql`
3. `V3__RAG_Metadata.sql`
4. `V4__10K_Filing_Linkage_RAG_Status.sql`
5. `V5__RAG_Eligible_10K_Embedding_View.sql`
6. `V6__Improve_10K_Embedding_Text_Cleaning.sql`
7. `V7__Exclude_LIVE_Section_Boundary_Contamination.sql`
8. `V8__Exclude_YHGJ_Section_Boundary_Contamination.sql`
9. `V9__Restore_Corrected_LIVE_YHGJ_10K_RAG.sql`
10. `V10__Enable_Pgvector_Extension.sql`
11. `V11__Exclude_10K_Item1_TOC_Chunks.sql`
12. `V12__Exclude_Reserved_Item6_Boilerplate.sql`
13. `V13__ESG_Provenance.sql`
14. `V14__ESG_Short_Evidence_Chunks.sql`

`page_start` and `page_end` remain nullable. ESG PDF citations use page and
character bounds; HTML filing provenance does not invent unstable page numbers.

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
- `section_instance_id`
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

- `(doc_id, section_instance_id)`

`section_code` is a reusable topic category. `section_instance_id` identifies
one contiguous occurrence, so a document may validly contain both
`community__0001` and `community__0002`.

## chunks

One row per retrieval chunk.

- `chunk_id`
- `external_chunk_id`
- `section_id`
- `doc_id`
- `company_id`
- `doc_type`
- `section_instance_id`
- `section_code`
- `source_id`
- `source_version_id`
- `chunk_type`
- `short_section_action`
- `short_section_reason`
- `merged_section_ids`
- `doc_quality_status`
- `rag_action`
- `quality_flags`
- `source_start_char`
- `source_end_char`
- `page_start`
- `page_end`
- `citation_ready`
- `citation_validation_status`
- `citation_validation_version`
- `chunk_index`
- `chunk_text`
- `token_count`
- `created_at`
- `updated_at`

Unique constraint:

- `(section_id, chunk_index)`
- `external_chunk_id`

## RAG Query Filter

For ESG-only retrieval, use only chunks where:

```sql
doc_quality_status = 'ok'
AND rag_action = 'index_as_esg'
AND citation_ready = TRUE
AND citation_validation_version = 'semantic_v1'
AND citation_validation_status IN (
    'verified_exact',
    'verified_whitespace_normalized'
)
AND (
    (COALESCE(chunk_type, 'normal') = 'normal' AND token_count BETWEEN 100 AND 600)
    OR (chunk_type = 'short_evidence' AND token_count BETWEEN 25 AND 99)
)
```

The `rag_action` gate can allow a governed annual-report ESG excerpt while
still excluding unrelated annual filings. `doc_type` remains available as a
filter and should be shown in citations, but it is not the chunk identity.

Do not use chunks where:

```sql
rag_action = 'exclude_from_esg_index'
OR doc_quality_status = 'exclude_from_esg_rag'
```
