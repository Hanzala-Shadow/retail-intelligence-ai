# Production vs candidate reading-order comparison

Date: 2026-07-30 - Seed: 20260730

**This supersedes `reports/parser_comparison_2026-07-30/` and `reports/old_vs_new_review_2026-07-30/`.** Both compared the candidate against `esg_reading_order.reconstruct_column_order` fed navigation-stripped pdfplumber words and called that 'production'. It is not: real production (`src/pdf_parser.py`) uses PyMuPDF words, unstripped, and runs a two-stage pipeline (column reader, then a region pass under `use_region_pass`). `reports/old_vs_new_review_2026-07-30/` (the r01-r03/g01-g19 renders and `recovered_scores_unblinded.json`) is the same voided baseline: its `r*` pages are exactly the 'production refuses' void group and its `g*` pages compare the candidate against the column reader alone, not real production. `reports/parser_comparison_2026-07-30/` is `column_order`/`regions`/table-variant output from navigation-stripped pdfplumber words, the same wrong baseline. This report is the first to call production's real two-stage path (`reconstruct_column_order` then, conditionally, `pdf_parser.reconstruct_region_order`) against raw, unstripped PyMuPDF words.

## 1. How often does production's region pass actually fire?

Nobody previously measured this. Across the full sample (58 pages, both groups combined):

| Branch | Pages | Share |
|---|---:|---:|
| region_pass | 11 | 19.0% |
| column_order | 17 | 29.3% |
| native | 30 | 51.7% |

Broken out per group:

| Group | Pages | region_pass | column_order | native |
|---|---:|---:|---:|---:|
| human_validation | 13 | 2 (15.4%) | 8 (61.5%) | 3 (23.1%) |
| diff_sample | 45 | 9 (20.0%) | 9 (20.0%) | 27 (60.0%) |

## 2. How many sampled pages does production actually differ from the candidate on?

**58 / 58** sampled pages differ (identical-input comparison). By construction, diff_sample pages were selected because they differ, so that group's share is not informative on its own; the human_validation number is the useful one because it is a fixed, independently-chosen set.

- human_validation: 13 / 13 differ.
- diff_sample: 45 / 45 differ (selected to differ, by design).

## 3. Was the candidate compatible with raw PyMuPDF words out of the box?

**Yes.** `reconstruct_by_regions` was called on raw, unstripped PyMuPDF word dicts (from `_pymupdf_words`) for all 58 sampled pages with zero exceptions. No shim was needed: the fields `reconstruct_by_regions` reads (`text`, `top`, `bottom`, `x0`, `x1`, `upright`) are all present in `_pymupdf_words`'s output schema, which happens to already match what `esg_reading_order`/`esg_reading_regions` expect.

## 4. Does navigation-stripping change the picture materially?

Secondary, reference-only run: candidate fed navigation-stripped pdfplumber words (via `esg_navigation.clean_navigation`), NOT identical input to production. Kept out of the headline numbers above.

- 7 / 58 sampled pages produced the SAME candidate text with and without navigation stripping.
- 51 / 58 differed.
- Navigation stripping materially changes the candidate's output on a meaningful share of pages; it is not a safe simplification to skip. (87.9% differ.)

## Sample composition

- human_validation: 13 pages (the 13 pages scored by a human in `reports/llm_reader_review_2026-07-30/`).
- diff_sample: 45 pages across 15 documents, seed 20260730, reproducible page list in `sampled_pages.json`.

## Files

- `results.json` - per-page record for every sampled page, both groups.
- `secondary_navigation_stripped.json` - the reference-only run from section 4.
- `sampled_pages.json` - reproducible diff_sample page list.
- `judging_queue.json` - blinded parser_a/parser_b texts + image paths only.
- `blind_key.json` - full unmasked mapping and per-page branch/status fields.
- `images/` - 110dpi PNG renders of every sampled page.
- `render_drops.json` - any pages excluded because their render failed.

No paid API calls were made. Judging queue and renders only; scoring happens separately (human or a separate chat-based model).

## 5. Blind judging results (added after initial report)

A separate AI session judged all 58 items blind (image + Parser A/B text only,
no reader names, no `blind_key.json` access) and wrote verdicts to
`blind_verdicts.json`. Unblinding against `blind_key.json`:

| Set | Production wins | Candidate wins | Tie |
|---|---:|---:|---:|
| human_validation (13, fixed calibration set) | 11 | 2 | 0 |
| diff_sample (45, selected because texts differ) | 12 | 27 | 6 |
| **Overall (58)** | **23** | **29** | **6** |

The two sets tell different stories and should not be averaged together. The
`human_validation` set is the only unbiased signal here — it was chosen before
any of this comparison existed, independent of who would win — and production
won it decisively (11/13). `diff_sample` was selected specifically because the
two readers disagree, which does not favor either side by construction, but it
skews toward pages where the readers make different structural calls (columns,
panels, tables) rather than being representative of an average page; on that
set the candidate did better, consistent with the human_validation-adjacent
finding in the earlier handoff (multi-panel/multi-column prose is where the
candidate's peel logic tends to win) and the known weak spot (dense tables) not
being a large share of this particular sample.

**Read this as: on the one page set nobody could have gamed, production still
wins. On pages that are hard/ambiguous enough to make the two readers
disagree, the candidate wins more often.** That is consistent with, not a
reversal of, the standing 448-page sweep result (14 Better / 3 Same / 2 Worse)
in the handoff doc — both point to the candidate being stronger specifically on
multi-panel/column pages, with tables remaining the open problem.
