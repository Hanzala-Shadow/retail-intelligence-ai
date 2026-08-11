# Retail Document Intelligence — ESG Docling-Fusion Pipeline

Turns sustainability/ESG PDF reports for publicly traded retailers into
retrieval-ready text chunks. The supported corpus is v2 and uses one canonical
section index, one canonical chunk index, and one active set of section/chunk
files. The pipeline ends at `esg_chunks_index.csv`. `data/esg.db` is an
optional offline QA copy of those files; it is not a serving database. This
repo has no embedding or vector-index stage.

The active internal identities are `esg_chunk_v4` for the chunker and
`esg_docling_fusion_v2` for the dataset. The `v2` name stays in metadata so
downstream evidence remains reproducible; active file and folder paths do not
carry parallel version suffixes.

## How it works

PDF in, chunks out, in six stages. Docling decides where the regions on a
page are and what order they read in; PyMuPDF supplies the actual words that
fill those regions (fusion). Neither tool does the whole job alone.

| # | Stage | Script | Env | What it does |
|---|-------|--------|-----|---------------|
| 1 | Convert | `esg/scripts/run_docling_gold_spike.py convert` | `venv-docling` | Runs Docling layout + TableFormer over each PDF, caches JSON. Slowest stage (~1.4 min/doc median). |
| 2 | Fuse | `esg/scripts/run_docling_gold_spike.py fuse` | `venv-docling` | Assigns PyMuPDF words into Docling's regions/tables, writes one fused text file per page. |
| 3 | Bridge | `esg/scripts/bridge_docling_to_pipeline.py` | `venv` | Converts fused pages into the per-document layout the rest of the pipeline reads; classifies page roles (content/toc/divider/cover/blank); writes `esg_parse_index_v2.csv`. |
| 4 | Sections | `esg/src/section_splitter_esg.py` | `venv` | Splits each document into topic sections at Docling's headings. |
| 5 | Chunks | `esg/src/esg_chunker.py` | `venv` | Cuts sections into token-bounded chunks (100–600 tokens; tiktoken + BGE tokenizer guard), builds `embedding_text`, applies retrieval gates. Produces **`esg_chunks_index.csv`**, the deliverable. |
| 6 | Summary | `esg/scripts/summarise_fusion_run.py` | `venv` | Read-only report: documents by quality flag, chunks by retrieval gate, documents contributing nothing to the index. |

Full stage-by-stage detail (I/O files, caching rules, flags) lives in
[esg/docs/DOCLING_FUSION_PIPELINE.md](esg/docs/DOCLING_FUSION_PIPELINE.md).

## Two virtual environments, on purpose

Docling pulls in torch/transformers (~1.5 GB) that the rest of the pipeline
doesn't need.

| Env | Built from | Runs stages |
|-----|-----------|-------------|
| `venv-docling` | `requirements-docling.txt` | 1–2 (convert, fuse) |
| `venv` | `requirements.txt` | 3–5 (bridge, sections, chunks) |

Both must be Python 3.13.2. Run commands from the repo root in PowerShell or
cmd — backslash paths break under Git Bash.

## Getting started

```powershell
python -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt

python -m venv venv-docling
venv-docling\Scripts\Activate.ps1
pip install -r requirements-docling.txt

Copy-Item .env.template .env
```

No `.env` values are required for stages 1–5; the template exists for the
legacy parser path.

## Running the pipeline

One command runs the whole chain:

```powershell
powershell -ExecutionPolicy Bypass -File esg\scripts\run_docling_fusion_corpus.ps1 -TimeBudgetMin 200
```

Reuse already-converted/fused work and start at the bridge stage:

```powershell
powershell -ExecutionPolicy Bypass -File esg\scripts\run_docling_fusion_corpus.ps1 -SkipConvert -SkipFuse
```

Every stage resumes by default and revalidates what it's about to skip
(content hashes, fusion settings, index rows) rather than trusting stale
output blindly. Use `-Force` to rebuild conversion, fusion, and bridge work.

Before a true corpus rebuild (PDFs added/removed/renamed), clear stale
sections/chunks/indexes first — sectioning and chunking **upsert**, so a
rebuild without clearing leaves stale rows mixed into the index:

```powershell
venv\Scripts\python.exe esg\scripts\prepare_clean_fusion_run.py --yes
```

This preserves the expensive Docling layout and fused-page caches.

Check whether the local CSVs still agree with the current Drive snapshot:

```powershell
venv\Scripts\python.exe esg\scripts\check_drive_csv_sync.py
```

Add `--hash-local` for full local MD5/SHA-256 checks. This reads every active
raw PDF and is slower than the default size and identity audit.

To preflight, back up, rewrite, and then recheck the tracker, file catalog,
and legacy parse index, use `--apply`. The command stops before writing if the
planned rows fail the source and downstream consistency checks. For a changed
PDF, the old catalog row is kept inactive as history and a new active row is
created with lineage IDs cleared until the source-lineage rebuild. The parse
v2, sections, chunks, and QA snapshots are not rewritten by this command:

```powershell
venv\Scripts\python.exe esg\scripts\check_drive_csv_sync.py --apply
```

## Necessary files

**Committed to git** (required):

```
esg/config.py                                  pipeline paths, single source of truth
esg/src/_bootstrap.py, esg/scripts/_bootstrap.py   puts config/src/common on sys.path
common/config.py, common/models.py             shared config loader
requirements.txt                               venv (stages 3-5)
requirements-docling.txt                       venv-docling (stages 1-2, pinned to docling 2.117.0)
.env.template                                  env var names (none required for 1-5)
models/bge-base-en-v1.5-tokenizer/             embedding-window token guard (stage 5)
data/00_reference/companies.csv                ticker → company name
data/00_reference/sustainability_report_tracker.csv   coverage tracker
conftest.py, pytest.ini                        test config
```

**Not committed, must be supplied locally** (see `.gitignore`):

```
data/00_reference/esg_parse_index.csv   source identity/lineage (stage 3 raises TypeError without it)
data/01_raw/sustainability/             source PDFs, per-ticker subfolders
venv/, venv-docling/                    never commit either
```

**Generated by the pipeline** (git-ignored, safe to delete/rebuild):

```
data/02_interim/sustainability/01_docling_layout/   Docling JSON cache (expensive — don't delete casually)
data/02_interim/sustainability/02_esg_text/         fused per-page text
data/02_interim/sustainability/03_pipeline_text/    bridge output (text, pages.csv, headings.csv)
data/03_sections/sustainability/                    section files, one per topic instance
data/04_chunks/sustainability/                      chunk text files
data/00_reference/esg_parse_index_v2.csv            per-document parse record (stage 3 output)
data/00_reference/esg_sections_index.csv            section index (stage 4 output)
data/esg.db                                          optional offline QA evidence
data/00_reference/esg_chunks_index.csv              chunk index — the deliverable (stage 5 output, 47 columns)
```

There is only one active SQLite file: `data/esg.db`. It mirrors the canonical
v2 corpus for read-only QA checks. It is not called by the corpus runner and
must not be treated as a second chunk store or as an application database.
`data/05_embedding/` remains a reserved placeholder.

The current canonical corpus contains 682 documents, 18,707 sections, and
50,510 chunks. Of those chunks, 49,734 are eligible and 776 are retained only
for audit. Every chunk has an exact validated citation slice. Previous live
and versioned outputs are kept only in the recoverable backup under
`data/_pre_promotion_backups/20260810T194256Z_v2_promotion/`.

## Tests

```powershell
venv\Scripts\python.exe -m pytest
```

The complete suite passes: 105 tests passed, 3 skipped, and 70 subtests
passed. The navigation-ribbon behavior, the Best Buy metric-table exclusion,
Drive-sync path handling, and QA loss classification all have regression
coverage.

## Further reading

- [esg/docs/DOCLING_FUSION_PIPELINE.md](esg/docs/DOCLING_FUSION_PIPELINE.md) — full stage-by-stage reference (inputs, outputs, flags, caching rules)
- [esg/docs/DATA_LAYOUT_AND_RUNBOOK.md](esg/docs/DATA_LAYOUT_AND_RUNBOOK.md) — `data/` folder layout and rebuild runbook
- [esg/docs/AI_HANDOFF.md](esg/docs/AI_HANDOFF.md) — quick orientation for picking this repo up cold
- [esg/docs/NEW_REPO_HANDOFF.md](esg/docs/NEW_REPO_HANDOFF.md) — what's needed to extract this pipeline into a standalone repo

## Team

Project Owner: Dr. Ayse Cetinel

Daily Technical Leads: Hanzala, Ibraheem

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md): keep PRs small, run scripts before
pushing, never commit `.env`, notify the team when pushing shared work.
