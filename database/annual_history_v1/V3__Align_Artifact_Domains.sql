BEGIN;

ALTER TABLE annual_history_documents
  DROP CONSTRAINT IF EXISTS
    annual_history_documents_parse_status_check;

ALTER TABLE annual_history_documents
  ADD CONSTRAINT annual_history_documents_parse_status_check
  CHECK (parse_status IN ('passed','review_required'));

ALTER TABLE annual_history_sections
  DROP CONSTRAINT IF EXISTS
    annual_history_sections_boundary_confidence_check;

ALTER TABLE annual_history_sections
  ADD CONSTRAINT
    annual_history_sections_boundary_confidence_check
  CHECK (boundary_confidence IN ('high','medium','low'));

COMMIT;
