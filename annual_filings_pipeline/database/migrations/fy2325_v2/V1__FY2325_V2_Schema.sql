BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE pipeline_runs (
    run_id UUID PRIMARY KEY,
    pipeline_stage TEXT NOT NULL,
    code_commit TEXT NOT NULL,
    git_branch TEXT NOT NULL,
    config_sha256 CHAR(64) NOT NULL,
    input_manifest_sha256 CHAR(64) NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL CHECK (status IN ('running', 'passed', 'failed', 'aborted')),
    row_count BIGINT,
    failure_count BIGINT,
    output_manifest_sha256 CHAR(64),
    notes TEXT
);

CREATE TABLE companies (
    company_id INTEGER PRIMARY KEY,
    ticker VARCHAR(20) NOT NULL UNIQUE,
    cik VARCHAR(20) NOT NULL UNIQUE,
    name VARCHAR(255) NOT NULL,
    sector VARCHAR(100),
    exchange VARCHAR(100),
    ipo_date DATE,
    fiscal_year_end_month SMALLINT NOT NULL CHECK (fiscal_year_end_month BETWEEN 1 AND 12),
    fiscal_year_end_day SMALLINT NOT NULL CHECK (fiscal_year_end_day BETWEEN 1 AND 31),
    fiscal_year_end_source TEXT NOT NULL,
    fiscal_year_end_status TEXT NOT NULL CHECK (
        fiscal_year_end_status IN ('SOURCE_VERIFIED', 'MANUAL_VERIFIED', 'REVIEW_REQUIRED')
    ),
    company_scope_status TEXT NOT NULL DEFAULT 'included' CHECK (
        company_scope_status IN ('included', 'excluded', 'review_required')
    ),
    company_scope_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        fiscal_year_end_day <= CASE fiscal_year_end_month
            WHEN 2 THEN 29
            WHEN 4 THEN 30
            WHEN 6 THEN 30
            WHEN 9 THEN 30
            WHEN 11 THEN 30
            ELSE 31
        END
    )
);

CREATE TABLE annual_filings (
    filing_id BIGSERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE RESTRICT,
    filing_year SMALLINT NOT NULL CHECK (filing_year BETWEEN 1990 AND 2100),
    coverage_year SMALLINT CHECK (coverage_year IN (2023, 2024, 2025)),
    accession_number VARCHAR(50) NOT NULL UNIQUE,
    filing_date DATE NOT NULL,
    document_period_end_date DATE,
    document_fiscal_year_focus SMALLINT,
    coverage_resolution_method TEXT,
    coverage_resolution_status TEXT NOT NULL CHECK (
        coverage_resolution_status IN ('resolved', 'review_required', 'rejected')
    ),
    source_url TEXT,
    source_file TEXT NOT NULL,
    source_sha256 CHAR(64) NOT NULL,
    form_type TEXT NOT NULL DEFAULT '10-K',
    is_amendment BOOLEAN NOT NULL DEFAULT FALSE,
    is_selected BOOLEAN NOT NULL DEFAULT FALSE,
    selection_reason TEXT,
    selection_manifest_sha256 CHAR(64),
    ingestion_run_id UUID NOT NULL REFERENCES pipeline_runs(run_id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        NOT is_selected OR (
            coverage_resolution_status = 'resolved'
            AND coverage_year IN (2023, 2024, 2025)
            AND is_amendment IS FALSE
            AND form_type = '10-K'
        )
    )
);

CREATE UNIQUE INDEX uq_selected_company_coverage_year
    ON annual_filings(company_id, coverage_year)
    WHERE is_selected IS TRUE;

CREATE TABLE documents (
    doc_id BIGSERIAL PRIMARY KEY,
    filing_id BIGINT NOT NULL UNIQUE REFERENCES annual_filings(filing_id) ON DELETE CASCADE,
    company_id INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE RESTRICT,
    coverage_year SMALLINT NOT NULL CHECK (coverage_year IN (2023, 2024, 2025)),
    doc_type TEXT NOT NULL CHECK (doc_type = '10-K'),
    filepath TEXT NOT NULL UNIQUE,
    source_sha256 CHAR(64) NOT NULL,
    parser_version TEXT NOT NULL,
    parser_config_sha256 CHAR(64) NOT NULL,
    text_sha256 CHAR(64) NOT NULL,
    parse_status TEXT NOT NULL CHECK (parse_status IN ('passed', 'review_required', 'failed')),
    parse_quality_flags JSONB NOT NULL DEFAULT '[]'::jsonb,
    rag_action TEXT NOT NULL CHECK (rag_action IN ('include', 'exclude', 'review_required')),
    ingestion_run_id UUID NOT NULL REFERENCES pipeline_runs(run_id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE sections (
    section_id BIGSERIAL PRIMARY KEY,
    doc_id BIGINT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    coverage_year SMALLINT NOT NULL CHECK (coverage_year IN (2023, 2024, 2025)),
    section_instance_id TEXT NOT NULL,
    canonical_section_code TEXT NOT NULL,
    section_heading TEXT,
    subsection_heading TEXT,
    section_text TEXT NOT NULL,
    source_start_char INTEGER NOT NULL CHECK (source_start_char >= 0),
    source_end_char INTEGER NOT NULL,
    section_text_sha256 CHAR(64) NOT NULL,
    splitter_version TEXT NOT NULL,
    splitter_config_sha256 CHAR(64) NOT NULL,
    boundary_method TEXT NOT NULL,
    boundary_confidence TEXT NOT NULL CHECK (
        boundary_confidence IN ('high', 'medium', 'low', 'unresolved')
    ),
    quality_status TEXT NOT NULL CHECK (quality_status IN ('passed', 'review_required', 'failed')),
    quality_flags JSONB NOT NULL DEFAULT '[]'::jsonb,
    rag_action TEXT NOT NULL CHECK (rag_action IN ('include', 'exclude', 'review_required')),
    ingestion_run_id UUID NOT NULL REFERENCES pipeline_runs(run_id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (doc_id, section_instance_id),
    CHECK (source_end_char > source_start_char)
);

CREATE TABLE chunks (
    chunk_id BIGSERIAL PRIMARY KEY,
    external_chunk_id TEXT NOT NULL UNIQUE,
    section_id BIGINT NOT NULL REFERENCES sections(section_id) ON DELETE CASCADE,
    doc_id BIGINT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    company_id INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE RESTRICT,
    coverage_year SMALLINT NOT NULL CHECK (coverage_year IN (2023, 2024, 2025)),
    chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
    canonical_section_code TEXT NOT NULL,
    subsection_heading TEXT,
    chunk_type TEXT NOT NULL CHECK (
        chunk_type IN ('narrative', 'table', 'table_continuation', 'list', 'mixed_approved')
    ),
    chunk_text TEXT NOT NULL,
    embedding_text TEXT NOT NULL,
    token_count INTEGER NOT NULL CHECK (token_count > 0),
    source_start_char INTEGER NOT NULL CHECK (source_start_char >= 0),
    source_end_char INTEGER NOT NULL,
    chunk_text_sha256 CHAR(64) NOT NULL,
    embedding_text_sha256 CHAR(64) NOT NULL,
    chunker_version TEXT NOT NULL,
    chunker_config_sha256 CHAR(64) NOT NULL,
    boundary_start_type TEXT NOT NULL,
    boundary_end_type TEXT NOT NULL,
    semantic_topic_count SMALLINT CHECK (semantic_topic_count IS NULL OR semantic_topic_count > 0),
    quality_status TEXT NOT NULL CHECK (quality_status IN ('passed', 'review_required', 'failed')),
    quality_flags JSONB NOT NULL DEFAULT '[]'::jsonb,
    rag_action TEXT NOT NULL CHECK (rag_action IN ('include', 'exclude', 'review_required')),
    ingestion_run_id UUID NOT NULL REFERENCES pipeline_runs(run_id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (section_id, chunk_index),
    CHECK (source_end_char > source_start_char)
);

CREATE TABLE chunk_embeddings (
    chunk_id BIGINT PRIMARY KEY REFERENCES chunks(chunk_id) ON DELETE CASCADE,
    embedding vector(768) NOT NULL,
    model_name TEXT NOT NULL,
    model_revision TEXT NOT NULL,
    embedding_text_sha256 CHAR(64) NOT NULL,
    normalized BOOLEAN NOT NULL,
    ingestion_run_id UUID NOT NULL REFERENCES pipeline_runs(run_id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE VIEW rag_eligible_10k_chunks_v2 AS
SELECT
    c.chunk_id,
    c.external_chunk_id,
    c.company_id,
    co.ticker,
    co.name AS company_name,
    c.coverage_year,
    c.canonical_section_code,
    c.subsection_heading,
    c.chunk_type,
    c.chunk_text,
    c.embedding_text,
    c.token_count,
    c.chunk_text_sha256,
    c.embedding_text_sha256
FROM chunks c
JOIN companies co ON co.company_id = c.company_id
JOIN sections s ON s.section_id = c.section_id
JOIN documents d ON d.doc_id = c.doc_id
JOIN annual_filings f ON f.filing_id = d.filing_id
JOIN pipeline_runs pr ON pr.run_id = c.ingestion_run_id
WHERE co.company_scope_status = 'included'
  AND co.fiscal_year_end_status <> 'REVIEW_REQUIRED'
  AND f.is_selected IS TRUE
  AND f.coverage_resolution_status = 'resolved'
  AND d.parse_status = 'passed'
  AND d.rag_action = 'include'
  AND s.quality_status = 'passed'
  AND s.rag_action = 'include'
  AND c.quality_status = 'passed'
  AND c.rag_action = 'include'
  AND pr.status = 'passed';

COMMIT;
