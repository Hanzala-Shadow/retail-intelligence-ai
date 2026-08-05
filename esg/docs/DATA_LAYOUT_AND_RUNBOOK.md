# Sustainability data layout and runbook

All non-source pipeline data belongs under `data/` and is ignored by Git.

```
data/
  00_reference/
    esg_parse_index.csv                 source identity input (copied, not generated)
    esg_parse_index_v2.csv              fusion parse result
    esg_sections_index.csv              section result
    esg_chunks_index.csv                chunk deliverable
  01_raw/sustainability/                source PDFs
  02_interim/sustainability/
    01_docling_layout/                  Docling *.json and *.pages.json cache
    02_esg_text/                        fused <stem>_p<N>.txt pages
    03_pipeline_text/                   bridge document text and sidecars
  03_sections/sustainability/           section files
  04_chunks/sustainability/             chunk files
  05_db/                                reserved; no database writer is included
  05_embedding/                         reserved; no embedding writer is included
```

## Required inputs before the bridge stage

Copy these files before stages 3-5:

- `data/00_reference/esg_parse_index.csv` for source identity and lineage.
- `data/01_raw/sustainability/` for source-PDF hashes.
- `data/00_reference/companies.csv` and `sustainability_report_tracker.csv`.
- `models/bge-base-en-v1.5-tokenizer/` for the embedding-window check.

## Reuse transferred Docling work

With layout and fused pages already in the numbered interim folders, run:

```powershell
powershell -ExecutionPolicy Bypass -File esg\scripts\run_docling_fusion_corpus.ps1 -SkipConvert -SkipFuse
```

This starts at the bridge and populates `03_pipeline_text`, sections, chunk
files, and the three output indexes. It does not need `venv-docling`.

To reuse layout JSON but regenerate fused pages, omit `-SkipFuse`. To convert
new PDFs too, omit both switches.

## Continuation

Every stage resumes by default, and each one checks that what it is about to
skip still matches what produced it.

Conversion checks for both complete Docling JSON files. Fusion reuses a page
only when the file is non-empty, the run's fusion settings match the ones
recorded in `fused_summary.settings.json`, and the summary still holds that
page's word counts. Bridge parsing reuses a document only when its text, page
map, heading sidecar and v2 index row exist *and* the fused pages still hash
to the `content_hash` recorded on that row. Sections and chunks validate their
own files and index rows before skipping.

Two consequences worth knowing:

- Changing `--table-mode`, `--table-assign` or `--snap` re-fuses the whole
  corpus rather than mixing two renderings. That is the point; it is not a
  failure. The change is reported before any work starts.
- Deleting `fused_summary.json` re-fuses everything, because a page with no
  word counts cannot be vouched for. Fusion is a minutes-scale stage, so this
  is a cost worth paying rather than trusting an unverifiable page.

Use `-Force` to rebuild Docling conversion, fusion, and bridge parsing. For a
true corpus rebuild after PDFs were removed or renamed, run the clean command
below first; `-Force` does not remove stale documents.

## CPU use

Keep Docling conversion at `-Workers 1`: Docling already uses the available
CPU cores internally, and additional converter processes slow it down or can
run out of memory.

The runner uses every logical CPU for bridge parsing, sectioning, and stage-5
chunk planning by default. Workers handle independent documents or sections;
the parent process remains the only writer of each shared index. Set
`-BridgeWorkers N`, `-SectionWorkers N`, or `-ChunkWorkers N` only when you
need to leave capacity for other work.

The bridge and section stages write independent document files in worker
processes. Their parent process serializes shared index updates, so parallel
work does not race or corrupt the generated corpus.

## Rebuild safely

Before a full stages 3-5 rebuild, run:

```powershell
venv\Scripts\python.exe esg\scripts\prepare_clean_fusion_run.py --yes
```

It deletes bridge text, sections, chunks, and derived indexes. It never deletes
the `01_docling_layout` or `02_esg_text` cache folders.

## Database, embeddings, and retrieval

Do not populate `05_db` or `05_embedding` yet. This repository ends at
`esg_chunks_index.csv`. It has no database schema/writer, embedding job, vector
index, or retrieval evaluation. Adding any of those requires a separate,
explicit design and validation step; do not treat the chunk index as a live
retrieval corpus.
