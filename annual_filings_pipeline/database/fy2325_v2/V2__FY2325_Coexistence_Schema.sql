BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS fy2325_v2_datasets (
    dataset_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('loading','validated','indexed','active','failed','retired')),
    manifest_sha256 CHAR(64) NOT NULL,
    chunker_version TEXT NOT NULL,
    chunker_config_sha256 CHAR(64) NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_revision TEXT NOT NULL,
    embedding_dimension INTEGER NOT NULL CHECK (embedding_dimension = 768),
    normalized BOOLEAN NOT NULL CHECK (normalized),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    validated_at TIMESTAMPTZ,
    activated_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS fy2325_v2_companies (
    dataset_id TEXT NOT NULL REFERENCES fy2325_v2_datasets(dataset_id) ON DELETE CASCADE,
    company_id INTEGER NOT NULL,
    ticker VARCHAR(20) NOT NULL,
    cik VARCHAR(20) NOT NULL,
    name TEXT NOT NULL,
    sector TEXT,
    exchange TEXT,
    ipo_date DATE,
    fiscal_year_end_month SMALLINT NOT NULL CHECK (fiscal_year_end_month BETWEEN 1 AND 12),
    fiscal_year_end_day SMALLINT NOT NULL CHECK (fiscal_year_end_day BETWEEN 1 AND 31),
    fiscal_year_end_source TEXT NOT NULL,
    fiscal_year_end_status TEXT NOT NULL CHECK (fiscal_year_end_status IN ('SOURCE_VERIFIED','MANUAL_VERIFIED','REVIEW_REQUIRED')),
    PRIMARY KEY (dataset_id, company_id),
    UNIQUE (dataset_id, ticker),
    UNIQUE (dataset_id, cik)
);

CREATE TABLE IF NOT EXISTS fy2325_v2_filings (
    filing_pk BIGSERIAL PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES fy2325_v2_datasets(dataset_id) ON DELETE CASCADE,
    company_id INTEGER NOT NULL,
    ticker VARCHAR(20) NOT NULL,
    cik VARCHAR(20) NOT NULL,
    coverage_year SMALLINT NOT NULL CHECK (coverage_year IN (2023,2024,2025)),
    filing_year SMALLINT NOT NULL CHECK (filing_year BETWEEN 1990 AND 2100),
    filing_date DATE NOT NULL,
    accession_number VARCHAR(50) NOT NULL,
    document_period_end_date DATE,
    document_fiscal_year_focus SMALLINT,
    form_type TEXT NOT NULL CHECK (form_type = '10-K'),
    is_amendment BOOLEAN NOT NULL CHECK (NOT is_amendment),
    source_file TEXT NOT NULL,
    source_sha256 CHAR(64) NOT NULL,
    selection_method TEXT NOT NULL,
    selection_status TEXT NOT NULL CHECK (selection_status = 'selected'),
    FOREIGN KEY (dataset_id, company_id)
      REFERENCES fy2325_v2_companies(dataset_id, company_id) ON DELETE RESTRICT,
    UNIQUE (dataset_id, accession_number),
    UNIQUE (dataset_id, ticker, coverage_year)
);

CREATE TABLE IF NOT EXISTS fy2325_v2_documents (
    document_pk BIGSERIAL PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES fy2325_v2_datasets(dataset_id) ON DELETE CASCADE,
    filing_pk BIGINT NOT NULL REFERENCES fy2325_v2_filings(filing_pk) ON DELETE CASCADE,
    accession_number VARCHAR(50) NOT NULL,
    company_id INTEGER NOT NULL,
    ticker VARCHAR(20) NOT NULL,
    coverage_year SMALLINT NOT NULL CHECK (coverage_year IN (2023,2024,2025)),
    output_file TEXT NOT NULL,
    source_file TEXT NOT NULL,
    source_sha256 CHAR(64) NOT NULL,
    text_sha256 CHAR(64) NOT NULL,
    parser_version TEXT NOT NULL CHECK (parser_version = 'fy2325-html-v2.2'),
    parser_config_sha256 CHAR(64) NOT NULL,
    parse_status TEXT NOT NULL CHECK (parse_status = 'passed'),
    char_count BIGINT NOT NULL CHECK (char_count > 0),
    semantic_table_count INTEGER NOT NULL CHECK (semantic_table_count >= 0),
    layout_table_count INTEGER NOT NULL CHECK (layout_table_count >= 0),
    quality_flags JSONB NOT NULL DEFAULT '[]'::jsonb,
    UNIQUE (dataset_id, accession_number),
    UNIQUE (dataset_id, filing_pk)
);

CREATE TABLE IF NOT EXISTS fy2325_v2_sections (
    section_pk BIGSERIAL PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES fy2325_v2_datasets(dataset_id) ON DELETE CASCADE,
    document_pk BIGINT NOT NULL REFERENCES fy2325_v2_documents(document_pk) ON DELETE CASCADE,
    source_section_id TEXT NOT NULL,
    accession_number VARCHAR(50) NOT NULL,
    company_id INTEGER NOT NULL,
    ticker VARCHAR(20) NOT NULL,
    coverage_year SMALLINT NOT NULL CHECK (coverage_year IN (2023,2024,2025)),
    canonical_section_code TEXT NOT NULL,
    section_heading TEXT,
    subsection_heading TEXT,
    section_text TEXT NOT NULL,
    output_file TEXT NOT NULL,
    source_start_char INTEGER NOT NULL CHECK (source_start_char >= 0),
    source_end_char INTEGER NOT NULL CHECK (source_end_char > source_start_char),
    source_text_sha256 CHAR(64) NOT NULL,
    section_text_sha256 CHAR(64) NOT NULL,
    splitter_version TEXT NOT NULL CHECK (splitter_version = 'fy2325-section-v2.7'),
    splitter_config_sha256 CHAR(64) NOT NULL,
    boundary_method TEXT NOT NULL,
    boundary_confidence TEXT NOT NULL CHECK (boundary_confidence IN ('high','low')),
    quality_status TEXT NOT NULL CHECK (quality_status = 'passed'),
    quality_flags JSONB NOT NULL DEFAULT '[]'::jsonb,
    rag_action TEXT NOT NULL CHECK (rag_action IN ('include','exclude')),
    UNIQUE (dataset_id, source_section_id)
);

CREATE TABLE IF NOT EXISTS fy2325_v2_chunks (
    chunk_pk BIGSERIAL PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES fy2325_v2_datasets(dataset_id) ON DELETE CASCADE,
    section_pk BIGINT NOT NULL REFERENCES fy2325_v2_sections(section_pk) ON DELETE CASCADE,
    document_pk BIGINT NOT NULL REFERENCES fy2325_v2_documents(document_pk) ON DELETE CASCADE,
    source_chunk_id TEXT NOT NULL,
    source_section_id TEXT NOT NULL,
    accession_number VARCHAR(50) NOT NULL,
    company_id INTEGER NOT NULL,
    ticker VARCHAR(20) NOT NULL,
    coverage_year SMALLINT NOT NULL CHECK (coverage_year IN (2023,2024,2025)),
    chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
    canonical_section_code TEXT NOT NULL,
    rag_section_code TEXT NOT NULL,
    subsection_heading TEXT,
    chunk_type TEXT NOT NULL CHECK (chunk_type IN ('narrative','table','table_continuation','list','mixed_approved')),
    chunk_text TEXT NOT NULL,
    embedding_text TEXT NOT NULL,
    token_count INTEGER NOT NULL CHECK (token_count > 0 AND token_count <= 400),
    embedding_token_count INTEGER NOT NULL CHECK (embedding_token_count > 0 AND embedding_token_count <= 512),
    embedding_max_tokens INTEGER NOT NULL CHECK (embedding_max_tokens = 512),
    source_start_char INTEGER NOT NULL CHECK (source_start_char >= 0),
    source_end_char INTEGER NOT NULL CHECK (source_end_char > source_start_char),
    section_start_char INTEGER NOT NULL CHECK (section_start_char >= 0),
    section_end_char INTEGER NOT NULL CHECK (section_end_char > section_start_char),
    chunk_text_sha256 CHAR(64) NOT NULL,
    embedding_text_sha256 CHAR(64) NOT NULL,
    chunker_version TEXT NOT NULL CHECK (chunker_version = 'fy2325-chunker-v2.16'),
    chunker_config_sha256 CHAR(64) NOT NULL,
    policy_postprocess_version TEXT,
    policy_postprocess_sha256 CHAR(64),
    boundary_start_type TEXT NOT NULL,
    boundary_end_type TEXT NOT NULL,
    semantic_topic_count SMALLINT CHECK (semantic_topic_count IS NULL OR semantic_topic_count > 0),
    continuation_from_previous BOOLEAN NOT NULL,
    continues_to_next BOOLEAN NOT NULL,
    quality_status TEXT NOT NULL CHECK (quality_status IN ('passed','failed')),
    quality_flags JSONB NOT NULL DEFAULT '[]'::jsonb,
    rag_action TEXT NOT NULL CHECK (rag_action IN ('include','exclude')),
    embedding_model TEXT NOT NULL,
    embedding_model_revision TEXT NOT NULL,
    UNIQUE (dataset_id, source_chunk_id),
    UNIQUE (dataset_id, section_pk, chunk_index),
    CHECK ((rag_action = 'include' AND quality_status = 'passed') OR
           (rag_action = 'exclude' AND quality_status = 'failed'))
);

CREATE TABLE IF NOT EXISTS fy2325_v2_embeddings (
    dataset_id TEXT NOT NULL REFERENCES fy2325_v2_datasets(dataset_id) ON DELETE CASCADE,
    chunk_pk BIGINT NOT NULL REFERENCES fy2325_v2_chunks(chunk_pk) ON DELETE CASCADE,
    source_chunk_id TEXT NOT NULL,
    embedding vector(768) NOT NULL,
    embedding_text_sha256 CHAR(64) NOT NULL,
    chunk_text_sha256 CHAR(64) NOT NULL,
    model_name TEXT NOT NULL CHECK (model_name = 'BAAI/bge-base-en-v1.5'),
    model_revision TEXT NOT NULL CHECK (model_revision = 'a5beb1e3e68b9ab74eb54cfd186867f64f240e1a'),
    dimension INTEGER NOT NULL CHECK (dimension = 768),
    normalized BOOLEAN NOT NULL CHECK (normalized),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (dataset_id, chunk_pk),
    UNIQUE (dataset_id, source_chunk_id)
);

CREATE INDEX IF NOT EXISTS fy2325_v2_filings_coverage_idx
  ON fy2325_v2_filings(dataset_id, coverage_year);
CREATE INDEX IF NOT EXISTS fy2325_v2_chunks_filter_idx
  ON fy2325_v2_chunks(dataset_id, ticker, coverage_year, rag_section_code, rag_action);

CREATE OR REPLACE VIEW rag_eligible_10k_chunks_v216_staging AS
SELECT
    ch.chunk_pk AS chunk_id,
    ch.source_chunk_id,
    ch.section_pk AS section_id,
    ch.document_pk AS doc_id,
    f.filing_pk AS filing_id,
    ch.company_id,
    ch.ticker,
    co.cik,
    ch.coverage_year AS filing_year,
    ch.coverage_year,
    ch.accession_number,
    f.filing_date,
    '10-K'::text AS doc_type,
    ch.rag_section_code AS section_code,
    ch.chunk_index,
    ch.chunk_text,
    ch.embedding_text,
    ch.token_count,
    ch.quality_status AS doc_quality_status,
    ch.rag_action,
    TRUE AS citation_ready,
    ch.quality_flags,
    ch.chunk_text_sha256,
    ch.embedding_text_sha256,
    ch.dataset_id
FROM fy2325_v2_chunks ch
JOIN fy2325_v2_embeddings e
  ON e.dataset_id = ch.dataset_id AND e.chunk_pk = ch.chunk_pk
JOIN fy2325_v2_documents d ON d.document_pk = ch.document_pk
JOIN fy2325_v2_filings f ON f.filing_pk = d.filing_pk
JOIN fy2325_v2_companies co
  ON co.dataset_id = ch.dataset_id AND co.company_id = ch.company_id
JOIN fy2325_v2_datasets ds ON ds.dataset_id = ch.dataset_id
WHERE ch.rag_action = 'include'
  AND ch.quality_status = 'passed'
  AND ds.status IN ('validated','indexed','active');

CREATE OR REPLACE VIEW benchmark_embeddings_bge_base_en_v15_v216 AS
SELECT
    chunk_pk AS chunk_id,
    embedding,
    model_name AS model_repo_id,
    model_revision AS resolved_revision,
    dimension,
    normalized,
    embedding_text_sha256,
    created_at AS embedded_at
FROM fy2325_v2_embeddings
WHERE dataset_id = 'fy2325-v2.16';

COMMIT;
