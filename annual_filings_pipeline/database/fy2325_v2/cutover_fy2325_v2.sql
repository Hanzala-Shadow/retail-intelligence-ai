\set ON_ERROR_STOP on
BEGIN;
DO $$
BEGIN
  IF (SELECT count(*) FROM rag_eligible_10k_chunks_v216_staging) <> 158570 THEN
    RAISE EXCEPTION 'staging RAG row count is not 158570';
  END IF;
END $$;
CREATE OR REPLACE VIEW rag_eligible_10k_chunks AS
SELECT chunk_id, section_id, doc_id, filing_id, company_id, ticker, cik,
       filing_year, accession_number, filing_date, doc_type, section_code,
       chunk_index, chunk_text, token_count, doc_quality_status, rag_action,
       citation_ready, quality_flags, embedding_text
FROM rag_eligible_10k_chunks_v216_staging;
UPDATE fy2325_v2_datasets
SET status='active', activated_at=now()
WHERE dataset_id='fy2325-v2.16' AND status='indexed';
COMMIT;
