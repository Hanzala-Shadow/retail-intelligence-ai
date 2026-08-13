# Retail Intelligence Pipeline Architecture

## Purpose

The project collects retail 10-K filings and ESG/sustainability PDFs, converts them into structured text sections and chunks, loads them into PostgreSQL, and prepares a controlled corpus for retrieval-augmented generation.

## Data Flow

```text
Reference CSVs
-> SEC / Google Drive downloaders
-> raw documents
-> parsers
-> section splitters
-> chunkers
-> QA reports
-> PostgreSQL
-> vector indexes
-> RAG evaluation
```

## Main Inputs

- `data/00_reference/companies.csv`
- `data/00_reference/filings.csv`
- `data/00_reference/sustainability_report_tracker.csv`
- `data/01_raw/10k/{ticker}/*.htm`
- `data/01_raw/sustainability/{ticker}/*.pdf`

## Main Pipelines

10-K pipeline:

- `src/sec_discovery.py`
- `src/sec_downloader.py`
- `src/html_parser.py`
- `src/section_splitter_10k.py`
- `src/chunker.py`
- `src/db_loader.py`
- `src/chunks_bulk_loader.py`

ESG pipeline:

- `src/drive_downloader.py`
- `src/pdf_parser.py`
- `src/backfill_esg_page_maps.py`
- `src/section_splitter_esg.py`
- `src/esg_chunker.py`
- `src/esg_pipeline_qa.py`
- `src/drive_to_db.py`

## RAG Readiness

RAG should use the ESG chunks only after QA filtering:

```text
doc_type = sustainability
doc_quality_status = ok
rag_action = index_as_esg
citation_ready = true
```

Excluded ESG-folder PDFs such as SEC/10-K-like annual reports are preserved for auditability but marked:

```text
doc_type = annual_report_with_esg
doc_quality_status = exclude_from_esg_rag
rag_action = exclude_from_esg_index
```

## Key Documentation

- `docs/ESG_PIPELINE.md`
- `docs/AI_RAG_ARCHITECTURE.md`
- `docs/RAG_EVALUATION_PLAN.md`
- `docs/RAG_EVAL_HARNESS.md`
- `docs/DATABASE.md`

## Current Scale

As of the latest local QA regeneration:

- 193 companies
- 524 ESG tracker rows
- 452 parsed ESG PDFs
- 5,762 ESG sections
- 32,262 ESG chunks
- 40 seed RAG evaluation questions
