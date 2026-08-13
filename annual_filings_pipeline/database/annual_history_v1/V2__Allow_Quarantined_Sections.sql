BEGIN;

ALTER TABLE annual_history_sections
  DROP CONSTRAINT annual_history_sections_quality_status_check,
  DROP CONSTRAINT annual_history_sections_rag_action_check;

ALTER TABLE annual_history_sections
  ADD CONSTRAINT annual_history_sections_quality_status_check
    CHECK (
      quality_status IN ('passed','review_required')
    ),
  ADD CONSTRAINT annual_history_sections_rag_action_check
    CHECK (
      rag_action IN ('include','exclude','review_required')
    ),
  ADD CONSTRAINT annual_history_sections_state_coherence_check
    CHECK (
      (
        quality_status = 'passed'
        AND rag_action IN ('include','exclude')
      )
      OR
      (
        quality_status = 'review_required'
        AND rag_action IN ('exclude','review_required')
      )
    );

COMMIT;
