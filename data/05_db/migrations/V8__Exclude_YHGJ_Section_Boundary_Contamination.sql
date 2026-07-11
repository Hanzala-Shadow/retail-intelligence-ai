BEGIN;

UPDATE documents d
SET
    doc_quality_status = 'review_required',
    rag_action = 'review',
    quality_flags = '["section_boundary_contamination"]'
FROM companies c
JOIN annual_filings af
    ON af.company_id = c.company_id
WHERE d.company_id = c.company_id
  AND d.filing_id = af.filing_id
  AND c.ticker = 'YHGJ'
  AND af.accession_number IN (
      '0001493152-24-011910',
      '0001641172-25-004622',
      '0001493152-26-012156'
  );

UPDATE chunks ch
SET
    doc_quality_status = 'review_required',
    rag_action = 'review',
    quality_flags = '["section_boundary_contamination"]',
    citation_ready = FALSE
FROM documents d
JOIN companies c
    ON c.company_id = d.company_id
JOIN annual_filings af
    ON af.filing_id = d.filing_id
WHERE ch.doc_id = d.doc_id
  AND c.ticker = 'YHGJ'
  AND af.accession_number IN (
      '0001493152-24-011910',
      '0001641172-25-004622',
      '0001493152-26-012156'
  );

COMMIT;
