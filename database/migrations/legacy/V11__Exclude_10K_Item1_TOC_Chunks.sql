BEGIN;

-- Retain high-confidence Item 1 table-of-contents chunks for lineage,
-- but exclude them from citation and semantic retrieval.
--
-- Scope is deliberately narrow:
--   * 10-K documents only
--   * Item_1 only
--   * first chunk only
--   * at least five distinct subsequent SEC item labels
WITH toc_chunks AS (
    SELECT
        ch.chunk_id
    FROM chunks AS ch
    JOIN documents AS d
      ON d.doc_id = ch.doc_id
    CROSS JOIN LATERAL regexp_matches(
        ch.chunk_text,
        '(^|[^[:alnum:]])item[[:space:]]+(1a|1b|1c|2|3|4|5|6|7a|7|8|9a|9b|9c|9|10|11|12|13|14|15|16)([[:space:].:–—-]|$)',
        'gi'
    ) AS match_result
    WHERE d.doc_type = '10-K'
      AND ch.section_code = 'Item_1'
      AND ch.chunk_index = 0
    GROUP BY ch.chunk_id
    HAVING COUNT(
        DISTINCT UPPER(match_result[2])
    ) >= 5
)
UPDATE chunks AS ch
SET
    doc_quality_status = 'passed',
    rag_action = 'exclude_boilerplate',
    quality_flags = '["retrieval_boilerplate","sec_item_toc"]',
    citation_ready = FALSE,
    updated_at = CURRENT_TIMESTAMP
FROM toc_chunks
WHERE ch.chunk_id = toc_chunks.chunk_id;

-- Abort if any detected TOC chunk remains eligible after the update.
DO $$
DECLARE
    remaining_eligible INTEGER;
BEGIN
    SELECT COUNT(*)
    INTO remaining_eligible
    FROM (
        SELECT
            ch.chunk_id
        FROM chunks AS ch
        JOIN documents AS d
          ON d.doc_id = ch.doc_id
        CROSS JOIN LATERAL regexp_matches(
            ch.chunk_text,
            '(^|[^[:alnum:]])item[[:space:]]+(1a|1b|1c|2|3|4|5|6|7a|7|8|9a|9b|9c|9|10|11|12|13|14|15|16)([[:space:].:–—-]|$)',
            'gi'
        ) AS match_result
        WHERE d.doc_type = '10-K'
          AND ch.section_code = 'Item_1'
          AND ch.chunk_index = 0
          AND (
              ch.rag_action = 'include'
              OR ch.citation_ready IS TRUE
          )
        GROUP BY ch.chunk_id
        HAVING COUNT(
            DISTINCT UPPER(match_result[2])
        ) >= 5
    ) AS still_eligible;

    IF remaining_eligible <> 0 THEN
        RAISE EXCEPTION
            '% Item 1 TOC chunks remain retrieval eligible',
            remaining_eligible;
    END IF;
END
$$;

COMMIT;
