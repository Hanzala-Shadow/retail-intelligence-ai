BEGIN;

-- Retain reserved Item 6 page-layout remnants for lineage, but exclude
-- them from embeddings, retrieval, and citation.
UPDATE chunks AS ch
SET
    doc_quality_status = 'passed',
    rag_action = 'exclude_boilerplate',
    quality_flags =
        '["retrieval_boilerplate","reserved_item6_boilerplate"]',
    citation_ready = FALSE,
    updated_at = CURRENT_TIMESTAMP
FROM documents AS d
WHERE ch.doc_id = d.doc_id
  AND d.doc_type = '10-K'
  AND ch.section_code = 'Item_6'
  AND ltrim(
      ch.chunk_text,
      chr(65279) || chr(8203) || chr(8288) ||
      E' \t\r\n'
  ) ~* (
      '^item[[:space:]]+6'
      '([[:space:].:–—-])+'
      '.{0,160}reserved'
  );

DO $$
DECLARE
    remaining_eligible INTEGER;
BEGIN
    SELECT COUNT(*)
    INTO remaining_eligible
    FROM chunks AS ch
    JOIN documents AS d
      ON d.doc_id = ch.doc_id
    WHERE d.doc_type = '10-K'
      AND ch.section_code = 'Item_6'
      AND ltrim(
          ch.chunk_text,
          chr(65279) || chr(8203) || chr(8288) ||
          E' \t\r\n'
      ) ~* (
          '^item[[:space:]]+6'
          '([[:space:].:–—-])+'
          '.{0,160}reserved'
      )
      AND (
          ch.rag_action = 'include'
          OR ch.citation_ready IS TRUE
      );

    IF remaining_eligible <> 0 THEN
        RAISE EXCEPTION
            '% reserved Item 6 boilerplate chunks remain eligible',
            remaining_eligible;
    END IF;
END
$$;

COMMIT;
