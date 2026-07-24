# ESG Pipeline — End-to-End Overview

**Verified against live data on 2026-07-20.** Every count below was read directly from the
files in `data/`, not copied from a prior report. Diagram: `docs/esg_pipeline_overview.svg`.

This document explains *what the pipeline is and why each stage exists*. For the exact
commands see `docs/ESG_PIPELINE.md` (deterministic core) and `scripts/VLM_RUNBOOK.md`
(VLM stages). For the programme's open questions see
`reports/FINAL_PROGRAMME_REPORT_2026-07-20.md`.

---

## 1. What the pipeline does

It turns a folder of published corporate sustainability report PDFs into a
**citation-gated retrieval corpus**: every chunk that reaches the embedding team can be
traced back to an exact character span of an exact page of an exact source PDF, and any
chunk whose reading order cannot be defended is withheld rather than shipped.

The governing design principle is **fail-closed**. At three separate points the pipeline
prefers to *exclude* content it cannot vouch for over shipping content that might be
scrambled. That is why 17,068 of 44,054 manifest rows are excluded — that number is the
pipeline working, not failing.

Scale: **124 tickers → 481 parsed documents → 28,249 physical pages → 39,575 parser chunks
+ 4,609 VLM chunks → 26,986 retrieval-eligible chunks.**

---

## 2. The stages

### Stage 0 — Acquisition
`src/drive_downloader.py` → `data/01_raw/sustainability/{ticker}/*.pdf`

Pulls PDFs from the team Google Drive against
`data/00_reference/sustainability_report_tracker.csv`. Skips a local file only when it is
non-empty *and* matches Drive's size and MD5, so a truncated download self-heals on the
next run. Checkpointed manifest: `esg_drive_manifest.csv`.

Scanned reports are handled **out-of-band**: `src/ocr_pdf.py` builds a searchable PDF which
is uploaded back to Drive *under the same filename*. The parser never runs OCR itself. The
raw filename stays the canonical identity; the parse index records the actual extractor
input separately in `parse_source_*` fields.

Source policy decisions that must survive re-download live in
`data/00_reference/esg_source_registry.csv` (e.g. ETSY 2024 admitted as
`annual_report_with_esg`, `source_scope=excerpt`).

### Stage 1 — Parse
`src/pdf_parser.py` → `data/02_interim/esg_text/{ticker}/{stem}.txt` + `.pages.csv`
Index: `esg_parse_index.csv` — **481 rows, all `parsed`, 0 failed.**

This is the hardest stage, because ESG reports are magazine-designed, not filed documents.
The extraction policy is hybrid:

1. `pdfplumber` is the default extractor.
2. On a clear 2–4 column page, text is **rebuilt from word coordinates** in left-to-right
   column order. The reconstruction is accepted only if every word is preserved and the
   column starts are stable and vertically overlapping.
3. `pypdfium2` is tried automatically when pdfplumber output shows low text density, low
   page coverage, or `(cid:...)` artifacts.
4. Pages with grid/tile risk or irregular coordinate structure are **recorded, not
   guessed at** — they get handed to the layout gate in Stage 4.

The simple-column logic came from `layout_v4`; the active audit is `layout_v5`, which adds a verified-table gate. Two v4 fixes
are load-bearing and should not be "improved" casually — both were measured:
- the metric regex could not match `%` (a `\b` after a non-word char never matches), so
  percentage-dense tables never fired the grid signature;
- column starts now use each column's **minimum** stable left edge, not the median. The
  median silently stole ragged-left-edge words (bullets) into the previous column —
  1,886 words across 170 pilot pages, 1,348 of them on already-live pages.

A parse writes a page map (`page, char_start, char_end, char_count`) beside every text
file. This is what makes citation possible downstream.

### Stage 2 — Section
`src/section_splitter_esg.py` → `data/03_sections/esg/...`
Index: `esg_sections_index.csv` — **22,807 sections.**

ESG headings are not standardised, so splitting uses conservative regex heuristics and
falls back to `full_document` when it cannot find reliable headings. A `section_code` is a
*topic label, not a unique location* — repeated topics get distinct instance IDs
(`community__0001`, `community__0002`).

Invariant enforced exactly: `section_text == parsed_text[source_start_char:source_end_char]`.

### Stage 3 — Chunk
`src/esg_chunker.py` → `data/04_chunks/esg/...`
Index: `esg_chunks_index.csv` — **39,575 chunks = 38,102 `normal` + 1,473 `short_evidence`.**

Normal chunks are bounded `100 ≤ tokens ≤ 600`. Real disclosures below that floor (compact
metric statements, captions, brief governance notes) are preserved as a single
`short_evidence` chunk when `25 ≤ tokens < 100`. Table-of-contents and navigation fragments
are deliberately left unchunked.

Every chunk is citation-validated (`semantic_v1`). `citation_ready=true` is **derived, not
asserted**: the chunk text must match its declared parsed-text slice, the slice must sit
inside its section, the parsed-text fingerprint must match, and the pages must resolve.
Only `verified_exact` and `verified_whitespace_normalized` count.

### Stage 4 — Layout QA (the first gate)
`src/esg_layout_qa.py` → `data/00_reference/esg_page_layout_qa.csv`
**Historical snapshot: 28,249 pages, all `layout_v4`. Rebuild before quoting `layout_v5` counts.**

| decision | pages | meaning |
|---|---:|---|
| `auto_pass` | 13,752 | ordinary single-flow page |
| `auto_pass_column_order_reconstructed` | 9,090 | multi-column, live text matches the coordinate reconstruction **exactly** |
| `auto_hold` | 5,407 | reading order cannot be defended |

Read-only — it never edits text, sections, or chunks. It only labels pages. The dominant
hold reason is `structural_grid_or_table_layout` (2,650 pages), then
`navigation_contents_layout` (376), then a long tail of `column_vertical_coverage` values
just under the 0.28 threshold.

Held pages are **stored and citation-traceable but excluded from retrieval**. The rationale:
on a borderless GRI/SASB disclosure table, column-major reading detaches every row's label
from its value, so the text is confidently wrong — worse than absent.

### Stage 5 — VLM branch (the second gate, and a recovery path)
`src/esg_vlm_stage.py` + `scripts/run_esg_vlm.py` + `scripts/build_esg_vlm_chunks.py`
→ `data/04_vlm/vlm_chunks_index.csv` — **4,609 VLM chunks.**

Added 2026-07-20. It does two different jobs with one model (`gpt-5-mini-2025-08-07`,
prompts pinned by sha256, cached on source-sha + page + model + prompt):

- **Classify** the 9,078 *passing* reconstructed pages. This exists because the layout gate
  has a measured recall gap: roughly 20.4% of reconstructed-and-passing pages are actually
  table-dominant, i.e. scrambled in live retrieval. The classifier flagged 1,976 (21.8%,
  in band). Their parser chunks are then excluded.
- **Extract** held table pages and flagged pages into page-level markdown, recovering
  content the deterministic path had to withhold.

The chunk builder **inherits** section assignment from the existing manifest page ranges —
the sectioner is never re-run and the protected baseline is untouched. Numbers carry a
corroboration annotation (`n_text_corroborated`, `graphic_only_count`,
`body_uncorroborated_count`): the digit screen is an **annotator, never a gate**, because
graphic-only numbers are frequently the only published form of the data.

### Stage 6 — Vector manifest (the shipping decision)
`scripts/build_esg_vector_manifest.py` → `data/00_reference/vector_index_manifest.csv`
**44,054 rows → 26,986 eligible / 17,068 excluded.**

This file is the pipeline's product — it is what the embedding/retrieval team consumes.

| outcome | rows | |
|---|---:|---|
| **eligible** — parser chunks | 22,399 | |
| **eligible** — VLM chunks | 4,587 | lineage `vlm_extraction_v1` |
| excluded — page held by layout QA | 12,381 | Stage 4 |
| excluded — VLM classifier called the page table-dominant | 4,381 | Stage 5 |
| excluded — `include_in_esg_index=false` | 264 | document-level policy |
| excluded — duplicate | 22 | |
| excluded — navigation chunk | 20 | |

Nothing is deleted. Exclusion is a manifest-level decision, so it is fully reversible and
every excluded chunk remains on disk and auditable.

### Stage 7 — Verification
- `src/esg_pipeline_qa.py` → `esg_pipeline_qa.csv`, reconciles tracker ↔ PDF ↔ text ↔
  sections ↔ chunks and prints priority fixes in a fixed order.
- `scripts/validate_esg_provenance.py` → strict provenance check.
- `pytest tests/` → full suite, green (125 tests as of 2026-07-20; the count grows with the pipeline — trust the run, not this line).
- `src/drive_to_db.py --dry-run` / `--commit` → PostgreSQL load.

---

## 3. Order of operations

```
scripts\run_esg_pipeline_fast.cmd          # A: intake → parse → remediation → section → chunk → layout → VLM decision → QA → manifest → validate → tests
scripts\run_esg_vlm.py classify --transport batch --wait  # B: optional paid work; approval required
scripts\run_esg_vlm.py extract  --transport batch --wait  # B: optional paid work; approval required
scripts\build_esg_vlm_chunks.py
scripts\run_esg_pipeline_fast.cmd -Stage vlm -EnableVlmIntegration -WhatIf  # C: no-write preview
scripts\run_esg_pipeline_fast.cmd -Stage vlm -EnableVlmIntegration          # D: approved local integration
```

A before B because the optional VLM stages read the layout-QA table. B must be
checked before C or D. The fast runner never starts paid VLM work. Its default
run still rebuilds the manifest and keeps unsafe pages held. Preview the full
run with `scripts\run_esg_pipeline_fast.cmd -Stage all -WhatIf`. Preview one
report with `-Ticker TICKER -PdfFile "report.pdf" -WhatIf`. An unscoped
`-Force` is blocked. The runner does not download or change Drive files, load a
live database, run migrations, or create embeddings.

---

## 4. Known limitations — state these whenever the corpus is presented

1. **~1,852 pages (95% CI 1,473–2,300) are table-dominant but currently pass**, i.e.
   scrambled in live retrieval. The VLM classifier addresses most of this, but the residual
   is real. This was the programme's top-ranked defect: confident-wrong beats absent.
2. **No gate can detect misordering.** `preservation_ratio` compares multisets, so it is
   blind to order by construction. An attempt to gate on authored content-stream order was
   measured and rejected — it is confounded and its discrimination is backwards. Do not
   retry without human-ordered ground truth first; that is a labelling exercise.
3. **Protected-baseline drift is unreconciled**: the approved 5-report baseline is 447
   sections, the live parser produces 537 (v4 improved it from +128 to +90). Identities must
   not be frozen until Ayse approves a baseline. No engineering closes this.
4. **The Task 0 rotated-text fix is committed but DORMANT.** Any `-Force` rebuild silently
   promotes it (~563 pages affected, 504 would newly flip to held). Bundle that deliberately.
5. **Manifest is one rebuild behind**: it covers 478 documents while the parse index now has
   481. Rerun stage C to pick up the three newer documents.
6. Website-only ESG sources (DDS, RVLV, RH, EVGO) stay `not_found` for this pipeline by
   design — they are a different source class and must not be mixed into PDF-derived chunks.

---

## 5. Evidence trail

The claims above are backed by, in order of authority:
`reports/FINAL_PROGRAMME_REPORT_2026-07-20.md` (programme close),
`reports/BRIEF_FOR_FABLE_NEXT_2026-07-20.md` (full operational state),
`reports/esg_layout_v4_promotion_2026-07-17.md` (the rebuild),
`reports/esg_table_grid_solution_2026-07-17/gold_labels_final.csv` (449-page gold set,
binary κ=0.962, personally reviewed by the owner in full),
`reports/esg_tagged_pdf_ground_truth_2026-07-17/` (publisher struct-tree ground truth:
reconstruction agrees at median 0.884, beats native row order on 75.1% of pages).
