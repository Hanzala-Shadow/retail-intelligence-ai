-- V10__Enable_Pgvector_Extension.sql
-- Enables pgvector in retail_pipeline.
--
-- This migration installs only the PostgreSQL extension objects.
-- It intentionally does not create an embeddings table, vector column,
-- or vector index because the final vector dimension depends on the
-- embedding model selected during the Week 3 model-selection meeting.

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;

COMMENT ON EXTENSION vector IS
    'Vector similarity support for the Retail Intelligence RAG pipeline';

COMMIT;
