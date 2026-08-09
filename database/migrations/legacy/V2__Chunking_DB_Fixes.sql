-- V2__Chunking_DB_Fixes.sql
-- Adds missing fields/constraints needed for safe chunking + DB loading.

-- Drive tracking fields
ALTER TABLE annual_filings
ADD COLUMN IF NOT EXISTS drive_file_id TEXT;

ALTER TABLE sustainability_reports
ADD COLUMN IF NOT EXISTS drive_file_id TEXT;

-- Denormalized chunk metadata for easier validation / Phase 2 retrieval
ALTER TABLE chunks
ADD COLUMN IF NOT EXISTS doc_type VARCHAR(30);

ALTER TABLE chunks
ADD COLUMN IF NOT EXISTS section_code VARCHAR(50);

-- Unique constraints needed for idempotent reloads and FK mapping
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_documents_filepath'
    ) THEN
        ALTER TABLE documents
        ADD CONSTRAINT uq_documents_filepath UNIQUE (filepath);
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_sections_doc_code'
    ) THEN
        ALTER TABLE sections
        ADD CONSTRAINT uq_sections_doc_code UNIQUE (doc_id, section_code);
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_chunks_section_index'
    ) THEN
        ALTER TABLE chunks
        ADD CONSTRAINT uq_chunks_section_index UNIQUE (section_id, chunk_index);
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_sustainability_company_year'
    ) THEN
        ALTER TABLE sustainability_reports
        ADD CONSTRAINT uq_sustainability_company_year UNIQUE (company_id, year);
    END IF;
END $$;

-- Helpful indexes
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_chunks_doc_type ON chunks(doc_type);
CREATE INDEX IF NOT EXISTS idx_chunks_section_code ON chunks(section_code);
