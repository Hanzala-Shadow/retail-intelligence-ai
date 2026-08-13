# ESG pipeline transfer package

Dataset `esg_docling_fusion_v2`, chunker `esg_chunk_v4`.
Source commit: `ff9a6b2c0ce2a26cd0e5b6c6a4968f4af316c6f8`

Generated ESG corpus, the code that produced it, and an offline SQLite QA
mirror. **No source PDFs.** Embeddings, vector indexing, retrieval evaluation,
serving, API and UI are not included and are not in scope for this package.

## Canonical counts

| measure | value |
|---|---|
| documents | 681 |
| sections | 18,710 |
| chunks | 50,530 |
| eligible to index | 49,754 |
| excluded, retained for audit | 776 |

### Deviation from the acceptance guide

The guide's gate table was written against an earlier corpus. Compare against
the values above, not the guide's:

| measure | guide | this package | delta |
|---|---|---|---|
| documents | 682 | 681 | -1 |
| sections | 18,707 | 18,710 | +3 |
| chunks | 50,510 | 50,530 | +20 |
| eligible_chunks | 49,734 | 49,754 | +20 |
| excluded_chunks | 776 | 776 | +0 |

One document (`GROV-GROVE COLLABORATIVE HLDG INC-2018`) was dropped from these
indexes. It was **not** deleted from Drive: on 2026-08-11 it was moved out of
the corpus folder `_ESG Reports/GROV/` into `Other ESG Sustainability Related
Reports/`, the holding area for out-of-scope documents, and a second copy
remains under `Archive/Sustainability Reports New/`. Both match the local file
by md5 (`97acf3b6e7bf6379678f69d74c57afbd`), verified against live Drive on
2026-08-12. The document was reclassified as out of scope, so its 8 chunks were
removed from the corpus; the bytes and their provenance are still in Drive.

Eight Type3-font documents that previously contributed no indexable content now
section correctly, which raises the chunk counts.

## What is in here

| path | holds |
|---|---|
| `common/`, `esg/config.py` | path and environment resolution, shared by every stage |
| `esg/src/` | the pipeline modules: `section_splitter_esg.py`, `esg_chunker.py`, and the TOC and year helpers they use |
| `esg/scripts/` | stage runners, the Ubuntu and Windows orchestrators, and the audit, sync and packaging tools |
| `esg/docs/` | stage-by-stage reference, data layout and runbooks |
| `tests/`, `esg/tests/` | the suite; `conftest.py` and `pytest.ini` are what make it importable |
| `models/bge-base-en-v1.5-tokenizer/` | the exact tokenizer chunk token counts depend on -- the chunker refuses to run without it |
| `data/00_reference/` | the canonical indexes and the reference CSVs they join to |
| `data/02_interim/sustainability/03_pipeline_text/` | bridge output: one text file, page map and heading list per document |
| `data/03_sections/sustainability/` | one file per topic section |
| `data/04_chunks/sustainability/` | one file per chunk |
| `data/esg.db` | the offline SQLite QA mirror |

The canonical data paths, which every stage defaults to:

```
data/00_reference/esg_parse_index.csv        source identity and lineage (input)
data/00_reference/esg_parse_index_v2.csv     per-document parse record (stage 3)
data/00_reference/esg_sections_index.csv     section index (stage 4)
data/00_reference/esg_chunks_index.csv       chunk index (stage 5) -- the deliverable
```

`esg_chunks_index.csv` is the artifact everything else exists to produce. Each
row carries its own `embedding_text`, so downstream retrieval needs that file
alone; the chunk and section files are what make a citation auditable back to
the source document.

## Install

Ubuntu 24.04, Python 3.12 or 3.13. The code parses cleanly under 3.12; the
corpus was produced on 3.13.

```bash
python3 -m venv venv
./venv/bin/python -m pip install -r requirements.txt      # production
./venv/bin/python -m pip install -r requirements-dev.txt  # adds pytest
```

`requirements-docling.txt` is only needed to re-run stages 1-2, which require
a CUDA torch build. This package ships their output, so the server does not
need it.

## Run

```bash
esg/scripts/run_esg_pipeline.sh                  # stages 3-5 (Linux)
esg/scripts/run_esg_pipeline.sh --with-convert   # adds stages 1-2, needs docling
```

The PowerShell equivalent, `esg/scripts/run_docling_fusion_corpus.ps1`, is
retained for Windows.

## Validate, read-only

```bash
sha256sum -c PACKAGE_SHA256SUMS
./venv/bin/python esg/scripts/audit_esg_qa_db.py --out /tmp/audit.json
./venv/bin/python esg/scripts/summarise_fusion_run.py \
    --parse-index data/00_reference/esg_parse_index_v2.csv \
    --chunks-index data/00_reference/esg_chunks_index.csv
./venv/bin/python -m pytest -q
```

**On line endings, before you report it as a defect.**
`PACKAGE_MANIFEST.csv` is CRLF; every other generated artifact here --
`PACKAGE_SHA256SUMS`, the archive's `.sha256` sidecar, this README,
`PACKAGE_METADATA.json`, `DATABASE_AUDIT.json`, `TEST_RESULTS.txt` -- is LF.
That difference is deliberate and both halves matter.

The manifest is a CSV, and RFC 4180 specifies CRLF as the line terminator, so
every CSV reader expects and strips it. The checksum files are parsed by
`sha256sum`, which takes everything after the two spaces as a filename and has
no notion of the CSV spec: a CRLF there makes it look for `README.md\r`, so
every entry reports "FAILED open or read" while the hashes underneath are
perfectly correct. That failure is indistinguishable from a corrupt archive,
which is why the two file types are written differently on purpose.

Two reviewers have flagged the manifest as an inconsistency before reading
this paragraph. It is not one. See `write_lf` in
`esg/scripts/build_transfer_package.py` for the same explanation in the code.

## Paths and configuration

`common/config.py` resolves every path from the package root, which it takes
from its own location on disk. Nothing requires a drive letter, a named user
profile or a Drive mount, so this runs wherever it is unpacked.

**There is no environment variable that relocates the project or the data
directory.** `data/` is always `<package root>/data`; to put the corpus
somewhere else, move the whole package. The transfer guide suggests
`RETAIL_INTELLIGENCE_ROOT` and `ESG_DATA_ROOT` overrides and this build does
not implement them -- stated here rather than left to be discovered, so nobody
sets them and expects an effect.

The variables `common/config.py` does read are all optional, and **none are
needed for stages 3-5**:

| variable | used by |
|---|---|
| `DB_URL` | SQLAlchemy connection string, legacy loader only |
| `DRIVE_ROOT_FOLDER_ID` | Google Drive folder id for the source audit |
| `GOOGLE_DRIVE_CLIENT_SECRET` | OAuth client secret path |
| `GOOGLE_DRIVE_CREDENTIALS_PATH` | OAuth token path |
| `SEC_USER_AGENT` | SEC request header, 10-K path only |

Drive access is an acquisition and audit step, never an import-time
requirement: the pipeline runs with none of these set.

On the command line, the two stages that write the corpus take explicit paths,
all defaulting to the locations above:

```
section_splitter_esg.py  --input --out --index --ticker --force
esg_chunker.py           --input --out --index --sections-index
                         --parse-index --ticker
```

Pass `--help` to either for the current list. Do not hardcode absolute paths
into any index, database row or manifest.

## The SQLite database

`data/esg.db` is an **offline QA mirror** of the canonical CSV indexes. It is
not a production serving database and must not silently become one. Rebuild it
with `esg/scripts/build_esg_qa_db.py`, which refuses to overwrite an existing
file.

## Resume and cleanup

The pipeline caches by document and resumes: re-running skips completed work
unless `--force` is passed. `esg/scripts/prepare_clean_fusion_run.py` is
**destructive** -- it clears generated output. Read it before running it.

## Known exceptions

### locally-minted-source-versions

Four identity IDs were minted on the packaging machine rather than issued upstream: source_version_id and extraction_artifact_id for BBY-BEST BUY CO INC-2016.pdf and COST-COSTCO WHOLESALE CORP-2020.pdf.

**Why:** Drive replaced both files on 2026-08-07, after the catalog snapshot. The corpus holds the current bytes (confirmed against Drive by md5 on 2026-08-12); the catalog described the superseded ones, so both documents carried no identity at all.

**Risk:** If the upstream Drive system later issues its own IDs for the same bytes, the two will disagree and nothing will detect it. Resolve by replacing these four IDs with upstream's when available.

**Visible in:** `esg_file_catalog.csv review_reason on the affected rows`

### ges-duplicate-alias

The loader logs 'conflicting catalog rows' for logical source ls_199108d2ad0d75ec066ffbee and artifact ea_b8f079115d5fb4d3e26840cb.

**Why:** GES-GUESS INC-2020-2021.pdf and GES-GUESS-2021-2020.pdf are one file stored in Drive under two names, identical sha256. One row is the canonical alias, the other carries duplicate_of_source_version_id.

**Risk:** None. This is the file_alias layer modelling two names for one source version, which is what it exists for. Documented rather than fixed.

**Visible in:** `esg_file_catalog.csv canonical_alias / duplicate_of_source_version_id`
