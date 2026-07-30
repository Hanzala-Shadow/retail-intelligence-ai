# Retail Intelligence Pipeline Architecture

## Purpose

The project collects retail 10-K filings and ESG/sustainability PDFs, converts them into structured text sections and chunks, loads them into PostgreSQL, and prepares a controlled corpus for retrieval-augmented generation.

## Repository layout

The two pipelines are separate top-level directories. They share a database, a
`data/` tree, and four modules — nothing else.

```text
common/              shared by both pipelines
  config.py            repo root, data/ stage dirs, env vars, the path bridge
  models.py            SQLAlchemy models
  db_utils.py          connection + health check
  base_parser.py       ParsedDocument / TableRef contract
  drive_auth.py        Google Drive OAuth desktop flow

esg/                 ESG / sustainability pipeline
  config.py            ESG_* paths; re-exports common/config.py
  src/                 stages
  scripts/             runners, QA, audits (all current runners are ESG)
  tests/
  docs/

filings/             10-K / SEC filings pipeline
  config.py            10-K paths; re-exports common/config.py
  src/                 stages
  scripts/             (empty)
  tests/               (empty)

data/                unchanged by the split — both pipelines write here
reports/  logs/      unchanged
tests/               cross-cutting tests that police both pipelines
conftest.py          puts both pipelines on sys.path for every test
```

`data/` and the database stay single on purpose: the split is about where code
lives, not where output goes. No stage directory, index CSV, or table moved.

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

- `filings/src/sec_discovery.py`
- `filings/src/sec_downloader.py`
- `filings/src/html_parser.py`
- `filings/src/section_splitter_10k.py`
- `filings/src/chunker.py`
- `filings/src/db_loader.py`
- `filings/src/chunks_bulk_loader.py`

ESG pipeline:

- `esg/src/drive_downloader.py`
- `esg/src/pdf_parser.py`
- `esg/src/backfill_esg_page_maps.py`
- `esg/src/section_splitter_esg.py`
- `esg/src/esg_chunker.py`
- `esg/src/esg_pipeline_qa.py`
- `esg/src/drive_to_db.py`

## Imports

Each pipeline directory is put on `sys.path` by a `_bootstrap` module, one per
`src/` and `scripts/` directory. A stage or script writes:

```python
import _bootstrap  # noqa: F401
import config                       # esg/config.py or filings/config.py
from common.models import Document  # the shared package
```

`_bootstrap` is importable with no path hack of its own, because running
`python esg/src/pdf_parser.py` already puts `esg/src/` on `sys.path`. That is
what keeps the depth arithmetic in four files instead of the 37 copies each
consumer carried before the split. Tests need no bootstrap at all — the rootdir
`conftest.py` covers them.

`common` is a package reached from the repo root, so `from common import
config` never competes with the bare `config` each pipeline owns.

## Paths

Every `data/`, `reports/`, and `logs/` path is defined once, in one of three
config modules: `common/config.py` for what both pipelines share,
`esg/config.py` and `filings/config.py` for what one pipeline owns. Both
pipeline configs re-export the shared half, so `import config` inside a
pipeline sees one flat namespace.

The PowerShell and bash runners read `python common/config.py --json`, which
prints the **merged** table — shared + ESG + 10-K. See
`docs/PIPELINE_PATHS.md`.

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

- `docs/PIPELINE_PATHS.md`
- `docs/DATABASE.md`
- `docs/AI_RAG_ARCHITECTURE.md`
- `docs/RAG_EVALUATION_PLAN.md`
- `esg/docs/ESG_PIPELINE.md`
- `esg/docs/ESG_SCRIPT_SPECIFICATIONS.md`

## Current Scale

As of the latest local QA regeneration:

- 193 companies
- 524 ESG tracker rows
- 452 parsed ESG PDFs
- 5,762 ESG sections
- 32,262 ESG chunks
- 40 seed RAG evaluation questions
