"""
models.py
SQLAlchemy ORM models for Retail Intelligence Pipeline.
Matches V1__Schema.sql + V2__Chunking_DB_Fixes.sql.
"""

from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    Date,
    TIMESTAMP,
    ForeignKey,
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


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("filepath", name="uq_documents_filepath"),
    )

    doc_id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.company_id", ondelete="CASCADE"), nullable=False)
    doc_type = Column(String(30), nullable=False)
    filepath = Column(Text, nullable=False)
    parse_status = Column(String(30), default="not_started")
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now())


class Section(Base):
    __tablename__ = "sections"
    __table_args__ = (
        UniqueConstraint("doc_id", "section_code", name="uq_sections_doc_code"),
    )

    section_id = Column(Integer, primary_key=True)
    doc_id = Column(Integer, ForeignKey("documents.doc_id", ondelete="CASCADE"), nullable=False)
    section_code = Column(String(50))
    section_title = Column(String(255))
    section_text = Column(Text)
    char_count = Column(Integer, default=0)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now())


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("section_id", "chunk_index", name="uq_chunks_section_index"),
    )

    chunk_id = Column(Integer, primary_key=True)
    section_id = Column(Integer, ForeignKey("sections.section_id", ondelete="CASCADE"), nullable=False)
    doc_id = Column(Integer, ForeignKey("documents.doc_id", ondelete="CASCADE"), nullable=False)
    company_id = Column(Integer, ForeignKey("companies.company_id", ondelete="CASCADE"), nullable=False)
    doc_type = Column(String(30))
    section_code = Column(String(50))
    chunk_index = Column(Integer, nullable=False)
    chunk_text = Column(Text, nullable=False)
    token_count = Column(Integer)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now())
