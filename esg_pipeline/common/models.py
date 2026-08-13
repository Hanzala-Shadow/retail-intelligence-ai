"""
models.py
SQLAlchemy ORM models for Retail Intelligence Pipeline.
Matches the migrations in data/Teamwork/05_db/migrations through V6.
"""

from sqlalchemy import (
    Column,
    BigInteger,
    Integer,
    String,
    Text,
    Date,
    Boolean,
    TIMESTAMP,
    ForeignKey,
    ForeignKeyConstraint,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Company(Base):
    __tablename__ = "companies"

    company_id = Column(Integer, primary_key=True)
    ticker = Column(String(20), nullable=False, unique=True)
    cik = Column(String(20), nullable=False, unique=True)
    name = Column(String(255), nullable=False)
    sector = Column(String(100))
    exchange = Column(String(50))
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now())


class AnnualFiling(Base):
    __tablename__ = "annual_filings"

    filing_id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.company_id", ondelete="CASCADE"), nullable=False)
    year = Column(Integer, nullable=False)
    accession_number = Column(String(50), unique=True, nullable=False)
    filing_date = Column(Date)
    download_status = Column(String(30), default="pending")
    drive_file_id = Column(Text)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now())


class SustainabilityReport(Base):
    __tablename__ = "sustainability_reports"
    __table_args__ = (
        UniqueConstraint("company_id", "year", name="uq_sustainability_company_year"),
    )

    report_id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.company_id", ondelete="CASCADE"), nullable=False)
    year = Column(Integer)
    report_url = Column(Text)
    drive_file_id = Column(Text)
    format = Column(String(20))
    download_status = Column(String(30), default="pending")
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now())


class LogicalSource(Base):
    __tablename__ = "logical_sources"

    logical_source_id = Column(String(320), primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.company_id", ondelete="RESTRICT"))
    policy_source_id = Column(String(320), unique=True)
    source_type = Column(String(80), nullable=False, default="unknown")
    report_year = Column(Integer)
    title = Column(Text)
    lifecycle_state = Column(String(20), nullable=False, default="active")
    superseded_by_logical_source_id = Column(
        String(320),
        ForeignKey("logical_sources.logical_source_id", ondelete="RESTRICT"),
    )
    ownership_review_required = Column(Boolean, nullable=False, default=False)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now())


class SourceVersion(Base):
    __tablename__ = "source_versions"
    __table_args__ = (
        UniqueConstraint(
            "logical_source_id",
            "source_version_id",
            name="uq_source_versions_logical_version",
        ),
    )

    source_version_id = Column(String(320), primary_key=True)
    logical_source_id = Column(
        String(320),
        ForeignKey("logical_sources.logical_source_id", ondelete="RESTRICT"),
        nullable=False,
    )
    original_sha256 = Column(String(64), unique=True)
    legacy_source_version_id = Column(String(320), unique=True)
    byte_size = Column(BigInteger)
    media_type = Column(String(100))
    lifecycle_state = Column(String(20), nullable=False, default="active")
    superseded_by_source_version_id = Column(
        String(320),
        ForeignKey("source_versions.source_version_id", ondelete="RESTRICT"),
    )
    ownership_review_required = Column(Boolean, nullable=False, default=False)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now())


class ExtractionArtifact(Base):
    __tablename__ = "extraction_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "source_version_id",
            "extraction_artifact_id",
            name="uq_extraction_artifacts_version_artifact",
        ),
    )

    extraction_artifact_id = Column(String(320), primary_key=True)
    source_version_id = Column(
        String(320),
        ForeignKey("source_versions.source_version_id", ondelete="RESTRICT"),
        nullable=False,
    )
    artifact_role = Column(String(40), nullable=False)
    artifact_sha256 = Column(String(64))
    storage_path = Column(Text)
    drive_file_id = Column(Text)
    parser_or_model = Column(String(160))
    prompt_version = Column(String(100))
    source_page_sha256 = Column(String(64))
    verification_state = Column(String(40), nullable=False, default="unverified")
    lifecycle_state = Column(String(20), nullable=False, default="active")
    superseded_by_extraction_artifact_id = Column(
        String(320),
        ForeignKey("extraction_artifacts.extraction_artifact_id", ondelete="RESTRICT"),
    )
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now())


class FileAlias(Base):
    __tablename__ = "file_aliases"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_version_id", "extraction_artifact_id"],
            [
                "extraction_artifacts.source_version_id",
                "extraction_artifacts.extraction_artifact_id",
            ],
            name="fk_file_aliases_version_artifact",
            ondelete="RESTRICT",
        ),
    )

    file_alias_id = Column(String(320), primary_key=True)
    source_version_id = Column(
        String(320),
        ForeignKey("source_versions.source_version_id", ondelete="RESTRICT"),
        nullable=False,
    )
    extraction_artifact_id = Column(String(320))
    observed_company_id = Column(
        Integer,
        ForeignKey("companies.company_id", ondelete="RESTRICT"),
    )
    file_path = Column(Text)
    drive_file_id = Column(Text)
    observed_filename = Column(Text)
    lifecycle_state = Column(String(20), nullable=False, default="active")
    first_seen_at = Column(TIMESTAMP, server_default=func.now())
    last_seen_at = Column(TIMESTAMP, server_default=func.now())
    superseded_by_file_alias_id = Column(
        String(320),
        ForeignKey("file_aliases.file_alias_id", ondelete="RESTRICT"),
    )
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now())


class SourceApproval(Base):
    __tablename__ = "source_approvals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["logical_source_id", "source_version_id"],
            ["source_versions.logical_source_id", "source_versions.source_version_id"],
            name="fk_source_approvals_logical_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_version_id", "extraction_artifact_id"],
            [
                "extraction_artifacts.source_version_id",
                "extraction_artifacts.extraction_artifact_id",
            ],
            name="fk_source_approvals_version_artifact",
            ondelete="RESTRICT",
        ),
    )

    # SQLite only auto-assigns rowids for columns declared exactly
    # "INTEGER PRIMARY KEY", so a plain BigInteger PK cannot autoincrement
    # there. The variant keeps BIGINT on PostgreSQL, which is the migration
    # target, while letting a local SQLite load insert approvals.
    source_approval_id = Column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True
    )
    logical_source_id = Column(String(320), nullable=False)
    source_version_id = Column(String(320), nullable=False)
    extraction_artifact_id = Column(String(320), nullable=False)
    approval_type = Column(String(40), nullable=False, default="ocr_replacement")
    approval_status = Column(String(20), nullable=False)
    approved_source_sha256 = Column(String(64), nullable=False)
    approved_artifact_sha256 = Column(String(64), nullable=False)
    reviewer = Column(String(255))
    approval_date = Column(TIMESTAMP)
    reason = Column(Text)
    lifecycle_state = Column(String(20), nullable=False, default="active")
    superseded_by_source_approval_id = Column(
        BigInteger,
        ForeignKey("source_approvals.source_approval_id", ondelete="RESTRICT"),
    )
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now())


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("filepath", name="uq_documents_filepath"),
        ForeignKeyConstraint(
            ["logical_source_id", "source_version_id"],
            ["source_versions.logical_source_id", "source_versions.source_version_id"],
            name="fk_documents_logical_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_version_id", "extraction_artifact_id"],
            [
                "extraction_artifacts.source_version_id",
                "extraction_artifacts.extraction_artifact_id",
            ],
            name="fk_documents_version_artifact",
            ondelete="RESTRICT",
        ),
    )

    doc_id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.company_id", ondelete="CASCADE"), nullable=False)
    # Which sustainability report this document is. Without it the report year
    # is unreachable from a chunk: sustainability_reports carries the year but
    # keys only to companies, so documents -> companies -> reports is a cross
    # join. AAPL has nine documents and nine reports, and that path returns 81
    # rows. Every ESG query the research team needs filters or groups by report
    # year, so the join has to resolve to exactly one row.
    #
    # Nullable because annual filings share this table and have no
    # sustainability report; the ESG loader fills it for every ESG document and
    # tests/test_documents_link_to_reports.py holds that line.
    report_id = Column(
        Integer,
        ForeignKey("sustainability_reports.report_id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    doc_type = Column(String(30), nullable=False)
    filepath = Column(Text, nullable=False)
    parse_status = Column(String(30), default="not_started")
    quality_flags = Column(Text)
    possible_wrong_doc_type = Column(Boolean, default=False)
    doc_quality_status = Column(String(40))
    rag_action = Column(String(50))
    logical_source_id = Column(String(320))
    source_version_id = Column(String(320))
    extraction_artifact_id = Column(String(320))
    file_alias_id = Column(
        String(320),
        ForeignKey("file_aliases.file_alias_id", ondelete="RESTRICT"),
    )
    lifecycle_state = Column(String(20), nullable=False, default="active")
    superseded_by_doc_id = Column(
        Integer,
        ForeignKey("documents.doc_id", ondelete="RESTRICT"),
    )
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now())


class Section(Base):
    __tablename__ = "sections"
    __table_args__ = (
        UniqueConstraint("doc_id", "section_instance_id", name="uq_sections_doc_instance"),
        ForeignKeyConstraint(
            ["logical_source_id", "source_version_id"],
            ["source_versions.logical_source_id", "source_versions.source_version_id"],
            name="fk_sections_logical_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_version_id", "extraction_artifact_id"],
            [
                "extraction_artifacts.source_version_id",
                "extraction_artifacts.extraction_artifact_id",
            ],
            name="fk_sections_version_artifact",
            ondelete="RESTRICT",
        ),
    )

    section_id = Column(Integer, primary_key=True)
    doc_id = Column(Integer, ForeignKey("documents.doc_id", ondelete="CASCADE"), nullable=False)
    section_instance_id = Column(String(100), nullable=False)
    section_code = Column(String(50))
    # Text, not String(255): a heading is whatever the document styled as one,
    # and 14 of 18,710 sections exceed 255 characters -- up to 569 -- because
    # some reports style a lead sentence as a heading. SQLite ignores VARCHAR
    # limits so this loaded cleanly for as long as the mirror was the only
    # target; PostgreSQL enforces them and rejected the row outright with
    # StringDataRightTruncation, which is how it was found.
    #
    # Text rather than a wider varchar because no bound is defensible here --
    # the next corpus can always exceed it -- and truncating at load would make
    # the SQLite mirror and PostgreSQL disagree about the same field, which is
    # the one thing a mirror must not do. On PostgreSQL text and varchar(n)
    # are the same representation, so this costs nothing.
    section_title = Column(Text)
    section_text = Column(Text)
    char_count = Column(Integer, default=0)
    source_start_char = Column(Integer)
    source_end_char = Column(Integer)
    page_start = Column(Integer)
    page_end = Column(Integer)
    logical_source_id = Column(String(320))
    source_version_id = Column(String(320))
    extraction_artifact_id = Column(String(320))
    lifecycle_state = Column(String(20), nullable=False, default="active")
    superseded_by_section_id = Column(
        Integer,
        ForeignKey("sections.section_id", ondelete="RESTRICT"),
    )
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now())


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("section_id", "chunk_index", name="uq_chunks_section_index"),
        UniqueConstraint("external_chunk_id", name="uq_chunks_external_chunk_id"),
        ForeignKeyConstraint(
            ["logical_source_id", "source_version_id"],
            ["source_versions.logical_source_id", "source_versions.source_version_id"],
            name="fk_chunks_logical_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_version_id", "extraction_artifact_id"],
            [
                "extraction_artifacts.source_version_id",
                "extraction_artifacts.extraction_artifact_id",
            ],
            name="fk_chunks_version_artifact",
            ondelete="RESTRICT",
        ),
    )

    chunk_id = Column(Integer, primary_key=True)
    external_chunk_id = Column(String(512))
    section_id = Column(Integer, ForeignKey("sections.section_id", ondelete="CASCADE"), nullable=False)
    doc_id = Column(Integer, ForeignKey("documents.doc_id", ondelete="CASCADE"), nullable=False)
    company_id = Column(Integer, ForeignKey("companies.company_id", ondelete="CASCADE"), nullable=False)
    doc_type = Column(String(30))
    section_instance_id = Column(String(100))
    section_code = Column(String(50))
    source_id = Column(String(255))
    source_version_id = Column(String(320))
    logical_source_id = Column(String(320))
    extraction_artifact_id = Column(String(320))
    legacy_source_version_id = Column(String(320))
    chunk_type = Column(String(40))
    short_section_action = Column(String(40))
    short_section_reason = Column(String(120))
    merged_section_ids = Column(Text)
    doc_quality_status = Column(String(40))
    rag_action = Column(String(50))
    quality_flags = Column(Text)
    source_start_char = Column(Integer)
    source_end_char = Column(Integer)
    page_start = Column(Integer)
    page_end = Column(Integer)
    citation_ready = Column(Boolean, default=False)
    citation_validation_status = Column(String(40))
    citation_validation_version = Column(String(50))
    lifecycle_state = Column(String(20), nullable=False, default="active")
    superseded_by_chunk_id = Column(
        Integer,
        ForeignKey("chunks.chunk_id", ondelete="RESTRICT"),
    )
    chunk_index = Column(Integer, nullable=False)
    chunk_text = Column(Text, nullable=False)
    token_count = Column(Integer)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now())
