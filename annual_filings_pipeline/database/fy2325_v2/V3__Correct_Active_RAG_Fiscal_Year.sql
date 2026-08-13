\set ON_ERROR_STOP on

BEGIN;

CREATE OR REPLACE VIEW rag_eligible_10k_chunks AS
SELECT
    ch.chunk_pk AS chunk_id,
    ch.source_chunk_id,
    ch.section_pk AS section_id,
    ch.document_pk AS doc_id,
    f.filing_pk AS filing_id,
    ch.company_id,
    ch.ticker,
    co.cik,
    ch.coverage_year AS filing_year,
    ch.coverage_year,
    ch.accession_number,
    f.filing_date,
    '10-K'::text AS doc_type,
    ch.rag_section_code AS section_code,
    ch.chunk_index,
    ch.chunk_text,
    ch.embedding_text,
    ch.token_count,
    ch.quality_status AS doc_quality_status,
    ch.rag_action,
    TRUE AS citation_ready,
    ch.quality_flags,
    ch.chunk_text_sha256,
    ch.embedding_text_sha256,
    ch.dataset_id
FROM fy2325_v2_chunks ch
JOIN fy2325_v2_embeddings e
  ON e.dataset_id = ch.dataset_id
 AND e.chunk_pk = ch.chunk_pk
JOIN fy2325_v2_documents d
  ON d.dataset_id = ch.dataset_id
 AND d.document_pk = ch.document_pk
JOIN fy2325_v2_filings f
  ON f.dataset_id = d.dataset_id
 AND f.filing_pk = d.filing_pk
JOIN fy2325_v2_companies co
  ON co.dataset_id = ch.dataset_id
 AND co.company_id = ch.company_id
JOIN fy2325_v2_datasets ds
  ON ds.dataset_id = ch.dataset_id
WHERE ch.rag_action = 'include'
  AND ch.quality_status = 'passed'
  AND ds.status IN ('validated', 'indexed', 'active');

DO $validation$
DECLARE
    active_count BIGINT;
    mismatch_count BIGINT;
    duplicate_chunks BIGINT;
BEGIN
    SELECT count(*)
    INTO active_count
    FROM rag_eligible_10k_chunks;

    IF active_count <> 158570 THEN
        RAISE EXCEPTION
          'active row count changed: actual %, expected 158570',
          active_count;
    END IF;

    SELECT count(*)
    INTO mismatch_count
    FROM rag_eligible_10k_chunks r
    JOIN fy2325_v2_filings f
      ON f.dataset_id = r.dataset_id
     AND f.accession_number = r.accession_number
    WHERE r.filing_year <> f.coverage_year;

    IF mismatch_count <> 0 THEN
        RAISE EXCEPTION
          'fiscal-year mismatches remain: %',
          mismatch_count;
    END IF;

    SELECT count(*) - count(DISTINCT source_chunk_id)
    INTO duplicate_chunks
    FROM rag_eligible_10k_chunks;

    IF duplicate_chunks <> 0 THEN
        RAISE EXCEPTION
          'duplicate active source chunk IDs: %',
          duplicate_chunks;
    END IF;
END
$validation$;

COMMIT;
