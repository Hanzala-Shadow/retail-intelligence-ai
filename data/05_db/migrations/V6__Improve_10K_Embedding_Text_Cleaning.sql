BEGIN;

CREATE OR REPLACE VIEW rag_eligible_10k_chunks AS
SELECT
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
    ch.quality_flags,

    btrim(
        regexp_replace(
            regexp_replace(
                regexp_replace(
                    regexp_replace(
                        regexp_replace(
                            replace(
                                replace(
                                    ch.chunk_text,
                                    chr(8203),
                                    ' '
                                ),
                                '�',
                                ' '
                            ),
                            '\[table:table_[0-9]+\]',
                            ' ',
                            'gi'
                        ),

                        -- Remove page identifiers directly attached to the
                        -- recurring page-header phrase:
                        -- 59 Table of Contents
                        -- F-37 Table of Contents
                        -- F- 37 Table of Contents
                        -- iv Table of Contents
                        '(^|[[:space:]])([0-9]{1,4}|F[[:space:]]*-[[:space:]]*[0-9]{1,4}|[ivxlcdm]{1,12})[[:space:]]+table[[:space:]]+of[[:space:]]+contents',
                        ' ',
                        'gi'
                    ),

                    -- Remove any remaining presentation heading while
                    -- preserving the text surrounding it.
                    'table[[:space:]]+of[[:space:]]+contents',
                    ' ',
                    'gi'
                ),

                -- Normalize non-breaking spaces.
                chr(160),
                ' ',
                'g'
            ),

            -- Normalize all repeated whitespace after cleaning.
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
  AND btrim(ch.chunk_text) <> '';

COMMENT ON VIEW rag_eligible_10k_chunks IS
'Citation-ready substantive 10-K chunks. chunk_text is authoritative original text; embedding_text is a derived cleaned representation for embeddings.';

COMMIT;
