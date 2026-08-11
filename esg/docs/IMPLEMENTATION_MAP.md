# ESG pipeline: stage → implementation map

What runs at each stage, which file implements it, and what it is contractually
required to preserve. Written 2026-08-11 against the live corpus
(`esg_docling_fusion_v2` / `esg_chunk_v4`). Every path below was checked to
exist; the stage arguments are taken from the runner
(`esg/scripts/run_docling_fusion_corpus.ps1`), not from prose.

Companion to the 10-K workstream's implementation table. The shapes differ in
one respect worth stating up front: **this pipeline ends at
`data/00_reference/esg_chunks_index.csv`.** There is no staging-load stage and no serving
database. `data/esg.db` is a read-only QA copy, not a serving store.

## Stage map

| Stage | Key implementation | Env |
|---|---|---|
| **1 · Convert** | `esg/scripts/run_docling_gold_spike.py convert` runs Docling layout + TableFormer over each PDF and caches `<stem>.json` and `<stem>.pages.json`. Sharded by `--shards`/`--shard`, `--no-ocr` by default, `--time-budget-min` stops cleanly at a document boundary. The only expensive stage (~1.4 min/doc median). | `venv-docling` |
| **2 · Fuse** | `esg/scripts/run_docling_gold_spike.py fuse` assigns PyMuPDF words into Docling's regions and writes one text file per page. `--table-mode grid` rebuilds tables from TableFormer cell boxes; `_grid_is_coherent` declines incoherent grids and falls back to word order per table. Docling's own text is discarded here. | `venv-docling` |
| **3 · Bridge** | `esg/scripts/bridge_docling_to_pipeline.py` turns fused pages into the per-document layout the rest of the pipeline reads, classifies page roles via `classify_page_role`, and writes `data/00_reference/esg_parse_index_v2.csv`. Reads identity columns from the file catalog lineage and carries them forward untouched. | `venv` |
| **4 · Sections** | `esg/src/section_splitter_esg.py` splits each document at Docling headings into topic sections; `esg/src/esg_compact_toc.py` detects compact contents clusters. Writes one file per section instance plus `data/00_reference/esg_sections_index.csv`. | `venv` |
| **5 · Chunks** | `esg/src/esg_chunker.py` cuts sections into token-bounded chunks (100–600 tokens; 25–99 for `short_evidence`), builds `embedding_text`, and applies the retrieval gates `rag_action` → `include_in_esg_index` → `citation_ready`. `esg/src/esg_year.py` resolves report year. Produces **`data/00_reference/esg_chunks_index.csv`, the deliverable** (47 columns). | `venv` |
| **6 · Summary** | `esg/scripts/summarise_fusion_run.py` — read-only. Documents by quality flag worst-first, chunks by retrieval gate, and documents contributing nothing to the index. | `venv` |

One command runs 1–6: `esg/scripts/run_docling_fusion_corpus.ps1`.

## Two tokenizers, two jobs

`tiktoken` cl100k sizes the chunks. The BGE tokenizer in
`models/bge-base-en-v1.5-tokenizer/` guards the 512-token embedding window — a
chunk whose final `embedding_text` exceeds it **raises** rather than being
silently truncated downstream. Measured header cost: mean 45.3 tokens, max 119,
0 of 4,000 sampled chunks over 512.

## Data contract

Each row of `data/00_reference/esg_chunks_index.csv` retains ticker, report year, section code,
section instance, chunk identifiers, page span, source character offsets, and
four identity columns (`logical_source_id`, `source_version_id`,
`file_alias_id`, `extraction_artifact_id`). Together these trace every chunk
back to a registered source document and to a byte range within it.

Three invariants the pipeline is built to hold:

1. **No character originates from a model.** Docling supplies structure only;
   every chunk body is byte-exact against the PyMuPDF text layer.
2. **Citation spans round-trip.** `source_start_char`/`source_end_char` index
   into the source text and are validated; `citation_validation_status`
   records the result per chunk.
3. **Identity is carried, never re-minted.** Regenerating the identity columns
   would issue new IDs for the same document and break lineage, so stage 3
   copies them across and rewrites only the extraction columns.

## Supporting tooling (not in the six-stage path)

| Purpose | Implementation |
|---|---|
| Clear stale sections/chunks/indexes before a true rebuild | `esg/scripts/prepare_clean_fusion_run.py` |
| Audit local CSVs against the current Drive snapshot | `esg/scripts/check_drive_csv_sync.py` |
| Apply Drive truth to tracker, catalog, and legacy parse index | `esg/scripts/apply_drive_truth_sync.py` |
| Build a self-contained chunk handout | `esg/scripts/build_chunk_handoff.py` |
| Build the read-only QA database (`data/esg.db`) | `esg/scripts/build_esg_qa_db.py`, which calls `esg/src/drive_to_db.py` for its load plan |
| Corpus-level QA, checkpoints 0–5 (Q5–Q28) | `esg/scripts/esg_database_tiers_2/checkpoint*.py`, run against a SHA-256-frozen snapshot |

## Two virtualenvs, on purpose

Docling pulls torch and transformers (~1.5 GB) that stages 3–6 do not need. The
runner calls each stage with an explicit interpreter: `venv-docling` for stages
1–2, `venv` for 3–6. Both Python 3.13.2.

## Notes for anyone comparing this with the 10-K table

- **No embedding or index stage here.** Corpus vectors exist as an offline
  artifact under `data/05_embedding/`, produced outside this repo; the pipeline
  itself ends at the chunk index and has no vector store.
- **No staging load and no cutover.** There is no serving database to gate, so
  there is no rollback step. The equivalent safety mechanism is the retrieval
  gate inside stage 5 plus the read-only QA checkpoints.
