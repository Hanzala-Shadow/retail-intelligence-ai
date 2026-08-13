-- V5__RAG_Eligible_10K_Embedding_View.sql
-- Adds cleaned embedding text while preserving the existing view column order.
-- Original chunk_text remains unchanged for citations.
-- No ESG/PDF data is modified.

BEGIN;

CREATE OR REPLACE VIEW rag_eligible_10k_chunks AS
SELECT
    -- Existing view columns: their names and order must remain unchanged.
    ch.chunk_id,
    ch.section_id,
    ch.doc_id,
    d.filing_id,
    ch.company_id,
    c.ticker,
    c.cik,
    af.year AS filing_year,
    af.accession_number,
    af.filing_date,
    ch.doc_type,
    ch.section_code,
    ch.chunk_index,
    ch.chunk_text,
    ch.token_count,
    ch.doc_quality_status,
    ch.rag_action,
    ch.citation_ready,

    -- New columns must be appended at the end.
    ch.quality_flags,

    BTRIM(
        REGEXP_REPLACE(
            REGEXP_REPLACE(
                REGEXP_REPLACE(
                    REGEXP_REPLACE(
                        REPLACE(
                            REPLACE(
                                ch.chunk_text,
                                CHR(8203),
                                ' '
                            ),
                            '�',
                            ' '
                        ),
                        '\[[Tt][Aa][Bb][Ll][Ee]:table_[0-9]+\]',
                        ' ',
                        'g'
                    ),
                    '\m[0-9]{1,3}[[:space:]]+[Tt]able of [Cc]ontents\M',
                    ' ',
                    'g'
                ),
                '\m[Tt]able of [Cc]ontents\M',
                ' ',
                'g'
            ),
            '[[:space:]]+',
            ' ',
            'g'
        )
    ) AS embedding_text

FROM chunks ch
JOIN documents d
    ON d.doc_id = ch.doc_id
JOIN annual_filings af
    ON af.filing_id = d.filing_id
JOIN companies c
    ON c.company_id = ch.company_id
WHERE ch.doc_type = '10-K'
  AND d.doc_quality_status = 'passed'
  AND d.rag_action = 'include'
  AND ch.doc_quality_status = 'passed'
  AND ch.rag_action = 'include'
  AND ch.citation_ready IS TRUE
  AND ch.section_code NOT IN ('HEADER', 'Signatures')
  AND ch.token_count BETWEEN 50 AND 500
  AND ch.chunk_text IS NOT NULL
  AND BTRIM(ch.chunk_text) <> '';

COMMIT;
