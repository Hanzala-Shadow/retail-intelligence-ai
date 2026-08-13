BEGIN;

-- Temporarily exclude only the three confirmed LIVE filings.
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
  AND c.ticker = 'LIVE'
  AND af.accession_number IN (
      '0001628280-23-042586',
      '0001628280-24-052099',
      '0001628280-25-057711'
  );

-- Keep the chunks for investigation, but make them unavailable for citation
-- and retrieval until their section boundaries are corrected and revalidated.
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
  AND c.ticker = 'LIVE'
  AND af.accession_number IN (
      '0001628280-23-042586',
      '0001628280-24-052099',
      '0001628280-25-057711'
  );

COMMIT;
