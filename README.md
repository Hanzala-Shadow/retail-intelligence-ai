# Retail Intelligence AI

This repository contains two isolated document-intelligence pipelines.

## Annual filings pipeline

Location: `annual_filings_pipeline/`

Processes SEC annual filings and supports the deployed annual-filings research application, PostgreSQL retrieval system, remote embedding/reranking services, and grounded answer generation.

## ESG pipeline

Location: `esg_pipeline/`

Processes sustainability and ESG reports through document fusion, sectioning, chunking, citation validation, offline QA, and PostgreSQL relational storage.

## Runtime boundaries

- Annual-filings database: `retail_pipeline`
- ESG database: `esg`
- Annual application services use only `annual_filings_pipeline/`
- Generated datasets, virtual environments, secrets, logs, and database backups are not committed
- Database backups remain under `/home/ubuntu/backups/`
