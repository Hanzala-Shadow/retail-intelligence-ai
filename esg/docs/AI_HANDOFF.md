# AI handoff

## Purpose

This repository runs the Docling-fusion sustainability pipeline. It turns PDF
files into retrieval-ready chunks. It ends at the chunk index. There is no
database, embedding, vector-index, or retrieval-evaluation stage here.

## Data layout

All run data is ignored by Git.

```
data/01_raw/sustainability/                         source PDFs
data/02_interim/sustainability/01_docling_layout/   Docling JSON cache
data/02_interim/sustainability/02_esg_text/         fused page files
data/02_interim/sustainability/03_pipeline_text/    bridge output
data/03_sections/sustainability/                    section files
data/04_chunks/sustainability/                      chunk files
data/00_reference/esg_parse_index.csv               source identity input
data/00_reference/esg_parse_index_v2.csv            fusion output index
```

## Running

To reuse transferred layout and fused output:

```powershell
powershell -ExecutionPolicy Bypass -File esg\scripts\run_docling_fusion_corpus.ps1 -SkipConvert -SkipFuse
```

To run the full corpus, omit both flags. The runner continues automatically:

- conversion reuses complete Docling JSON pairs;
- fusion reuses a page when the file is non-empty, the fusion settings match
  `fused_summary.settings.json`, and the summary holds that page's word counts;
- bridge parsing reuses complete text, sidecars, and v2 index rows whose
  `content_hash` still matches the fused pages behind them;
- sectioning and chunking validate completed output before skipping it.

Changing `--table-mode`, `--table-assign` or `--snap` re-fuses the corpus
instead of mixing renderings, and deleting `fused_summary.json` re-fuses it
too. Both are reported before work starts.

Use `-Force` to rebuild conversion, fusion, and bridge work. Before a true
corpus rebuild after deleting or renaming PDFs, run:

```powershell
venv\Scripts\python.exe esg\scripts\prepare_clean_fusion_run.py --yes
```

That keeps the expensive layout and fused-page caches.

## Environments

`venv-docling` runs conversion and fusion. `venv` runs bridge parsing,
sectioning, and chunking. Do not commit either folder.

## Parallel work

The runner uses all logical CPUs by default for bridge parsing, sectioning,
and chunk planning. Workers write separate documents; the parent process writes
the shared CSV indexes.

## Important limits

- Preserve `esg_parse_index.csv`; it carries source identity and must not be
  overwritten by v2 output.
- Do not commit PDFs, generated data, caches, credentials, `.env`, or virtual
  environments.
- Five heading-ribbon tests are inherited failures from the original checkout.
  They need a separate global navigation-ribbon implementation.
- Chunks are generated artifacts. Do not treat them as approved database,
  embedding, vector, or retrieval data without separate validation.
- `content_hash` in the v2 parse index is the fingerprint of the fused pages a
  row was built from, not the production pipeline's pdfplumber content hash.
  The column was cleared as stale before it was repurposed; nothing else reads
  it. Bridge resume depends on it.
- `fused_summary.json` is the only record of how many words landed in a region.
  A document can convert cleanly and still place almost nothing (FLEXSTEEL-2024
  placed 2%), so treat this file as run evidence, not scratch output.
