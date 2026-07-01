from sqlalchemy import (
    Column, Integer, String, Text, Date,
    ForeignKey, TIMESTAMP
)
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.sql import func

Base = declarative_base()


class Company(Base):
    __tablename__ = "companies"  # FIX: was _tablename_ (single underscore) -- SQLAlchemy silently ignored it

    company_id = Column(Integer, primary_key=True)
    ticker = Column(String(20), unique=True, nullable=False)
    cik = Column(String(20), unique=True, nullable=False)
    name = Column(String(255), nullable=False)
    sector = Column(String(100))
    exchange = Column(String(50))
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now())

    annual_filings = relationship("AnnualFiling", back_populates="company")
    sustainability_reports = relationship("SustainabilityReport", back_populates="company")
    documents = relationship("Document", back_populates="company")


class AnnualFiling(Base):
    __tablename__ = "annual_filings"  # FIX: was _tablename_

    filing_id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.company_id"), nullable=False)
    year = Column(Integer, nullable=False)
    accession_number = Column(String(50), unique=True, nullable=False)
    filing_date = Column(Date)
    download_status = Column(String(30), default="pending")
    created_at = Column(TIMESTAMP, server_default=func.now())  # FIX: was missing, exists in schema
    updated_at = Column(TIMESTAMP, server_default=func.now())  # FIX: was missing, exists in schema

    company = relationship("Company", back_populates="annual_filings")


class SustainabilityReport(Base):
    __tablename__ = "sustainability_reports"  # FIX: was _tablename_

    report_id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.company_id"), nullable=False)
    year = Column(Integer)
    report_url = Column(Text)
    format = Column(String(20))
    download_status = Column(String(30), default="pending")
    created_at = Column(TIMESTAMP, server_default=func.now())  # FIX: was missing
    updated_at = Column(TIMESTAMP, server_default=func.now())  # FIX: was missing

    company = relationship("Company", back_populates="sustainability_reports")


class Document(Base):
    __tablename__ = "documents"  # FIX: was _tablename_

    doc_id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.company_id"), nullable=False)
    doc_type = Column(String(30), nullable=False)
    filepath = Column(Text, nullable=False)
    parse_status = Column(String(30), default="not_started")
    created_at = Column(TIMESTAMP, server_default=func.now())  # FIX: was missing
    updated_at = Column(TIMESTAMP, server_default=func.now())  # FIX: was missing

    company = relationship("Company", back_populates="documents")
    sections = relationship("Section", back_populates="document")
    chunks = relationship("Chunk", back_populates="document")  # FIX: was missing entirely


class Section(Base):
    __tablename__ = "sections"  # FIX: was _tablename_

    section_id = Column(Integer, primary_key=True)
    doc_id = Column(Integer, ForeignKey("documents.doc_id"), nullable=False)
    section_code = Column(String(50))
    section_title = Column(String(255))
    section_text = Column(Text)
    char_count = Column(Integer, default=0)
    created_at = Column(TIMESTAMP, server_default=func.now())  # FIX: was missing
    updated_at = Column(TIMESTAMP, server_default=func.now())  # FIX: was missing

    document = relationship("Document", back_populates="sections")
    chunks = relationship("Chunk", back_populates="section")


class Chunk(Base):
    __tablename__ = "chunks"  # FIX: was _tablename_

    chunk_id = Column(Integer, primary_key=True)
    section_id = Column(Integer, ForeignKey("sections.section_id"), nullable=False)
    doc_id = Column(Integer, ForeignKey("documents.doc_id"), nullable=False)
    company_id = Column(Integer, ForeignKey("companies.company_id"), nullable=False)
    chunk_index = Column(Integer, nullable=False)
    chunk_text = Column(Text, nullable=False)
    token_count = Column(Integer)
    created_at = Column(TIMESTAMP, server_default=func.now())  # FIX: was missing
    updated_at = Column(TIMESTAMP, server_default=func.now())  # FIX: was missing

    section = relationship("Section", back_populates="chunks")
    document = relationship("Document", back_populates="chunks")  # FIX: was missing
    # Note: no back_populates on company side for chunks (Company doesn't need a chunks list)