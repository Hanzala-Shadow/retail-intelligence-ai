-- V9__Restore_Corrected_LIVE_YHGJ_10K_RAG.sql
-- Restores six LIVE/YHGJ filings after corrected section rebuilding,
-- Unicode-safe chunk rebuilding, and database structural QA.

BEGIN;

DO $$
DECLARE
    target_documents INTEGER;
    target_chunks INTEGER;
    invalid_chunks INTEGER;
BEGIN
    SELECT COUNT(*)
    INTO target_documents
    FROM documents d
    JOIN companies c
      ON c.company_id = d.company_id
    JOIN annual_filings af
      ON af.filing_id = d.filing_id
    WHERE (
        c.ticker = 'LIVE'
        AND af.accession_number IN (
            '0001628280-23-042586',
            '0001628280-24-052099',
            '0001628280-25-057711'
        )
    ) OR (
        c.ticker = 'YHGJ'
        AND af.accession_number IN (
            '0001493152-24-011910',
            '0001641172-25-004622',
            '0001493152-26-012156'
        )
    );

    IF target_documents <> 6 THEN
        RAISE EXCEPTION
            'Expected 6 corrected target documents, found %',
            target_documents;
    END IF;

    SELECT COUNT(*)
    INTO target_chunks
    FROM chunks ch
    JOIN documents d
      ON d.doc_id = ch.doc_id
    JOIN companies c
      ON c.company_id = d.company_id
    JOIN annual_filings af
      ON af.filing_id = d.filing_id
    WHERE (
        c.ticker = 'LIVE'
        AND af.accession_number IN (
            '0001628280-23-042586',
            '0001628280-24-052099',
            '0001628280-25-057711'
        )
    ) OR (
        c.ticker = 'YHGJ'
        AND af.accession_number IN (
            '0001493152-24-011910',
            '0001641172-25-004622',
            '0001493152-26-012156'
        )
    );

    IF target_chunks <> 1307 THEN
        RAISE EXCEPTION
            'Expected 1307 corrected target chunks, found %',
            target_chunks;
    END IF;

    SELECT COUNT(*)
    INTO invalid_chunks
    FROM chunks ch
    JOIN documents d
      ON d.doc_id = ch.doc_id
    JOIN companies c
      ON c.company_id = d.company_id
    JOIN annual_filings af
      ON af.filing_id = d.filing_id
    WHERE (
        (
            c.ticker = 'LIVE'
            AND af.accession_number IN (
                '0001628280-23-042586',
                '0001628280-24-052099',
                '0001628280-25-057711'
            )
        ) OR (
            c.ticker = 'YHGJ'
            AND af.accession_number IN (
                '0001493152-24-011910',
                '0001641172-25-004622',
                '0001493152-26-012156'
            )
        )
    )
      AND (
          ch.chunk_text IS NULL
          OR BTRIM(ch.chunk_text) = ''
          OR ch.token_count IS NULL
          OR ch.token_count < 50
          OR ch.token_count > 500
          OR STRPOS(ch.chunk_text, '�') > 0
      );

    IF invalid_chunks <> 0 THEN
        RAISE EXCEPTION
            'Found % invalid corrected target chunks',
            invalid_chunks;
    END IF;
END
$$;

UPDATE documents d
SET
    doc_quality_status = 'passed',
    rag_action = 'include',
    quality_flags = '[]',
    updated_at = CURRENT_TIMESTAMP
FROM companies c,
     annual_filings af
WHERE d.company_id = c.company_id
  AND d.filing_id = af.filing_id
  AND (
      (
          c.ticker = 'LIVE'
          AND af.accession_number IN (
              '0001628280-23-042586',
              '0001628280-24-052099',
              '0001628280-25-057711'
          )
      ) OR (
          c.ticker = 'YHGJ'
          AND af.accession_number IN (
              '0001493152-24-011910',
              '0001641172-25-004622',
              '0001493152-26-012156'
          )
      )
  );

UPDATE chunks ch
SET
    doc_quality_status = 'passed',
    rag_action = 'include',
    quality_flags = '[]',
    citation_ready = TRUE,
    updated_at = CURRENT_TIMESTAMP
FROM documents d,
     companies c,
     annual_filings af
WHERE ch.doc_id = d.doc_id
  AND d.company_id = c.company_id
  AND d.filing_id = af.filing_id
  AND ch.section_code NOT IN ('HEADER', 'Signatures')
  AND (
      (
          c.ticker = 'LIVE'
          AND af.accession_number IN (
              '0001628280-23-042586',
              '0001628280-24-052099',
              '0001628280-25-057711'
          )
      ) OR (
          c.ticker = 'YHGJ'
          AND af.accession_number IN (
              '0001493152-24-011910',
              '0001641172-25-004622',
              '0001493152-26-012156'
          )
      )
  );

UPDATE chunks ch
SET
    doc_quality_status = 'passed',
    rag_action = 'exclude_boilerplate',
    quality_flags = '["retrieval_boilerplate"]',
    citation_ready = FALSE,
    updated_at = CURRENT_TIMESTAMP
FROM documents d,
     companies c,
     annual_filings af
WHERE ch.doc_id = d.doc_id
  AND d.company_id = c.company_id
  AND d.filing_id = af.filing_id
  AND ch.section_code IN ('HEADER', 'Signatures')
  AND (
      (
          c.ticker = 'LIVE'
          AND af.accession_number IN (
              '0001628280-23-042586',
              '0001628280-24-052099',
              '0001628280-25-057711'
          )
      ) OR (
          c.ticker = 'YHGJ'
          AND af.accession_number IN (
              '0001493152-24-011910',
              '0001641172-25-004622',
              '0001493152-26-012156'
          )
      )
  );

COMMIT;
