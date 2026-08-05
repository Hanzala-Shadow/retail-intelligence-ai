# Docling-Fusion Pipeline

PDF in, retrieval-ready chunks out. Docling decides where the regions are and
what order they read in; PyMuPDF supplies the words that fill them. Neither tool
does the whole job alone: docling's own text is discarded, and PyMuPDF has no
idea what a column or a table is.

One command runs the whole chain:

```
powershell -ExecutionPolicy Bypass -File esg\scripts\run_docling_fusion_corpus.ps1 -TimeBudgetMin 200
```

Docling cache and fused pages land under `-WorkRoot` (default
`outputs/docling_run4h`). The bridge, sections, chunks, and indexes land in
`data/`.

## Two virtualenvs, on purpose

Docling pulls torch and transformers, roughly 1.5 GB, which the production
pipeline does not want in its own environment. The runner calls each stage with
an explicit interpreter:

| Env | Built from | Runs |
| --- | --- | --- |
| `venv-docling` | `requirements-docling.txt` | stages 1-2 (convert, fuse) |
| `venv` | `requirements.txt` | stages 3-5 (bridge, sections, chunks) |

Both are Python 3.13.2. Run from the repo root in PowerShell or cmd — backslash
paths fail under Git Bash.

## Inputs the run needs

| Path | In git? | Why it is needed |
| --- | --- | --- |
| `outputs/.../input_pdfs/*.pdf` | no (3.4 GB) | the corpus itself |
| `data/00_reference/esg_parse_index.csv` | no (363 KB) | carries source identity IDs (see stage 3) |
| `data/01_raw/sustainability/` | no | source PDFs for hashing synthesised rows |
| `data/00_reference/companies.csv` | yes | ticker to company name |
| `data/00_reference/sustainability_report_tracker.csv` | yes | coverage tracker |
| `models/bge-base-en-v1.5-tokenizer/` | yes | token counting in stage 5 |

The two "no" entries must be copied alongside the branch. Without the parse
index stage 3 raises a `TypeError` rather than degrading.

---

## Stage 1 — convert

**Script** `esg/scripts/run_docling_gold_spike.py convert` (venv-docling)

Runs docling's layout and TableFormer models over each PDF and caches the
result. This is the only expensive stage — roughly 1.4 min/document median,
2.1 mean. Everything after it is minutes.

| File | Role |
| --- | --- |
| `input_pdfs/*.pdf` | in — source documents |
| `work/docling_json/<stem>.json` | out — docling's full document export |
| `work/docling_json/<stem>.pages.json` | out — regions grouped by page, what stage 2 reads |
| `logs/convert_shard*.log` | out — per-shard stdout |

**Caching.** A document counts as converted only when *both* JSON files exist
and are non-empty. Both are written to a `.tmp` name and renamed into place, so
an interrupted run leaves either the complete old file or nothing. Stopping the
run and re-running it is safe; a half-written pair is deleted and reconverted.

**OCR is off by default.** It costs ~80% of convert time (9.91 → 2.04 s/page)
and changes nothing, because fusion takes its words from PyMuPDF and throws
docling's text away. The exception is a PDF with no text layer at all, where
neither tool has anything to read.

**Text-density report.** After converting, the stage scans the whole cache and
names documents whose words-per-page is too low to be real. Below 60 is a
document PyMuPDF cannot read either (`NEEDS OCR`); 60-100 is worth a look
(`review`). Thresholds come from this corpus: median 330 w/pg, 10th percentile
212, sparsest legitimate document 75.5. It reports and never acts — reconvert
the named documents with `-WithOcr` yourself.

**Table quality is already maximal.** docling 2.117.0 defaults to
`TableFormerMode.ACCURATE` with cell matching on, and the script overrides only
`do_ocr`.

---

## Stage 2 — fuse

**Script** `esg/scripts/run_docling_gold_spike.py fuse` (venv-docling)

The fusion proper. For each page, docling's regions define the boxes and the
reading order; PyMuPDF's words are assigned into them.

| File | Role |
| --- | --- |
| `work/docling_json/<stem>.pages.json` | in — regions and reading order |
| `input_pdfs/*.pdf` | in — re-opened for word positions |
| `work/fused/<stem>_p<N>.txt` | out — fused text, one file per page |
| `work/fused_summary.json` | out — per-document word placement counts |

`--pdf-dir` is not optional. Without it the stage silently processes only what
it can find and still prints a clean summary.

**`--table-mode grid`** rebuilds a table from TableFormer's cell boxes instead
of emitting its words in reading order. Measured over 86 documents: 1,086 of
1,139 tables (95%) have a coherent grid, pipe-delimited rows go from 242 to
10,603, and no word is lost — the extra tokens are delimiters. Incoherent grids
are declined by `_grid_is_coherent` and fall back to words automatically, so
this degrades per table rather than per corpus. Without the flag the stage
reverts to flattened words.

**`--table-assign cell`** (the default) decides which grid slot each word
belongs to using docling's own cell boxes, smallest containing cell wins.

---

## Stage 3 — bridge

**Script** `esg/scripts/bridge_docling_to_pipeline.py` (venv)

Turns fused pages into the file layout the rest of the pipeline already reads,
and produces a v2 parse index.

| File | Role |
| --- | --- |
| `work/fused/*.txt` | in — fused page text |
| `data/00_reference/esg_parse_index.csv` | in — **source identity**, see below |
| `data/01_raw/sustainability/` | in — PDFs for hashing synthesised rows |
| `interim/esg_text/<TICKER>/<stem>.txt` | out — whole document text |
| `interim/esg_text/<TICKER>/<stem>.pages.csv` | out — page boundaries and roles |
| `interim/esg_text/<TICKER>/<stem>.headings.csv` | out — docling headings, what stage 4 splits on |
| `esg_parse_index_v2.csv` | out — per-document parse record |

**Why the production parse index is required.** Not for parsing — this pipeline
parses the PDFs itself. It carries identity columns that describe the *source
PDF* rather than the parser: `logical_source_id`, `source_version_id`,
`file_alias_id`, `extraction_artifact_id` and the source hashes. Those tie a
chunk back to a registered document in the database. Regenerating them would
mint new IDs for the same document and break lineage, so they are copied across
untouched and only the extraction columns are rewritten.

Documents the production pipeline never parsed get a synthesised row instead of
being dropped — without this the v2 corpus silently loses every document outside
the production index.

**`classify_page_role`** lives here (`bridge_docling_to_pipeline.py:212`) and
labels each page `content` / `toc` / `divider` / `cover` / `blank`. Note this is
a *different* classifier from `esg/src/esg_page_role.py`, which belongs to the
legacy parser path and is not used by this pipeline. A page with five or more
complete sentences is always `content`, whatever else shares it — that rule
exists because contents lists sometimes sit on the same page as real prose.

---

## Stage 4 — sections

**Script** `esg/src/section_splitter_esg.py` (venv)

Splits each document into topic sections at docling's headings.

| File | Role |
| --- | --- |
| `interim/esg_text/**` | in — text, pages, headings |
| `sections/esg/<TICKER>/<stem>__<topic>__<NNNN>.txt` | out — one file per section instance |
| `sections_index.csv` | out — one row per section, with page and char bounds |
| `esg/src/esg_compact_toc.py` | helper — detects compact contents clusters |

This repository uses the heading-first sectioner directly. There is no legacy
splitter fallback.

`section_code` is a reusable topic category; `section_instance_id` identifies
one contiguous occurrence, so a document may validly hold both `community__0001`
and `community__0002`.

---

## Stage 5 — chunks

**Script** `esg/src/esg_chunker.py` (venv)

Cuts sections into token-bounded chunks, builds the embedding text, and applies
the retrieval gates.

| File | Role |
| --- | --- |
| `sections/esg/**` | in — section text |
| `sections_index.csv` | in — section metadata and bounds |
| `esg_parse_index_v2.csv` | in — document quality and identity |
| `models/bge-base-en-v1.5-tokenizer/` | in — embedding-window guard |
| `chunks/esg/<TICKER>/<stem>__<topic>__<NNNN>__chunk_<NNNN>.txt` | out — chunk text |
| `chunks_index.csv` | out — **the deliverable**, one row per chunk, 47 columns |
| `esg/src/esg_year.py` | helper — report year from filename and text |
| `esg/src/esg_compact_toc.py` | helper — contents-cluster detection |

**Two tokenizers, two jobs.** `tiktoken` cl100k sizes the chunks (100-600
tokens, 25-99 for `short_evidence`). The BGE tokenizer guards the embedding
window — a chunk whose final embedding text exceeds 512 BGE tokens raises rather
than being silently truncated at embed time.

**`embedding_text`** is built here and stored, not computed later. It is a
metadata header followed by the byte-exact chunk body:

```
Company: ...
Ticker: ...
Document: ...
Reporting year: ...
ESG topic: ...
Subsection: ...
Content type: ...

<chunk text, unmodified>
```

The body is byte-exact by design — `source_start_char` / `source_end_char` index
into the source text, and citation validation checks that the span round-trips.

**Retrieval gating** runs `rag_action` → `include_in_esg_index` →
`citation_ready`. `--workers` parallelises chunk planning; all file and index
writes stay in the parent process.

---

## Stage 6 — summary

**Script** `esg/scripts/summarise_fusion_run.py` (venv)

Read-only. Prints documents by quality flag worst-first, chunks by retrieval
gate so the drop from produced to indexable is visible, and the documents
contributing nothing to the index.

It exists because "0 failed" says almost nothing: FLEXSTEEL-2024 passed the
smoke test with no error while 98% of its words landed in no region.

---

## Rebuilding cleanly

Sectioning and chunking **upsert**. Re-running without clearing leaves stale
rows from removed or renamed documents in place, and the index merges new rows
into old ones — the counts look plausible and are wrong.

Before rebuilding stages 2-5, delete:

```
outputs/<run>/sections/          outputs/<run>/sections_index.csv
outputs/<run>/chunks/            outputs/<run>/chunks_index.csv
outputs/<run>/interim/           outputs/<run>/esg_parse_index_v2.csv
```

**Never delete `work/docling_json/`** — that is the convert cache and rebuilding
it costs hours. Re-run with `-SkipConvert` to reuse it.

`esg/scripts/prepare_clean_run.py` does this job for the production `data/` tree
but does not cover this pipeline's work root.

## Supporting files

| File | Role |
| --- | --- |
| `esg/config.py` | all pipeline paths in one place |
| `esg/src/_bootstrap.py` | puts `config`, `src` and `common` on the import path |
| `esg/scripts/PipelinePaths.ps1` | path helpers for PowerShell callers |
| `requirements.txt` | production venv |
| `requirements-docling.txt` | docling venv, fully pinned |
| `.env.template` | env var names; none are needed for stages 1-5 |

## What this pipeline does not do

There is no database stage. `esg/src/drive_to_db.py` exists and its `--dry-run`
works against these outputs, but it is not wired into the runner and has no
columns for `embedding_text`, `retrieval_tier`, `page_role` or
`include_in_esg_index`. There is no embedding stage and no retrieval evaluation.
The pipeline ends at `chunks_index.csv`.
