BEGIN;

CREATE TABLE IF NOT EXISTS annual_history_datasets (
    dataset_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('building','validated','failed','retired')),
    manifest_sha256 CHAR(64) NOT NULL,
    parser_version TEXT NOT NULL,
    splitter_version TEXT NOT NULL,
    chunker_version TEXT NOT NULL,
    chunker_config_sha256 CHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    validated_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS annual_history_batches (
    dataset_id TEXT NOT NULL REFERENCES annual_history_datasets(dataset_id) ON DELETE CASCADE,
    batch_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('loading','committed','failed')),
    manifest_sha256 CHAR(64) NOT NULL,
    filing_count INTEGER NOT NULL CHECK (filing_count > 0),
    document_count INTEGER,
    section_count INTEGER,
    chunk_count INTEGER,
    committed_at TIMESTAMPTZ,
    PRIMARY KEY (dataset_id, batch_id)
);

CREATE TABLE IF NOT EXISTS annual_history_filings (
    filing_pk BIGSERIAL PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES annual_history_datasets(dataset_id) ON DELETE CASCADE,
    batch_id TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    ticker VARCHAR(20) NOT NULL,
    cik VARCHAR(20) NOT NULL,
    coverage_year SMALLINT NOT NULL CHECK (coverage_year BETWEEN 2015 AND 2025),
    filing_year SMALLINT NOT NULL CHECK (filing_year BETWEEN 1990 AND 2100),
    filing_date DATE NOT NULL,
    accession_number VARCHAR(50) NOT NULL,
    report_date DATE,
    dei_fiscal_year_focus SMALLINT,
    form_type TEXT NOT NULL CHECK (form_type = '10-K'),
    source_file TEXT NOT NULL,
    source_sha256 CHAR(64) NOT NULL,
    fiscal_year_source TEXT NOT NULL,
    resolution_confidence TEXT NOT NULL CHECK (resolution_confidence IN ('high','medium')),
    resolution_evidence JSONB,
    UNIQUE (dataset_id, accession_number),
    UNIQUE (dataset_id, ticker, coverage_year),
    FOREIGN KEY (dataset_id, batch_id)
      REFERENCES annual_history_batches(dataset_id, batch_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS annual_history_documents (
    document_pk BIGSERIAL PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES annual_history_datasets(dataset_id) ON DELETE CASCADE,
    filing_pk BIGINT NOT NULL REFERENCES annual_history_filings(filing_pk) ON DELETE CASCADE,
    accession_number VARCHAR(50) NOT NULL,
    company_id INTEGER NOT NULL,
    ticker VARCHAR(20) NOT NULL,
    coverage_year SMALLINT NOT NULL CHECK (coverage_year BETWEEN 2015 AND 2025),
    source_sha256 CHAR(64) NOT NULL,
    text_sha256 CHAR(64) NOT NULL,
    parser_version TEXT NOT NULL,
    parser_config_sha256 CHAR(64) NOT NULL,
    parse_status TEXT NOT NULL CHECK (parse_status = 'passed'),
    char_count BIGINT NOT NULL CHECK (char_count > 0),
    semantic_table_count INTEGER NOT NULL CHECK (semantic_table_count >= 0),
    layout_table_count INTEGER NOT NULL CHECK (layout_table_count >= 0),
    quality_flags JSONB NOT NULL DEFAULT '[]'::jsonb,
    UNIQUE (dataset_id, accession_number)
);

CREATE TABLE IF NOT EXISTS annual_history_sections (
    section_pk BIGSERIAL PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES annual_history_datasets(dataset_id) ON DELETE CASCADE,
    document_pk BIGINT NOT NULL REFERENCES annual_history_documents(document_pk) ON DELETE CASCADE,
    source_section_id TEXT NOT NULL,
    accession_number VARCHAR(50) NOT NULL,
    company_id INTEGER NOT NULL,
    ticker VARCHAR(20) NOT NULL,
    coverage_year SMALLINT NOT NULL CHECK (coverage_year BETWEEN 2015 AND 2025),
    canonical_section_code TEXT NOT NULL,
    section_heading TEXT,
    subsection_heading TEXT,
    section_text TEXT NOT NULL,
    source_start_char INTEGER NOT NULL CHECK (source_start_char >= 0),
    source_end_char INTEGER NOT NULL CHECK (source_end_char > source_start_char),
    source_text_sha256 CHAR(64) NOT NULL,
    section_text_sha256 CHAR(64) NOT NULL,
    splitter_version TEXT NOT NULL,
    splitter_config_sha256 CHAR(64) NOT NULL,
    boundary_method TEXT NOT NULL,
    boundary_confidence TEXT NOT NULL CHECK (boundary_confidence IN ('high','low')),
    quality_status TEXT NOT NULL
      CHECK (quality_status IN ('passed','review_required')),
    quality_flags JSONB NOT NULL DEFAULT '[]'::jsonb,
    rag_action TEXT NOT NULL
      CHECK (rag_action IN ('include','exclude','review_required')),
    CHECK (
      (quality_status = 'passed'
       AND rag_action IN ('include','exclude'))
      OR
      (quality_status = 'review_required'
       AND rag_action IN ('exclude','review_required'))
    ),
    UNIQUE (dataset_id, source_section_id)
);

CREATE TABLE IF NOT EXISTS annual_history_chunks (
    chunk_pk BIGSERIAL PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES annual_history_datasets(dataset_id) ON DELETE CASCADE,
    section_pk BIGINT NOT NULL REFERENCES annual_history_sections(section_pk) ON DELETE CASCADE,
    document_pk BIGINT NOT NULL REFERENCES annual_history_documents(document_pk) ON DELETE CASCADE,
    source_chunk_id TEXT NOT NULL,
    source_section_id TEXT NOT NULL,
    accession_number VARCHAR(50) NOT NULL,
    company_id INTEGER NOT NULL,
    ticker VARCHAR(20) NOT NULL,
    coverage_year SMALLINT NOT NULL CHECK (coverage_year BETWEEN 2015 AND 2025),
    chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
    canonical_section_code TEXT NOT NULL,
    rag_section_code TEXT NOT NULL,
    subsection_heading TEXT,
    chunk_type TEXT NOT NULL,
    chunk_text TEXT NOT NULL,
    embedding_text TEXT NOT NULL,
    token_count INTEGER NOT NULL CHECK (token_count > 0 AND token_count <= 400),
    embedding_token_count INTEGER NOT NULL CHECK (embedding_token_count > 0 AND embedding_token_count <= 512),
    source_start_char INTEGER NOT NULL,
    source_end_char INTEGER NOT NULL,
    section_start_char INTEGER NOT NULL,
    section_end_char INTEGER NOT NULL,
    chunk_text_sha256 CHAR(64) NOT NULL,
    embedding_text_sha256 CHAR(64) NOT NULL,
    chunker_version TEXT NOT NULL,
    chunker_config_sha256 CHAR(64) NOT NULL,
    boundary_start_type TEXT NOT NULL,
    boundary_end_type TEXT NOT NULL,
    continuation_from_previous BOOLEAN NOT NULL,
    continues_to_next BOOLEAN NOT NULL,
    quality_status TEXT NOT NULL CHECK (quality_status IN ('passed','failed')),
    quality_flags JSONB NOT NULL DEFAULT '[]'::jsonb,
    rag_action TEXT NOT NULL CHECK (rag_action IN ('include','exclude')),
    UNIQUE (dataset_id, source_chunk_id),
    UNIQUE (dataset_id, section_pk, chunk_index)
);

CREATE INDEX IF NOT EXISTS annual_history_filings_year_idx
  ON annual_history_filings(dataset_id, coverage_year, ticker);
CREATE INDEX IF NOT EXISTS annual_history_chunks_filter_idx
  ON annual_history_chunks(dataset_id, ticker, coverage_year, rag_section_code, rag_action);

COMMIT;
