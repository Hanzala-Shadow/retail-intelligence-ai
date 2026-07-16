-- V13__ESG_Provenance.sql
-- Preserves contiguous ESG section identities and validated citation provenance.

ALTER TABLE sections
ADD COLUMN IF NOT EXISTS section_instance_id VARCHAR(100);

-- Existing loaders treated section_code as a unique physical section. Preserve
-- that identity for normal legacy rows and give any code-less row a stable ID.
UPDATE sections
SET section_instance_id = COALESCE(
    NULLIF(BTRIM(section_code), ''),
    'legacy_section_' || section_id::TEXT
)
WHERE section_instance_id IS NULL OR BTRIM(section_instance_id) = '';

ALTER TABLE sections
ALTER COLUMN section_instance_id SET NOT NULL;

-- Repeated canonical codes are valid when they are separate, noncontiguous
-- instances. The instance, not the canonical category, is the physical key.
ALTER TABLE sections
DROP CONSTRAINT IF EXISTS uq_sections_doc_code;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_sections_doc_instance'
    ) THEN
        ALTER TABLE sections
        ADD CONSTRAINT uq_sections_doc_instance
        UNIQUE (doc_id, section_instance_id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_sections_doc_code
ON sections(doc_id, section_code);

ALTER TABLE chunks
ADD COLUMN IF NOT EXISTS external_chunk_id VARCHAR(512);

ALTER TABLE chunks
ADD COLUMN IF NOT EXISTS section_instance_id VARCHAR(100);

ALTER TABLE chunks
ADD COLUMN IF NOT EXISTS source_id VARCHAR(255);

ALTER TABLE chunks
ADD COLUMN IF NOT EXISTS source_version_id VARCHAR(320);

ALTER TABLE chunks
ADD COLUMN IF NOT EXISTS citation_validation_status VARCHAR(40);

ALTER TABLE chunks
ADD COLUMN IF NOT EXISTS citation_validation_version VARCHAR(50);

-- Backfill the denormalized section identity without inventing external chunk
-- or source IDs that did not exist in the legacy indexes.
UPDATE chunks AS chunk
SET section_instance_id = section.section_instance_id
FROM sections AS section
WHERE chunk.section_id = section.section_id
  AND (
      chunk.section_instance_id IS NULL
      OR BTRIM(chunk.section_instance_id) = ''
  );

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_chunks_external_chunk_id'
    ) THEN
        ALTER TABLE chunks
        ADD CONSTRAINT uq_chunks_external_chunk_id UNIQUE (external_chunk_id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_chunks_section_instance_id
ON chunks(section_instance_id);

CREATE INDEX IF NOT EXISTS idx_chunks_source_id
ON chunks(source_id);

CREATE INDEX IF NOT EXISTS idx_chunks_source_version_id
ON chunks(source_version_id);

CREATE INDEX IF NOT EXISTS idx_chunks_citation_validation_status
ON chunks(citation_validation_status);
