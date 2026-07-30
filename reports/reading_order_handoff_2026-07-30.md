# Reading-order problem — handoff brief

**Date:** 2026-07-30
**Repo:** `C:\Users\Aziz\Documents\ChatGPT Codex\retail-intelligence-ESG-works`
**Purpose:** everything known about the ESG reading-order defect, so a fresh
session can continue without re-deriving it. Includes measured results, code
locations, and — importantly — the approaches already tried and disproven.

---

## 1. Corpus state (measured, not assumed)

| Quantity | Value |
|---|---|
| Documents | 202 |
| Pages audited | 11,593 (100% of every doc's `page_count`, no truncation) |
| Chunks | 16,433 |
| Chunker version | `esg_chunk_v2` |
| Embedding model | BAAI/bge-base-en-v1.5, 512-token limit |

**Open discrepancy:** the 2026-07-21 clean reparse was recorded as 500 documents
/ 39,946 chunks. This folder holds 202 / 16,433. Either this is a subset or the
corpus changed. Aziz deferred this question. Every population number below is
scaled to the 202-doc corpus — re-check them if the full corpus is larger.

### Layout-QA decision distribution (`data/00_reference/esg_page_layout_qa.csv`)

| Decision | Pages | Share |
|---|---|---|
| `auto_pass` | 4,795 | 41.4% |
| `auto_pass_column_order_reconstructed` | 3,005 | 25.9% |
| `auto_hold` | 1,642 | 14.2% |
| `auto_pass_region_order_reconstructed` | 1,106 | 9.5% |
| `auto_pass_verified_table_extraction` | 909 | 7.8% |
| `auto_pass_navigation_contents` | 136 | 1.2% |

`auto_hold` is the only route to VLM review.

---

## 2. The core defect: the gate cannot fail

### 2.1 `preservation_ratio` is a tautology

`src/esg_reading_order.py`, `reconstruct_column_order()` (lines ~241–341).
The relevant code is lines 315–320:

```python
source_counter = Counter(... for word in usable)
reconstructed_counter = _word_counter(text)
preserved = sum(min(count, reconstructed_counter.get(token, 0)) ...)
preservation_ratio = preserved / len(usable)
```

`text` is built by partitioning **that same `usable` list** into
header / columns / footer and concatenating. Every word lands in exactly one
bucket and all buckets are joined, so the word multiset of `text` is identical
to `usable` **by construction**.

- **3,004 of 3,005 pages score exactly 1.0000.**
- `MIN_PRESERVATION_RATIO = 0.995` (line 30) is therefore a no-op gate.

It is a permutation checksum: it proves no word was lost while rearranging, and
is mathematically incapable of detecting wrong order or a wrong column count.
All real correctness rests on `_stable_column_starts()`, which nothing validates.

### 2.2 The same flaw on the table path

`auto_pass_verified_table_extraction` gates on `table_token_recall`, a
bag-of-tokens measure. **864 of 909 pages score exactly 1.0.** Order-blind by
construction, so a chart whose axis labels all survive passes at 1.0.

### 2.3 The gold set already contained the counter-evidence

`data/00_reference/esg_layout_gold_labels.csv` — 449 pages, two annotators plus
adjudication. On pages the audit **auto-passed at `preservation=1.0000`**, the
annotators had written:

- **NKE p162** — "column-major reading would detach row labels"
- **MELI p79** — "would badly scramble if read column-major"
- **JWN p41** — "column-major reading would detach rows"
- **MUSA p28** — "column-major reading would detach [labels from data]"
- **KMX p53** — "row context would detach"
- **CASY p54** — "reconstruction concatenates same-row content across all 5 SDG columns"

**35 of 46 (76%) joinable human-labelled `table_dominant` pages were auto-passed.**

---

## 3. The four distinct failure modes

These are **different bugs**. Treating them as one is the main analytical trap.

### Mode A — row-structured content read column-major
Unruled tables, GRI/SASB/TCFD disclosure indices, metric grids. A table's
column left-edges look exactly like prose column starts, so column
reconstruction fires and every row label is detached from its values.
**Status: detector built and validated (§4). 736 pages affected.**

### Mode B — wrong column count
`_stable_column_starts()` returns the wrong number of columns.
- **BBWI 2023 p22**: detected **2** edges (`[34, 587]`) on a page with roughly
  **4** columns. Aziz's review: *"three columns are read like one. the fourth one
  is read correctly."* Under-detection.
- **BBWI 2023 p36**: detected **4** edges (`[34, 218, 413, 586]`). Review:
  *"reads two columns as one."*

`preservation_ratio` is blind to column count, so both pass at 1.0.
**Status: diagnosed, NOT fixed.** Proposed fix: recursive gutter detection —
after finding a column, look *inside* it for persistent vertical whitespace
bands. If present, the count is wrong.

### Mode C — mid-page full-width heading torn across columns
`HEADER_FOOTER_BAND_SHARE = 0.06` (line 29) hoists only the top 6% of page
height. A full-width heading sitting mid-page, above a band of columns, is
assigned to whichever single column its `x0` lands in (line ~307); the other
columns lose it. This is Aziz's original complaint: *"the overarching column on
top gets splited and parts of it gets to each bottom collumn."*

**Status: mechanism confirmed in code, NOT fixed.** Proposed fix: segment the
page into horizontal bands at each full-width line, then reconstruct columns
**within each band**. Depends on Mode B being fixed first — band detection needs
correct column edges.

### Mode D — charts (adjacent problem, separate track)
pdfplumber extracts text; bar/line geometry is not text, so only axis labels
survive. Aziz: *"the graph is parsed as dates and percentages only."* Only
`auto_hold` reaches the VLM, so chart-heavy auto-passed pages never escalate.
See §7 — largely explored, small population, low priority.

---

## 4. What has been built and validated

### `src/esg_row_structure.py` (committed)

Detects Mode A. Read-only; measures word boxes, rewrites nothing. Reuses the
pipeline's own `_stable_column_starts()` so results are directly comparable.

**Gate feature:** `p25_fill` — the 25th percentile of per-segment fill ratio
(segment width ÷ its column's observed content width). Table cells leave a tail
of very short segments; prose lines nearly fill their column.

**Threshold:** `MAX_P25_FILL = 0.2455`

**Performance** (5-fold CV × 6 repeats, thresholds refitted inside each training
fold, on the 135 joinable gold prose/table pages):

| | value |
|---|---|
| Recall on `table_dominant` | **71%** |
| False positive on `prose` | **8%** |
| In-sample (optimistic) figure | 83% / 13% |

Current gate catches **0%** of these, so this is 0 → 71%.
The single-feature rule **beat every two-feature combination** once thresholds
were not fitted on the test data.

### Corpus application

Applied to all 3,005 `auto_pass_column_order_reconstructed` pages:

| Result | Pages |
|---|---|
| `row_structured` (should NOT be read column-major) | **736 (24.5%)** |
| `column_safe` | 2,256 |
| `not_applicable` | 13 |

Output: **`reports/rowcheck_review_queue.csv`** — one row per flagged page,
sorted worst-first, with ticker, pdf, page, current decision,
`preservation_ratio`, and all feature values.

⚠️ **Ignore the `full_width_span_count` column in that file** (reports 1,716
pages). The span detector inherits the bad column edges from Mode B, so it fires
on ordinary prose. Only the 736 `row_structured` figure is trustworthy.

### The proposed Mode A fix, and why it is cheap

For a row-structured page the **natural extraction order is already correct** —
a table read across each row preserves label↔value association. The pipeline
*creates* the defect by reordering. So the fix is to **skip the reorder** for
flagged pages, not to add processing.

**Canonical demonstration — MELI-MERCADOLIBRE-2023 p79** (GRI index):

BEFORE (in the corpus now, `preservation_ratio = 1.000000`):
```
GRI 203: Indirect 203-1 Investment in infrastructure
economic impacts and supported services
...
GRI 401: 401-1 New employee hires and
Employment 2016 employee turnover

ANSWER OR OMISSION SDG PAGE
During the reporting period we did not make significant investments...
```

AFTER (natural order):
```
GRI STANDARD CONTENT   ANSWER OR OMISSION   SDG   PAGE
GRI 203: Indirect 203-1 Investment in infrastructure  During the reporting
  period we did not make significant investments in infrastructure...  8
GRI: 408: Child 408-1 Operations and suppliers at  We identified no suppliers
  with a significant risk of cases of child labor during the period.  8
```

**Honest limitation:** AFTER is materially better, **not clean**. Multi-line
cells still wrap awkwardly and some values land on neighbouring lines. Natural
order restores row *association*; it does not reconstruct a tidy table.

**Risk to check before applying to all 736:** on a genuine prose page, skipping
the reorder *reintroduces* column interleaving. The 8% false-positive rate
therefore does active harm. Sample ~15 flagged pages that look like prose and
confirm before bulk-applying.

Where the text lives, for building before/after comparisons:
- `data/00_reference/esg_parse_index.csv` → `parsed_text_file`, `page_map_file`
- page map gives `char_start` / `char_end` per page into the parsed text
- BEFORE = `parsed_text[char_start:char_end]`
- AFTER = `pdfplumber` `page.extract_text()`

---

## 5. DEAD ENDS — do not repeat these

This section is the highest-value part of this document.

### 5.1 Band co-occupancy — **AUC 0.511 (noise)**
Hypothesis: tables share horizontal bands across columns more than prose does.
**False.** Prose median 0.333, table median 0.407 — indistinguishable. Equal
leading aligns prose lines just as well. Retained in
`esg_row_structure.py` as reported evidence only, never as a decision input.

### 5.2 Edge-crossing rate — **AUC 0.539 (noise), and structurally broken**
Hypothesis: if text lines cross a claimed gutter, it is not a gutter.
Sound in principle, **impossible as implemented**: `_line_segments()` splits
lines at those very gutters (`gap_threshold = max(16.0, page_width * 0.018)`)
*before* anything can be measured, so segments can never cross an edge. This
repeats the exact circularity of `preservation_ratio`. Measured crossing rates
on known-bad pages: BBWI p22 = 0.000, p36 = 0.039, AMZN p62 = 0.074.

**If retried, it must operate on raw visual lines, not segments.** Note it still
cannot detect Mode B under-detection (BBWI p22), because the three real columns
all sit *inside* one detected column, and nothing crosses anything.

### 5.3 `page.find_tables()` as a chart/table excluder — unusable
PyMuPDF's detector flags **charts as tables**: on AEO-2023 p9 it reports the
stacked bar chart as a 6×13 table; on p7 it reports the bar chart's own bars as
8 tables. Blanket exclusion destroys working cases.

### 5.4 Full-width-span detection off current column edges — unreliable
Reports 88 of 142 gold pages, and on BBWI p36 flags ordinary prose lines,
because it inherits the wrong edges from Mode B. Fix Mode B first.

### 5.5 Full feature ranking (measured, 135 gold pages)

| Feature | AUC | Direction | Best thr | Recall | FP |
|---|---|---|---|---|---|
| `short_seg_share` | 0.887 | ≥ | 0.268 | 83% | 17% |
| `p25_fill` | **0.874** | ≤ | 0.246 | 76% | 9% |
| `numeric_share` | 0.842 | ≥ | 0.029 | 73% | 11% |
| `median_fill` | 0.826 | ≤ | 0.464 | 71% | 19% |
| `median_words_per_seg` | 0.818 | ≤ | 5.5 | 71% | 10% |
| `low_fill_share` | 0.782 | ≥ | 0.500 | 73% | 26% |
| `segs_per_band` | 0.684 | ≥ | 1.341 | 85% | 60% |
| `pitch_cv` | 0.533 | ≤ | — | — | — |
| `shared_band_share` | 0.511 | — | — | — | — |

`p25_fill` was chosen over `short_seg_share` for its much lower false-positive
rate, which matters because a false positive actively harms a prose page.

---

## 6. The validation problem (read before trusting any number here)

- Gold set: **449** labelled pages. Only **157 join** to the current audit on
  `(pdf_file, page)`.
- **155 of 160 unmatched files are absent from the live 202-doc corpus**
  (2021 / 2022 / 2025 editions). Five more point to page numbers beyond the
  current file's length — different editions behind the same filename.
- So the gold set is **~65% stale**. Everything in §4 is measured on the 157,
  and the threshold `0.2455` was fitted on those same pages.
- Aziz's own chunk review: **25 of 150** sampled chunks verdicted —
  5 `good`, 11 `usable_with_defects`, 8 `bad`, 1 `unsure`. Of the 6 reviewed
  chunks that are **live for retrieval** (`eligible`): 1 good, 2 defective,
  3 bad. Sample is stratified toward held chunks, so **32% bad is not a corpus
  rate.**
- Defect tag counts from that review: `split_mid_thought` 13,
  `heading_context_lost` 7, `reading_order_wrong` 7, `missing_page_content` 6,
  `table_mangled` 4, `boilerplate_or_navigation` 3.

**Aziz is building a review site (~1 hour) to crowdsource confirmation.**
The question reviewers should be asked is the actual gate decision:

> *Should this page be read column by column? Yes / No*

Not "is this a table?" — that is a different question.

---

## 7. Charts (Mode D) — explored, low priority

Vector charts **are** recoverable deterministically via PyMuPDF
`get_drawings()`; no VLM and no pixel calibration needed, because the values
already exist in the text layer and only the *association* is lost (fill colour
→ series, x-position → category, containment → value).

Prototype: `chartlib.py` in the session scratchpad (not committed). Two
independent checks — value-vs-bar-height correlation, and axis-label-count vs
category-group-count. Verified exact on AEO-2023 p7/p9/p10, DECK p44, ROST p31.

**Aziz's insight that separating tables from charts improves quality was correct
and measured:** using a uniform-height test to reject table cells and row bands
(cells share a bottom edge with identical heights; chart bars vary in height)
took chart clusters from 90 → 23 and garbage FAILs from 79 → 13 on the same
220-page sample, while keeping 9 of 10 real detections.

**Why it is low priority:** projected **~315 pages with any bar chart, ~52
cleanly recoverable** — out of 11,593. An earlier estimate of 3,846 "chart-heavy"
pages was wrong; 85% of those have no bar chart at all (icon grids, vector-masked
photos).

Known chart-page structures, for anyone resuming:
- Captions can be **above** (DECK p44) or **below** (DECK p125) the chart.
- **The caption line is the chart-segmentation signal** — it has one blob centred
  on each chart's span, while the axis line has one blob per column. On DECK p125
  (six independent small multiples) caption centres are 448 / 706 / 987. This is
  more robust than splitting on x-gaps, which fails: median pitch 30.4 puts the
  threshold at 91 while Landfill→Post Industrial are separated by 83.
- Axis labels should be assigned by **projecting column-centre boundaries** onto
  the axis text, not by guessing text gaps. One change fixes ROST's merged
  `2020* 2021`, DECK's merged `FY19 FY20`, and AEO p10's truncated `AE Bottoms`.
- Legend must be scoped **per chart**. Page-global legend leaked the bottom
  chart's series onto AEO-2023 p9's top chart, which has no legend at all —
  pure fabrication.
- DECK p44 has two charts with identical axes and identical series names; only
  the titles distinguish them. Without caption capture you index two
  contradictory answers to "FY19 preferred material share".

---

## 8. Adjacent finding: the chunker cuts mid-word

`src/esg_chunker.py`: `CHUNK_SIZE = 500`, `OVERLAP = 50`,
`MIN_CHUNK_TOKENS = 100`, `MAX_CHUNK_TOKENS = 600`. It splits at fixed **token**
positions then maps back to character offsets. Because BGE uses WordPiece
subwords, a boundary can land inside a word. **Confirmed with real examples:**

```
...and social responsi
...(surpassed in 2          <- ends mid-number
ip with our stakeholders and community...
```

**Caveat:** only ~338 of 16,433 chunks could be checked this way — the other
16,095 had `source_start_char`/`source_end_char` that do not resolve against
their `source_section_file`. **That is itself an unexplained finding worth
investigating** (offsets may be relative to the parsed doc rather than the
section file, or provenance has drifted).

---

## 9. Assessment of the external task prompts

Aziz has two agent prompts from another model ("Task 3: fix chunking", "Task 4:
fix reading order"). Verified:

**Trustworthy:**
- All seven named failure pages **exist and are all currently auto-passed**:
  BURL 2023 p30 (`column_order_reconstructed`), W 2024 p6
  (`column_order_reconstructed`), PTRN 2023 p128 (`auto_pass`), KTB 2024 p20
  (`region_order_reconstructed`) / p21 (`column_order_reconstructed`),
  UPBD 2023 p46 / p47 (`verified_table_extraction`). Not hallucinated.
- Named source files exist. (`tests/test_esg_chunker.py` and
  `tests/test_pdf_parser.py` do not — they are listed as existing but must be
  created. `src/esg_embedding_text.py` is correctly to-be-created.)
- The mid-word claim is **true** (§8).
- Ordering advice is right: reading order → sectioning → chunking.
- Two genuinely good catches: a shared embedding-text builder so chunker and
  `esg_p1_enrichment.py` cannot disagree on token counts; and counting tokens
  **after** metadata prefixes are added.
- "Raw chunk text must remain an exact contiguous substring" correctly protects
  citation validation.

**Problems:**
1. **Task 4 specifies metrics measured here as noise** — `column_crossing_count`
   and `interleaving_score` are §5.1/§5.2. It also has **no validation step**:
   it says "add real validation", lists ~15 metrics, then holds when "confidence
   is below the set threshold" without saying how the threshold is chosen. That
   is how the current broken gate was built.
2. **Do not run Tasks 3 and 4 in parallel** — both modify the vector manifest and
   its logic and add fields to overlapping files. The prompt's own final line
   contradicts its opening advice.
3. **Task 4 misses Mode B entirely** (wrong column count), which is the failure
   Aziz's review complained about most.

Neither task mentions validating against the gold set, or that it is 65% stale.
Both together force a full rebuild (16,433 chunks, 11,593 pages).

**Recommended use:** run sequentially (Task 4 → re-section → Task 3); add a hard
rule that no metric may gate anything until scored with cross-validated recall
and false-positive numbers; add Mode B detection; keep Task 3 close to as-written
(it is the stronger prompt).

---

## 10. Recommended fix sequence

1. **Mode A — skip the reorder for the 736 flagged pages.** Costs nothing (it is
   *less* processing). Sample ~15 prose-looking flagged pages first to bound the
   8% false-positive harm. Put behind a flag that can be disabled in one line.
2. **Mode B — recursive gutter detection** inside each detected column. Must come
   before Mode C, which needs correct edges.
3. **Mode C — horizontal band segmentation** at full-width lines, then columns
   within each band.
4. **Replace the gate.** `preservation_ratio` may remain as reported evidence but
   must not decide. Every new check must measure the page against evidence the
   reconstruction did not itself produce, and must be able to fail.
5. **Only then reparse.** Reparsing before the checks are validated produces an
   unmeasurable result.
6. **Charts last, if at all.**

---

## 11. Key files

| Path | Role |
|---|---|
| `src/esg_reading_order.py` | column reconstruction; the tautological ratio at lines 315–320 |
| `src/esg_layout_qa.py` | page audit, decision rules, `AUDIT_VERSION = "layout_v7"` |
| `src/pdf_parser.py` | extraction, page maps, table paths |
| `src/esg_row_structure.py` | **new** — Mode A detector (this work) |
| `src/esg_chunker.py` | fixed-token chunker, `esg_chunk_v2` |
| `data/00_reference/esg_page_layout_qa.csv` | 11,593 page audit rows |
| `data/00_reference/esg_layout_gold_labels.csv` | 449 two-annotator labels (65% stale) |
| `data/00_reference/esg_parse_index.csv` | 202 docs; parsed-text and page-map paths |
| `data/00_reference/esg_chunks_index.csv` | 16,433 chunks |
| `reports/rowcheck_review_queue.csv` | **736 flagged pages**, worst-first |
| `reports/rowcheck_gold_scoring.csv` | per-page scoring vs gold |
| `reports/rowcheck_features.csv` | feature values per gold page |
| `reports/chunk_quality_review_2026-07-29/` | Aziz's review site + verdicts CSV |

Environment: `venv/Scripts/python.exe`; pdfplumber and PyMuPDF 1.28 both
available. All analysis scripts here are read-only against the corpus.

---

## 12. Governing principle

The original bug was not a bad threshold. It was a **gate that could not fail**,
which manufactured confidence for 3,005 pages while the contradicting evidence
sat in the repo's own gold set.

Two of the three replacement ideas tried here were also noise, and one repeated
the identical circularity. So: **no check ships without a cross-validated recall
and false-positive number measured against labels it did not help produce.**
