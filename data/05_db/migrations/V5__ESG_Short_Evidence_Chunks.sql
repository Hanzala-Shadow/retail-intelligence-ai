-- V5__ESG_Short_Evidence_Chunks.sql
-- Records retrieval-policy metadata for short ESG evidence chunks.

ALTER TABLE chunks
ADD COLUMN IF NOT EXISTS chunk_type VARCHAR(40);

ALTER TABLE chunks
ADD COLUMN IF NOT EXISTS short_section_action VARCHAR(40);

ALTER TABLE chunks
ADD COLUMN IF NOT EXISTS short_section_reason VARCHAR(120);

ALTER TABLE chunks
ADD COLUMN IF NOT EXISTS merged_section_ids TEXT;

UPDATE chunks
SET chunk_type = 'normal'
WHERE chunk_type IS NULL OR BTRIM(chunk_type) = '';

CREATE INDEX IF NOT EXISTS idx_chunks_chunk_type
ON chunks(chunk_type);

CREATE INDEX IF NOT EXISTS idx_chunks_short_section_action
ON chunks(short_section_action);
