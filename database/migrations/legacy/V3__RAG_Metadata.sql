-- V3__RAG_Metadata.sql
-- Adds document quality and citation metadata needed for Phase 3 RAG filtering/evaluation.

ALTER TABLE documents
ADD COLUMN IF NOT EXISTS quality_flags TEXT;

ALTER TABLE documents
ADD COLUMN IF NOT EXISTS possible_wrong_doc_type BOOLEAN DEFAULT FALSE;

ALTER TABLE documents
ADD COLUMN IF NOT EXISTS doc_quality_status VARCHAR(40);

ALTER TABLE documents
ADD COLUMN IF NOT EXISTS rag_action VARCHAR(50);

ALTER TABLE sections
ADD COLUMN IF NOT EXISTS source_start_char INTEGER;

ALTER TABLE sections
ADD COLUMN IF NOT EXISTS source_end_char INTEGER;

ALTER TABLE sections
ADD COLUMN IF NOT EXISTS page_start INTEGER;

ALTER TABLE sections
ADD COLUMN IF NOT EXISTS page_end INTEGER;

ALTER TABLE chunks
ADD COLUMN IF NOT EXISTS doc_quality_status VARCHAR(40);

ALTER TABLE chunks
ADD COLUMN IF NOT EXISTS rag_action VARCHAR(50);

ALTER TABLE chunks
ADD COLUMN IF NOT EXISTS quality_flags TEXT;

ALTER TABLE chunks
ADD COLUMN IF NOT EXISTS source_start_char INTEGER;

ALTER TABLE chunks
ADD COLUMN IF NOT EXISTS source_end_char INTEGER;

ALTER TABLE chunks
ADD COLUMN IF NOT EXISTS page_start INTEGER;

ALTER TABLE chunks
ADD COLUMN IF NOT EXISTS page_end INTEGER;

ALTER TABLE chunks
ADD COLUMN IF NOT EXISTS citation_ready BOOLEAN DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_documents_doc_quality_status ON documents(doc_quality_status);
CREATE INDEX IF NOT EXISTS idx_documents_rag_action ON documents(rag_action);
CREATE INDEX IF NOT EXISTS idx_chunks_rag_action ON chunks(rag_action);
CREATE INDEX IF NOT EXISTS idx_chunks_citation_ready ON chunks(citation_ready);
CREATE INDEX IF NOT EXISTS idx_chunks_page_range ON chunks(page_start, page_end);
