-- V4__10K_Filing_Linkage_RAG_Status.sql
-- Links parsed 10-K documents to annual filings and populates safe RAG metadata.
-- This migration deliberately does not populate HTML page ranges or character offsets.
-- Sustainability/ESG rows are not modified.

BEGIN;

-- ------------------------------------------------------------------
-- 1. Establish an exact document-to-filing relationship
-- ------------------------------------------------------------------

ALTER TABLE documents
ADD COLUMN IF NOT EXISTS filing_id INTEGER;

-- Extract the SEC accession number from each 10-K document filepath.
-- Example:
-- data/01_raw/10k/AAPL/0000320193-23-000106.htm
UPDATE documents AS d
SET filing_id = af.filing_id
FROM annual_filings AS af
WHERE d.doc_type = '10-K'
  AND d.company_id = af.company_id
  AND af.accession_number =
      substring(
          d.filepath
          FROM '([0-9]{10}-[0-9]{2}-[0-9]{6})'
      )
  AND d.filing_id IS DISTINCT FROM af.filing_id;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'documents_filing_id_fkey'
    ) THEN
        ALTER TABLE documents
        ADD CONSTRAINT documents_filing_id_fkey
        FOREIGN KEY (filing_id)
        REFERENCES annual_filings(filing_id)
        ON DELETE CASCADE;
    END IF;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_filing_id
ON documents(filing_id)
WHERE filing_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_documents_filing_id
ON documents(filing_id);

-- ------------------------------------------------------------------
-- 2. Populate safe document-level quality metadata
-- ------------------------------------------------------------------

UPDATE documents
SET
    parse_status = 'parsed',
    possible_wrong_doc_type = FALSE,
    doc_quality_status = 'passed',
    rag_action = 'include',
    quality_flags = '[]',
    updated_at = CURRENT_TIMESTAMP
WHERE doc_type = '10-K'
  AND filing_id IS NOT NULL;

-- Any unmatched 10-K should not silently pass.
UPDATE documents
SET
    doc_quality_status = 'review_required',
    rag_action = 'review',
    quality_flags = '["missing_filing_link"]',
    updated_at = CURRENT_TIMESTAMP
WHERE doc_type = '10-K'
  AND filing_id IS NULL;

-- ------------------------------------------------------------------
-- 3. Populate safe chunk-level RAG metadata
-- ------------------------------------------------------------------

-- Substantive validated chunks are eligible for retrieval.
UPDATE chunks AS ch
SET
    doc_quality_status = 'passed',
    rag_action = 'include',
    quality_flags = '[]',
    citation_ready = TRUE,
    updated_at = CURRENT_TIMESTAMP
FROM documents AS d
WHERE ch.doc_id = d.doc_id
  AND d.doc_type = '10-K'
  AND d.filing_id IS NOT NULL
  AND ch.section_code NOT IN ('HEADER', 'Signatures')
  AND ch.chunk_text IS NOT NULL
  AND BTRIM(ch.chunk_text) <> ''
  AND ch.token_count BETWEEN 50 AND 500;

-- Retain boilerplate in PostgreSQL for lineage, but exclude it from
-- ordinary embedding generation and semantic retrieval.
UPDATE chunks AS ch
SET
    doc_quality_status = 'passed',
    rag_action = 'exclude_boilerplate',
    quality_flags = '["retrieval_boilerplate"]',
    citation_ready = FALSE,
    updated_at = CURRENT_TIMESTAMP
FROM documents AS d
WHERE ch.doc_id = d.doc_id
  AND d.doc_type = '10-K'
  AND d.filing_id IS NOT NULL
  AND ch.section_code IN ('HEADER', 'Signatures');

-- Anything that fails the verified chunk requirements requires review.
UPDATE chunks AS ch
SET
    doc_quality_status = 'review_required',
    rag_action = 'review',
    quality_flags = '["chunk_validation_failure"]',
    citation_ready = FALSE,
    updated_at = CURRENT_TIMESTAMP
FROM documents AS d
WHERE ch.doc_id = d.doc_id
  AND d.doc_type = '10-K'
  AND (
      d.filing_id IS NULL
      OR ch.chunk_text IS NULL
      OR BTRIM(ch.chunk_text) = ''
      OR ch.token_count IS NULL
      OR ch.token_count < 50
      OR ch.token_count > 500
  );

CREATE INDEX IF NOT EXISTS idx_documents_filing_quality
ON documents(filing_id, doc_quality_status);

CREATE INDEX IF NOT EXISTS idx_chunks_retrieval_eligibility
ON chunks(doc_type, rag_action, citation_ready);

COMMIT;
