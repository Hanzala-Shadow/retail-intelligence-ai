-- V6__Source_Identity_And_Lineage.sql
-- Adds durable report, version, alias, and extraction identities.
--
-- Safety rules:
--   * Existing documents, sections, and chunks are never deleted.
--   * Legacy chunk source strings are retained in legacy_* columns.
--   * Every backfill ID is deterministic, so rerunning this SQL adds no rows.
--   * New ESG loads use the ls_/sv_/fa_/ea_ IDs from the intake catalog.
--   * Legacy 10-K loaders may keep using documents.filepath during rollout.

CREATE TABLE IF NOT EXISTS logical_sources (
    logical_source_id VARCHAR(320) PRIMARY KEY,
    company_id INTEGER REFERENCES companies(company_id) ON DELETE RESTRICT,
    policy_source_id VARCHAR(320),
    source_type VARCHAR(80) NOT NULL DEFAULT 'unknown',
    report_year INTEGER,
    title TEXT,
    lifecycle_state VARCHAR(20) NOT NULL DEFAULT 'active',
    superseded_by_logical_source_id VARCHAR(320),
    ownership_review_required BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_logical_sources_lifecycle_state
        CHECK (lifecycle_state IN ('active', 'superseded')),
    CONSTRAINT fk_logical_sources_superseded_by
        FOREIGN KEY (superseded_by_logical_source_id)
        REFERENCES logical_sources(logical_source_id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_logical_sources_policy_source_id
ON logical_sources(policy_source_id)
WHERE policy_source_id IS NOT NULL AND BTRIM(policy_source_id) <> '';

CREATE INDEX IF NOT EXISTS idx_logical_sources_company
ON logical_sources(company_id);

CREATE INDEX IF NOT EXISTS idx_logical_sources_lifecycle_state
ON logical_sources(lifecycle_state);

CREATE TABLE IF NOT EXISTS source_versions (
    source_version_id VARCHAR(320) PRIMARY KEY,
    logical_source_id VARCHAR(320) NOT NULL,
    original_sha256 CHAR(64),
    legacy_source_version_id VARCHAR(320),
    byte_size BIGINT,
    media_type VARCHAR(100),
    lifecycle_state VARCHAR(20) NOT NULL DEFAULT 'active',
    superseded_by_source_version_id VARCHAR(320),
    ownership_review_required BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_source_versions_logical_source
        FOREIGN KEY (logical_source_id)
        REFERENCES logical_sources(logical_source_id) ON DELETE RESTRICT,
    CONSTRAINT fk_source_versions_superseded_by
        FOREIGN KEY (superseded_by_source_version_id)
        REFERENCES source_versions(source_version_id) ON DELETE RESTRICT,
    CONSTRAINT ck_source_versions_lifecycle_state
        CHECK (lifecycle_state IN ('active', 'superseded')),
    CONSTRAINT ck_source_versions_original_sha256
        CHECK (
            original_sha256 IS NULL
            OR original_sha256 ~ '^[0-9a-f]{64}$'
        ),
    CONSTRAINT uq_source_versions_logical_version
        UNIQUE (logical_source_id, source_version_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_source_versions_original_sha256
ON source_versions(original_sha256)
WHERE original_sha256 IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_source_versions_legacy_id
ON source_versions(legacy_source_version_id)
WHERE legacy_source_version_id IS NOT NULL
  AND BTRIM(legacy_source_version_id) <> '';

CREATE INDEX IF NOT EXISTS idx_source_versions_logical_source
ON source_versions(logical_source_id);

CREATE INDEX IF NOT EXISTS idx_source_versions_lifecycle_state
ON source_versions(lifecycle_state);

CREATE TABLE IF NOT EXISTS extraction_artifacts (
    extraction_artifact_id VARCHAR(320) PRIMARY KEY,
    source_version_id VARCHAR(320) NOT NULL,
    artifact_role VARCHAR(40) NOT NULL,
    artifact_sha256 CHAR(64),
    storage_path TEXT,
    drive_file_id TEXT,
    parser_or_model VARCHAR(160),
    prompt_version VARCHAR(100),
    source_page_sha256 CHAR(64),
    verification_state VARCHAR(40) NOT NULL DEFAULT 'unverified',
    lifecycle_state VARCHAR(20) NOT NULL DEFAULT 'active',
    superseded_by_extraction_artifact_id VARCHAR(320),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_extraction_artifacts_source_version
        FOREIGN KEY (source_version_id)
        REFERENCES source_versions(source_version_id) ON DELETE RESTRICT,
    CONSTRAINT fk_extraction_artifacts_superseded_by
        FOREIGN KEY (superseded_by_extraction_artifact_id)
        REFERENCES extraction_artifacts(extraction_artifact_id) ON DELETE RESTRICT,
    CONSTRAINT ck_extraction_artifacts_role
        CHECK (artifact_role IN (
            'original', 'ocr_derivative', 'page_ocr_override',
            'vlm_derivative', 'parsed_text', 'legacy_original'
        )),
    CONSTRAINT ck_extraction_artifacts_lifecycle_state
        CHECK (lifecycle_state IN ('active', 'superseded')),
    CONSTRAINT ck_extraction_artifacts_sha256
        CHECK (
            artifact_sha256 IS NULL
            OR artifact_sha256 ~ '^[0-9a-f]{64}$'
        ),
    CONSTRAINT uq_extraction_artifacts_version_artifact
        UNIQUE (source_version_id, extraction_artifact_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_extraction_artifacts_version_role_hash
ON extraction_artifacts(source_version_id, artifact_role, artifact_sha256)
WHERE artifact_sha256 IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_extraction_artifacts_source_version
ON extraction_artifacts(source_version_id);

CREATE INDEX IF NOT EXISTS idx_extraction_artifacts_lifecycle_state
ON extraction_artifacts(lifecycle_state);

CREATE TABLE IF NOT EXISTS file_aliases (
    file_alias_id VARCHAR(320) PRIMARY KEY,
    source_version_id VARCHAR(320) NOT NULL,
    extraction_artifact_id VARCHAR(320),
    observed_company_id INTEGER REFERENCES companies(company_id) ON DELETE RESTRICT,
    file_path TEXT,
    drive_file_id TEXT,
    observed_filename TEXT,
    lifecycle_state VARCHAR(20) NOT NULL DEFAULT 'active',
    first_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    superseded_by_file_alias_id VARCHAR(320),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_file_aliases_source_version
        FOREIGN KEY (source_version_id)
        REFERENCES source_versions(source_version_id) ON DELETE RESTRICT,
    CONSTRAINT fk_file_aliases_version_artifact
        FOREIGN KEY (source_version_id, extraction_artifact_id)
        REFERENCES extraction_artifacts(source_version_id, extraction_artifact_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_file_aliases_superseded_by
        FOREIGN KEY (superseded_by_file_alias_id)
        REFERENCES file_aliases(file_alias_id) ON DELETE RESTRICT,
    CONSTRAINT ck_file_aliases_location
        CHECK (file_path IS NOT NULL OR drive_file_id IS NOT NULL),
    CONSTRAINT ck_file_aliases_lifecycle_state
        CHECK (lifecycle_state IN ('active', 'superseded'))
);

CREATE INDEX IF NOT EXISTS idx_file_aliases_source_version
ON file_aliases(source_version_id);

CREATE INDEX IF NOT EXISTS idx_file_aliases_drive_file_id
ON file_aliases(drive_file_id);

CREATE INDEX IF NOT EXISTS idx_file_aliases_file_path
ON file_aliases(file_path);

CREATE UNIQUE INDEX IF NOT EXISTS uq_file_aliases_active_drive_file_id
ON file_aliases(drive_file_id)
WHERE lifecycle_state = 'active'
  AND drive_file_id IS NOT NULL
  AND BTRIM(drive_file_id) <> '';

CREATE UNIQUE INDEX IF NOT EXISTS uq_file_aliases_active_file_path
ON file_aliases(file_path)
WHERE lifecycle_state = 'active'
  AND file_path IS NOT NULL
  AND BTRIM(file_path) <> '';

CREATE TABLE IF NOT EXISTS source_approvals (
    source_approval_id BIGSERIAL PRIMARY KEY,
    logical_source_id VARCHAR(320) NOT NULL,
    source_version_id VARCHAR(320) NOT NULL,
    extraction_artifact_id VARCHAR(320) NOT NULL,
    approval_type VARCHAR(40) NOT NULL DEFAULT 'ocr_replacement',
    approval_status VARCHAR(20) NOT NULL,
    approved_source_sha256 CHAR(64) NOT NULL,
    approved_artifact_sha256 CHAR(64) NOT NULL,
    reviewer VARCHAR(255),
    approval_date TIMESTAMP,
    reason TEXT,
    lifecycle_state VARCHAR(20) NOT NULL DEFAULT 'active',
    superseded_by_source_approval_id BIGINT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_source_approvals_logical_version
        FOREIGN KEY (logical_source_id, source_version_id)
        REFERENCES source_versions(logical_source_id, source_version_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_source_approvals_version_artifact
        FOREIGN KEY (source_version_id, extraction_artifact_id)
        REFERENCES extraction_artifacts(source_version_id, extraction_artifact_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_source_approvals_superseded_by
        FOREIGN KEY (superseded_by_source_approval_id)
        REFERENCES source_approvals(source_approval_id) ON DELETE RESTRICT,
    CONSTRAINT ck_source_approvals_status
        CHECK (approval_status IN ('pending', 'approved', 'rejected')),
    CONSTRAINT ck_source_approvals_lifecycle_state
        CHECK (lifecycle_state IN ('active', 'superseded')),
    CONSTRAINT ck_source_approvals_source_sha256
        CHECK (approved_source_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_source_approvals_artifact_sha256
        CHECK (approved_artifact_sha256 ~ '^[0-9a-f]{64}$')
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_source_approvals_active_type
ON source_approvals(source_version_id, approval_type)
WHERE lifecycle_state = 'active';

CREATE INDEX IF NOT EXISTS idx_source_approvals_artifact
ON source_approvals(extraction_artifact_id);

-- Add lineage columns without removing filepath or the legacy source columns.
ALTER TABLE documents
ADD COLUMN IF NOT EXISTS logical_source_id VARCHAR(320);

ALTER TABLE documents
ADD COLUMN IF NOT EXISTS source_version_id VARCHAR(320);

ALTER TABLE documents
ADD COLUMN IF NOT EXISTS extraction_artifact_id VARCHAR(320);

ALTER TABLE documents
ADD COLUMN IF NOT EXISTS file_alias_id VARCHAR(320);

ALTER TABLE documents
ADD COLUMN IF NOT EXISTS lifecycle_state VARCHAR(20) NOT NULL DEFAULT 'active';

ALTER TABLE documents
ADD COLUMN IF NOT EXISTS superseded_by_doc_id INTEGER;

ALTER TABLE sections
ADD COLUMN IF NOT EXISTS logical_source_id VARCHAR(320);

ALTER TABLE sections
ADD COLUMN IF NOT EXISTS source_version_id VARCHAR(320);

ALTER TABLE sections
ADD COLUMN IF NOT EXISTS extraction_artifact_id VARCHAR(320);

ALTER TABLE sections
ADD COLUMN IF NOT EXISTS lifecycle_state VARCHAR(20) NOT NULL DEFAULT 'active';

ALTER TABLE sections
ADD COLUMN IF NOT EXISTS superseded_by_section_id INTEGER;

ALTER TABLE chunks
ADD COLUMN IF NOT EXISTS logical_source_id VARCHAR(320);

ALTER TABLE chunks
ADD COLUMN IF NOT EXISTS extraction_artifact_id VARCHAR(320);

ALTER TABLE chunks
ADD COLUMN IF NOT EXISTS legacy_source_version_id VARCHAR(320);

ALTER TABLE chunks
ADD COLUMN IF NOT EXISTS lifecycle_state VARCHAR(20) NOT NULL DEFAULT 'active';

ALTER TABLE chunks
ADD COLUMN IF NOT EXISTS superseded_by_chunk_id INTEGER;

-- Preserve the old free-form version value before source_version_id becomes a FK.
UPDATE chunks
SET legacy_source_version_id = NULLIF(BTRIM(source_version_id), '')
WHERE legacy_source_version_id IS NULL
  AND source_version_id IS NOT NULL
  AND BTRIM(source_version_id) <> '';

-- One legacy logical source per old policy source string.
INSERT INTO logical_sources (
    logical_source_id,
    company_id,
    policy_source_id,
    source_type,
    ownership_review_required
)
SELECT
    CASE
        WHEN BTRIM(chunk.source_id) LIKE 'ls\_%' ESCAPE '\'
            THEN BTRIM(chunk.source_id)
        ELSE 'legacy_ls_' || LEFT(MD5(BTRIM(chunk.source_id)), 24)
    END,
    CASE WHEN COUNT(DISTINCT chunk.company_id) = 1 THEN MIN(chunk.company_id) END,
    BTRIM(chunk.source_id),
    COALESCE(NULLIF(MIN(chunk.doc_type), ''), 'unknown'),
    COUNT(DISTINCT chunk.company_id) > 1
FROM chunks AS chunk
WHERE chunk.source_id IS NOT NULL
  AND BTRIM(chunk.source_id) <> ''
GROUP BY BTRIM(chunk.source_id)
ON CONFLICT DO NOTHING;

-- A conflicting old version string gets a review-only owner instead of being
-- silently assigned to one of several policy sources.
INSERT INTO logical_sources (
    logical_source_id,
    source_type,
    ownership_review_required,
    title
)
SELECT
    'legacy_ls_version_' || LEFT(MD5(BTRIM(chunk.legacy_source_version_id)), 24),
    'unknown',
    TRUE,
    'Review owner for legacy source version ' || BTRIM(chunk.legacy_source_version_id)
FROM chunks AS chunk
WHERE chunk.legacy_source_version_id IS NOT NULL
  AND BTRIM(chunk.legacy_source_version_id) <> ''
GROUP BY BTRIM(chunk.legacy_source_version_id)
HAVING COUNT(DISTINCT NULLIF(BTRIM(chunk.source_id), '')) <> 1
ON CONFLICT DO NOTHING;

-- Convert each old free-form version string to a deterministic legacy row.
INSERT INTO source_versions (
    source_version_id,
    logical_source_id,
    legacy_source_version_id,
    ownership_review_required
)
SELECT
    CASE
        WHEN version_map.old_version_id LIKE 'sv\_%' ESCAPE '\'
            THEN version_map.old_version_id
        ELSE 'legacy_sv_' || LEFT(MD5(version_map.old_version_id), 24)
    END,
    CASE
        WHEN version_map.source_count = 1 THEN
            CASE
                WHEN version_map.only_source_id LIKE 'ls\_%' ESCAPE '\'
                    THEN version_map.only_source_id
                ELSE 'legacy_ls_' || LEFT(MD5(version_map.only_source_id), 24)
            END
        ELSE
            'legacy_ls_version_' || LEFT(MD5(version_map.old_version_id), 24)
    END,
    version_map.old_version_id,
    version_map.source_count <> 1
FROM (
    SELECT
        BTRIM(chunk.legacy_source_version_id) AS old_version_id,
        COUNT(DISTINCT NULLIF(BTRIM(chunk.source_id), '')) AS source_count,
        MIN(NULLIF(BTRIM(chunk.source_id), '')) AS only_source_id
    FROM chunks AS chunk
    WHERE chunk.legacy_source_version_id IS NOT NULL
      AND BTRIM(chunk.legacy_source_version_id) <> ''
    GROUP BY BTRIM(chunk.legacy_source_version_id)
) AS version_map
ON CONFLICT DO NOTHING;

-- Give every document a logical source. Use the one unambiguous legacy
-- version when possible; otherwise keep the document under a reviewable
-- per-document legacy identity.
INSERT INTO logical_sources (
    logical_source_id,
    company_id,
    source_type,
    ownership_review_required,
    title
)
SELECT
    'legacy_ls_doc_' || document.doc_id::TEXT,
    document.company_id,
    document.doc_type,
    TRUE,
    'Legacy document ' || document.doc_id::TEXT
FROM documents AS document
LEFT JOIN chunks AS chunk ON chunk.doc_id = document.doc_id
GROUP BY document.doc_id, document.company_id, document.doc_type
HAVING COUNT(DISTINCT NULLIF(BTRIM(chunk.legacy_source_version_id), '')) <> 1
ON CONFLICT DO NOTHING;

WITH document_version AS (
    SELECT
        document.doc_id,
        COUNT(DISTINCT NULLIF(BTRIM(chunk.legacy_source_version_id), '')) AS version_count,
        MIN(NULLIF(BTRIM(chunk.legacy_source_version_id), '')) AS only_old_version
    FROM documents AS document
    LEFT JOIN chunks AS chunk ON chunk.doc_id = document.doc_id
    GROUP BY document.doc_id
)
UPDATE documents AS document
SET logical_source_id = CASE
    WHEN document_version.version_count = 1 THEN source_version.logical_source_id
    ELSE 'legacy_ls_doc_' || document.doc_id::TEXT
END
FROM document_version
LEFT JOIN source_versions AS source_version
  ON source_version.legacy_source_version_id = document_version.only_old_version
WHERE document.doc_id = document_version.doc_id
  AND document.logical_source_id IS NULL;

-- Documents without exactly one known version receive a stable fallback
-- version. This avoids guessing which historical chunk version is current.
INSERT INTO source_versions (
    source_version_id,
    logical_source_id,
    legacy_source_version_id,
    ownership_review_required
)
SELECT
    'legacy_sv_doc_' || document.doc_id::TEXT,
    document.logical_source_id,
    'legacy_document_' || document.doc_id::TEXT || '__version_1',
    TRUE
FROM documents AS document
LEFT JOIN chunks AS chunk ON chunk.doc_id = document.doc_id
GROUP BY document.doc_id, document.logical_source_id
HAVING COUNT(DISTINCT NULLIF(BTRIM(chunk.legacy_source_version_id), '')) <> 1
ON CONFLICT DO NOTHING;

WITH document_version AS (
    SELECT
        document.doc_id,
        COUNT(DISTINCT NULLIF(BTRIM(chunk.legacy_source_version_id), '')) AS version_count,
        MIN(NULLIF(BTRIM(chunk.legacy_source_version_id), '')) AS only_old_version
    FROM documents AS document
    LEFT JOIN chunks AS chunk ON chunk.doc_id = document.doc_id
    GROUP BY document.doc_id
)
UPDATE documents AS document
SET source_version_id = CASE
    WHEN document_version.version_count = 1 THEN source_version.source_version_id
    ELSE 'legacy_sv_doc_' || document.doc_id::TEXT
END
FROM document_version
LEFT JOIN source_versions AS source_version
  ON source_version.legacy_source_version_id = document_version.only_old_version
WHERE document.doc_id = document_version.doc_id
  AND document.source_version_id IS NULL;

-- One original placeholder artifact per backfilled version. Real intake rows
-- later add content-addressed ea_ IDs without deleting this history.
INSERT INTO extraction_artifacts (
    extraction_artifact_id,
    source_version_id,
    artifact_role,
    verification_state,
    lifecycle_state
)
SELECT
    'legacy_ea_' || LEFT(MD5(source_version.source_version_id || ':original'), 24),
    source_version.source_version_id,
    'legacy_original',
    'legacy_backfill',
    'active'
FROM source_versions AS source_version
ON CONFLICT DO NOTHING;

UPDATE documents AS document
SET extraction_artifact_id =
    'legacy_ea_' || LEFT(MD5(document.source_version_id || ':original'), 24)
WHERE document.extraction_artifact_id IS NULL;

-- The legacy filepath remains on documents for 10-K compatibility, while the
-- new alias row becomes the durable location history.
INSERT INTO file_aliases (
    file_alias_id,
    source_version_id,
    extraction_artifact_id,
    observed_company_id,
    file_path,
    observed_filename,
    lifecycle_state
)
SELECT
    'legacy_fa_' || LEFT(MD5(document.filepath), 24),
    document.source_version_id,
    document.extraction_artifact_id,
    document.company_id,
    document.filepath,
    REGEXP_REPLACE(document.filepath, '^.*[/\\\\]', ''),
    document.lifecycle_state
FROM documents AS document
WHERE document.filepath IS NOT NULL
  AND BTRIM(document.filepath) <> ''
ON CONFLICT DO NOTHING;

UPDATE documents AS document
SET file_alias_id = 'legacy_fa_' || LEFT(MD5(document.filepath), 24)
WHERE document.file_alias_id IS NULL
  AND document.filepath IS NOT NULL
  AND BTRIM(document.filepath) <> '';

-- Sections inherit the version used to create their stored text.
UPDATE sections AS section
SET logical_source_id = COALESCE(section.logical_source_id, document.logical_source_id),
    source_version_id = COALESCE(section.source_version_id, document.source_version_id),
    extraction_artifact_id = COALESCE(
        section.extraction_artifact_id,
        document.extraction_artifact_id
    )
FROM documents AS document
WHERE section.doc_id = document.doc_id
  AND (
      section.logical_source_id IS NULL
      OR section.source_version_id IS NULL
      OR section.extraction_artifact_id IS NULL
  );

-- Chunks with an old version keep that version's history. Chunks without one
-- inherit their document lineage.
WITH chunk_lineage AS (
    SELECT
        chunk.chunk_id,
        COALESCE(source_version.source_version_id, document.source_version_id)
            AS source_version_id,
        COALESCE(source_version.logical_source_id, document.logical_source_id)
            AS logical_source_id
    FROM chunks AS chunk
    JOIN documents AS document ON document.doc_id = chunk.doc_id
    LEFT JOIN source_versions AS source_version
      ON source_version.legacy_source_version_id = chunk.legacy_source_version_id
)
UPDATE chunks AS chunk
SET source_version_id = chunk_lineage.source_version_id,
    logical_source_id = chunk_lineage.logical_source_id,
    extraction_artifact_id = 'legacy_ea_' || LEFT(
        MD5(chunk_lineage.source_version_id || ':original'),
        24
    )
FROM chunk_lineage
WHERE chunk.chunk_id = chunk_lineage.chunk_id
  AND (
      chunk.logical_source_id IS NULL
      OR chunk.extraction_artifact_id IS NULL
      OR chunk.source_version_id IS NULL
      OR chunk.source_version_id NOT LIKE 'sv\_%' ESCAPE '\'
  );

-- Add constraints after the backfill. Each DO block is safe to rerun.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'documents'::regclass
          AND conname = 'fk_documents_logical_version'
    ) THEN
        ALTER TABLE documents
        ADD CONSTRAINT fk_documents_logical_version
        FOREIGN KEY (logical_source_id, source_version_id)
        REFERENCES source_versions(logical_source_id, source_version_id)
        ON DELETE RESTRICT NOT VALID;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'documents'::regclass
          AND conname = 'fk_documents_version_artifact'
    ) THEN
        ALTER TABLE documents
        ADD CONSTRAINT fk_documents_version_artifact
        FOREIGN KEY (source_version_id, extraction_artifact_id)
        REFERENCES extraction_artifacts(source_version_id, extraction_artifact_id)
        ON DELETE RESTRICT NOT VALID;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'documents'::regclass
          AND conname = 'fk_documents_file_alias'
    ) THEN
        ALTER TABLE documents
        ADD CONSTRAINT fk_documents_file_alias
        FOREIGN KEY (file_alias_id)
        REFERENCES file_aliases(file_alias_id) ON DELETE RESTRICT NOT VALID;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'documents'::regclass
          AND conname = 'fk_documents_superseded_by'
    ) THEN
        ALTER TABLE documents
        ADD CONSTRAINT fk_documents_superseded_by
        FOREIGN KEY (superseded_by_doc_id)
        REFERENCES documents(doc_id) ON DELETE RESTRICT NOT VALID;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'sections'::regclass
          AND conname = 'fk_sections_logical_version'
    ) THEN
        ALTER TABLE sections
        ADD CONSTRAINT fk_sections_logical_version
        FOREIGN KEY (logical_source_id, source_version_id)
        REFERENCES source_versions(logical_source_id, source_version_id)
        ON DELETE RESTRICT NOT VALID;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'sections'::regclass
          AND conname = 'fk_sections_version_artifact'
    ) THEN
        ALTER TABLE sections
        ADD CONSTRAINT fk_sections_version_artifact
        FOREIGN KEY (source_version_id, extraction_artifact_id)
        REFERENCES extraction_artifacts(source_version_id, extraction_artifact_id)
        ON DELETE RESTRICT NOT VALID;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'sections'::regclass
          AND conname = 'fk_sections_superseded_by'
    ) THEN
        ALTER TABLE sections
        ADD CONSTRAINT fk_sections_superseded_by
        FOREIGN KEY (superseded_by_section_id)
        REFERENCES sections(section_id) ON DELETE RESTRICT NOT VALID;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'chunks'::regclass
          AND conname = 'fk_chunks_logical_version'
    ) THEN
        ALTER TABLE chunks
        ADD CONSTRAINT fk_chunks_logical_version
        FOREIGN KEY (logical_source_id, source_version_id)
        REFERENCES source_versions(logical_source_id, source_version_id)
        ON DELETE RESTRICT NOT VALID;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'chunks'::regclass
          AND conname = 'fk_chunks_version_artifact'
    ) THEN
        ALTER TABLE chunks
        ADD CONSTRAINT fk_chunks_version_artifact
        FOREIGN KEY (source_version_id, extraction_artifact_id)
        REFERENCES extraction_artifacts(source_version_id, extraction_artifact_id)
        ON DELETE RESTRICT NOT VALID;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'chunks'::regclass
          AND conname = 'fk_chunks_superseded_by'
    ) THEN
        ALTER TABLE chunks
        ADD CONSTRAINT fk_chunks_superseded_by
        FOREIGN KEY (superseded_by_chunk_id)
        REFERENCES chunks(chunk_id) ON DELETE RESTRICT NOT VALID;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'documents'::regclass
          AND conname = 'ck_documents_lifecycle_state'
    ) THEN
        ALTER TABLE documents
        ADD CONSTRAINT ck_documents_lifecycle_state
        CHECK (lifecycle_state IN ('active', 'superseded')) NOT VALID;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'sections'::regclass
          AND conname = 'ck_sections_lifecycle_state'
    ) THEN
        ALTER TABLE sections
        ADD CONSTRAINT ck_sections_lifecycle_state
        CHECK (lifecycle_state IN ('active', 'superseded')) NOT VALID;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'chunks'::regclass
          AND conname = 'ck_chunks_lifecycle_state'
    ) THEN
        ALTER TABLE chunks
        ADD CONSTRAINT ck_chunks_lifecycle_state
        CHECK (lifecycle_state IN ('active', 'superseded')) NOT VALID;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_documents_logical_source
ON documents(logical_source_id);

CREATE INDEX IF NOT EXISTS idx_documents_source_version
ON documents(source_version_id);

CREATE INDEX IF NOT EXISTS idx_documents_extraction_artifact
ON documents(extraction_artifact_id);

CREATE INDEX IF NOT EXISTS idx_documents_lifecycle_state
ON documents(lifecycle_state);

CREATE INDEX IF NOT EXISTS idx_sections_logical_source
ON sections(logical_source_id);

CREATE INDEX IF NOT EXISTS idx_sections_source_version
ON sections(source_version_id);

CREATE INDEX IF NOT EXISTS idx_sections_extraction_artifact
ON sections(extraction_artifact_id);

CREATE INDEX IF NOT EXISTS idx_sections_lifecycle_state
ON sections(lifecycle_state);

CREATE INDEX IF NOT EXISTS idx_chunks_logical_source
ON chunks(logical_source_id);

CREATE INDEX IF NOT EXISTS idx_chunks_extraction_artifact
ON chunks(extraction_artifact_id);

CREATE INDEX IF NOT EXISTS idx_chunks_lifecycle_state
ON chunks(lifecycle_state);

-- These validations scan existing rows but do not rewrite or delete them.
-- They make the migration fail and roll back if a backfilled link is unsafe.
ALTER TABLE documents VALIDATE CONSTRAINT fk_documents_logical_version;
ALTER TABLE documents VALIDATE CONSTRAINT fk_documents_version_artifact;
ALTER TABLE documents VALIDATE CONSTRAINT fk_documents_file_alias;
ALTER TABLE documents VALIDATE CONSTRAINT fk_documents_superseded_by;
ALTER TABLE documents VALIDATE CONSTRAINT ck_documents_lifecycle_state;

ALTER TABLE sections VALIDATE CONSTRAINT fk_sections_logical_version;
ALTER TABLE sections VALIDATE CONSTRAINT fk_sections_version_artifact;
ALTER TABLE sections VALIDATE CONSTRAINT fk_sections_superseded_by;
ALTER TABLE sections VALIDATE CONSTRAINT ck_sections_lifecycle_state;

ALTER TABLE chunks VALIDATE CONSTRAINT fk_chunks_logical_version;
ALTER TABLE chunks VALIDATE CONSTRAINT fk_chunks_version_artifact;
ALTER TABLE chunks VALIDATE CONSTRAINT fk_chunks_superseded_by;
ALTER TABLE chunks VALIDATE CONSTRAINT ck_chunks_lifecycle_state;

-- Validation is separate and read-only. Run scripts/validate_esg_db_identity.py
-- before and after applying this migration. Do not apply this migration from
-- the pipeline runner.
