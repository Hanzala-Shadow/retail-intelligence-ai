# Reading-order work — session handoff

Date: 2026-07-30 · Repo: `retail-intelligence-ESG-works` · Branch: `Phase_4.1_Aziz`

**This is the current master handoff.** It supersedes
`reports/reading_order_bundle2_2026-07-30/HANDOFF_regions_2026-07-30.md`, which
remains accurate for the code-change detail but predates all validation and the
table investigation.

> ## ⚠️ READ THIS BEFORE USING ANY COMPARISON RESULT BELOW
>
> **The old-vs-new comparison was run against the wrong baseline.**
>
> `scripts/compare_reading_parsers.py` treats
> `esg_reading_order.reconstruct_column_order` as "the production reader". It is
> not. `src/pdf_parser.py` runs **two** readers: the column reader first, then
> `pdf_parser.reconstruct_region_order` (its own region pass, gated at 99.5%
> token preservation) whenever `use_region_pass` fires — which happens when the
> column reader returns `ambiguous`, when the page has table blocks, or when a
> reconstructed page has a mixed-width header (`pdf_parser.py:1585`).
>
> Consequences, measured:
>
> - The "production refuses these pages" group is **void**. On all **18/18** of
>   those pages production's region pass returns `reconstructed` and emits text
>   of near-identical length to the candidate's. Production was never silent.
>   The Codex result of candidate 17 – production 0 on that group was scored
>   against an empty string that production would never have produced.
> - The 13 human-scored pages are **mostly unaffected**: the region pass would
>   have overridden the column reader on only 1 of 13 (ACI p7). So
>   production 8 – candidate 4 there still stands, with that one caveat.
>   (`table_blocks` can also trigger the pass and was not checked — it needs
>   PyMuPDF — so treat 12/13 as an upper bound.)
> - **The real unanswered question** is candidate vs `reconstruct_region_order`,
>   which has never been compared. They are not redundant: over those 18 pages
>   the two produce identical text on only 2, mean similarity 0.71, minimum 0.26.
>
> **Do not act on the "adopt as a fallback" conclusion.** It was derived from the
> void group. Any future comparison must call production's real path — column
> reader, then region pass under the same `use_region_pass` condition — not one
> component of it.

---

## 1. What this work is

`src/esg_reading_regions.py` (`reconstruct_by_regions`) is a **default-off,
read-only candidate** PDF reading-order reader. It is **not wired into
production** and changes no corpus data. It sits on top of the validated
production reader `src/esg_reading_order.py` (`reconstruct_column_order`),
reusing its line-grouping, gutter-splitting and column-detection primitives.

The difference: `reconstruct_column_order` picks **one column count for the
whole page** and returns empty text with `ambiguous` rather than guess.
`reconstruct_by_regions` splits a page into regions and decides per region.

Region types: `heading`, `single_column_prose`, `multi_column_prose`,
`row_structured`, `table_verified`, `uncertain`.

**Standing invariant:** every cleaned body word appears exactly once
(`preservation_ratio == 1.0`). Holds on every page tested — 12 scoped pages,
448 sweep pages, 42 comparison pages.

## 2. File map

| Path | What |
|---|---|
| `src/esg_reading_regions.py` | The candidate module. All code changes live here |
| `src/esg_reading_order.py` | Validated production reader. **Unchanged** |
| `src/esg_navigation.py`, `src/pdf_parser.py` | **Untouched, off limits** |
| `src/esg_row_structure.py` | `MAX_P25_FILL`, drives `row_structured` |
| `src/esg_layout_qa.py` | Audit decisions + table acceptance bars |
| `scripts/run_reading_order_pilot.py` | Runs the 12 scoped pages → bundle2 report |
| `scripts/run_reading_order_peel_sweep.py` | 448-page blast-radius sweep |
| `scripts/compare_reading_parsers.py` | 5-variant parser comparison harness |
| `tests/test_esg_reading_regions.py` | Synthetic unit tests |
| `tests/test_esg_reading_regions_real_pages.py` | Real-page text-order regression |
| `reports/reading_order_bundle2_2026-07-30/` | Pilot output + original handoff |
| `reports/reading_order_peel_sweep_2026-07-30/` | Sweep + human scores |
| `reports/parser_comparison_2026-07-30/` | Parser comparison |

**Everything above is untracked in git (`??`).** There is no baseline to diff
against; these documents are the record.

## 3. Code changes made, and why

Two fixes were inherited from a prior session (structure-change cut selection;
`_merge_orphan_panel_titles`) — see the older handoff. This session added two more.

### Change A — corroborate the "full-width" side of a structure change

New `_is_single_column_slice`, used in `_split_structure_changes`.

`_different_structure` fires on "one repeated column start on one side, two or
more on the other" and reads that as a single-column block turning into columns.
But `_rough_starts` needs a start to **repeat twice** before it reports a cluster
at all, so inside a 4-line window it silently under-reports any column that is
sparse there. A side whose second column merely failed to repeat looked
identical to a genuinely full-width one.

Measured on AEO-2024 p8: a "before" slice containing two obviously two-segment
lines (`sustainable sources | 60%`, `2028 GOAL: | 46%`) still reported one start
cluster, cutting the progress card in half and emitting the whole chart between
the pieces.

Fix: a candidate now also requires the one-cluster side to consist entirely of
one-segment lines — the direct evidence, and the same signal the cut selection
already trusts downstream.

**Result:** AEO-2024 p8 fixed (5 → 3 regions, verified against a render).
BBY p46 and AEO-2023 p8 unchanged. AEO-2023 p3 now correct *by principle*
rather than by luck of a size guard.

### Change B — peel side-by-side panels before splitting into runs

New `_large_gap`, `_row_gap_middles`, `_bridges_row_gap`,
`MAX_PANEL_PEEL_DEPTH = 3`, a `require` predicate on `_panel_split`, and
recursion in `_regions_from_lines`.

The architecture split pages into vertical runs first, then looked for panels
*within* each run. That assumes every panel starts and ends at the same heights
as its neighbours. BBWI-2023 p22 has a pull-quote running the full page height
beside content with its own row breaks — it bridges those row gaps, so no run
boundary can fall there, and splitting run-by-run emitted three panels a slice
at a time, mid-sentence.

Fix, two parts:

1. **Peel first, at whole-region scope**, recursing into each side. At whole-body
   scope a heading reaching across a gutter disqualifies it, which is what stops
   the columns of one flowing block being torn apart.
2. **Gate on bridging.** `_bridges_row_gap` requires one side to reach across a
   row gap that would otherwise split the other — the one configuration a
   run-first reader *cannot* order. Where nothing spans, the existing
   runs-then-panels path is left alone.

Four alternative selection rules were measured and rejected first (widest gap,
fewest shared lines, one-side-atomic, XY-cut significance) — none picks the
correct gutter on BBWI p22. Detail in the older handoff.

**The gate is not decorative:** removing it changes output on 39 of 448 pages,
and all 43 of its rejections were of the *widest* strip at their node, i.e.
strips the code would otherwise have taken.

### Tried and reverted

Enforcing `MIN_REGION_RUN_LINES` as a structure-*persistence* check (requiring
the new structure to hold for N lines). It would have merged BBWI p22's
infographic into one region, but **regressed AEO-2023 p8 from 3 regions to 1**.
Reverted; no trace in the source.

## 4. Validation evidence

### Tests — 53 passing

```bash
python -m unittest tests.test_esg_navigation tests.test_esg_reading_order tests.test_esg_reading_regions tests.test_esg_reading_regions_real_pages
```

`test_esg_reading_regions_real_pages.py` was **rewritten**. It previously
asserted region counts and bboxes only — which is exactly why BBWI p22 passed
while emitting three panels in slices. It now asserts text order plus a
block-non-interruption check, because ordering alone cannot catch a panel
emitted in two pieces with another panel wedged between them.

Note: the suite builds the pilot bundle **once per module** via a cached
`bundle_rows()`. Per-class `setUpClass` turned a ~90s suite into minutes.

### Mutation checks — the tests are load-bearing

| Reverted | Tests that fail |
|---|---|
| `_is_single_column_slice` → always True | both `SideBySidePanelTests`, only the `year=2024` subtests |
| `MAX_PANEL_PEEL_DEPTH = 0` | 4 real-page + both `SpanningPanelTests` |
| `_bridges_row_gap` → always True | `test_two_different_layouts_on_one_page_become_two_regions` |

### 448-page out-of-sample sweep

32 documents, 32 tickers, 14 pages each, seed `20260730`, page list preserved.
No overlap with the 12 pages the fixes were tuned against.

- Peel fires on 69 pages (15.4%); depth 1 on 4; depth 2 reached 8 times, never fired
- 388 of 448 pages unchanged in region count
- Word preservation 1.0 in **both** configurations on all 448
- One status flip, `needs_review → candidate_ready` (JWN 2024 p5) — reviewed, correct

### Human Better/Same/Worse review (Aziz, 19 pages)

**14 Better / 3 Same / 2 Worse — net +12.**

Both Worse pages are severity-3 (block order), not severity-1 (sentence
shredding): KSS p22 moves a sidebar into the middle of the main column; ACI p29
puts `2022 PROGRESS` mid-table. Neither creates interleaving, so the
pre-registered disqualifying condition did not trigger.

**The notes matter more than the tally.** The peel splits by page type:

- **Multi-panel / multi-column prose — consistent win.**
- **Dense tables — neutral to negative.** Reviewer note: *"usually both not good
  for tables."*
- Several Betters are **partial** fixes: *"went from one column to the other
  mid-column"*, *"still joined two columns"*.

## 5. Findings that reframe the work

### 5.1 Charts are a ceiling, not a bug

AEO-2024 p8's chart block, after the fix:

```
TOTAL PREFERRED FIBERS
80% 75% 62% 60% 54% 53% 46% 40% 23% 20% 9% 3% 0
2018 2019 2020 2021 2022 2023 2024 2028 GOAL
```

Order is correct. But that sequence interleaves **y-axis gridlines**
(80/60/40/20/0) with **bar labels** (75/62/54/53/46/23/9/3), and the years sit
on a separate line. The value↔year mapping is gone. "What % in 2021?" is
unanswerable from this chunk.

Reading order is a 1-D linearisation; a bar chart's meaning is a 2-D spatial
mapping. **No ordering of these tokens recovers it.** Charts need an
image-reading path (`esg_vlm_stage`), not a better reader.

### 5.2 Tables — the real blocker, and it is NOT acceptance scope

Initial diagnosis was: the `verified_table_text` hook replaces the *whole page*,
so it is gated at 99.5% recall against all page words, and real tables share
their page with a heading — fix the scope. Measured over 42 pages
(AEO-2024 ×10, ACI-2022 ×32): 9 real tables found, all 9 refused, four of them
missing only the page heading (ACI p29 98.1%, AEO p4 97.6%, AEO p3 95.0%,
ACI p32 94.7%). `extra_token_ratio` was 0.000 on eight of nine — **extraction
quality is not the problem.**

**That diagnosis turned out to be wrong**, and the correction is the most
important finding in this document (see §6 for the evidence). Region-scoped
substitution was implemented and fires **zero times** on 42 pages. Cause:

- **Whole-page tables** — the region reader **fragments the table into many
  regions**. ACI p30 has *12* regions inside one ruled table; ACI p29 has 8.
  A one-table↔one-region uniqueness rule can never be satisfied.
- **Partial-page tables** — the table matches **no region at all** (7 of 12).

So there is no correct scope to substitute into, because **the decomposition
does not produce a table-shaped region**. That is the same weakness the human
review found, now with a mechanism behind it.

**Implication for the next design:** stop trying to match tables to
independently-derived regions. Let detected ruled lines **drive** the
decomposition — a ruled table should *define* a region boundary, not be matched
against one afterwards.

### 5.3 Threshold fragility — the thing most worth checking next

Traced on TGT-2019 p8 (4-column infographic). At depth 0 the widest gutter
(x≈385) is rejected at **shared-line share 0.83 against a limit of 0.82**. The
next gutter is taken instead; once that column is removed, x≈385 measures 0.55
and peels cleanly at depth 1. Each peel de-noises the measurement for the next —
a genuinely good property.

But **this page's behaviour hangs on a margin of 0.01.** If
`PANEL_MAX_SHARED_LINE_SHARE` were 0.84 the whole recursion order would invert,
and nobody knows whether the result would be better or worse. That constant is
one of the unvalidated `PANEL_*` values.

**Recommended:** sweep `PANEL_MAX_SHARED_LINE_SHARE` across ~0.75–0.90 and count
how many of the 448 pages change output. If many change, the peel's behaviour is
balanced on a knife edge and every result above is softer than it looks.

### 5.4 Depth cap — settled, leave at 3

`MAX_PANEL_PEEL_DEPTH = 3`. Measured: cap 3 and cap 2 produce **byte-identical
text** on all four pages that reach depth 2; cap 1 changes all four. So depth 1
is load-bearing and depth 2 is pure headroom. Lowering to 2 buys nothing and
removes headroom for a 5-column page. **Leave it at 3; document the measurement
rather than refitting the constant.** (An earlier recommendation to lower it was
withdrawn once the depth-reached data was examined.)

## 6. Region-level table substitution — COMPLETE, negative result

**Status: finished and closed out.** Deliverable:
`reports/parser_comparison_2026-07-30/regions_table_cells_summary.md`.

A parallel session implemented the region-level table substitution task. It landed:

- `reconstruct_by_regions(..., verified_region_tables=...)` — new optional
  parameter (the recommended design)
- `RegionReadingOrderResult.region_texts` — new field
- `_region_containment_share`, `VERIFIED_REGION_MIN_CONTAINMENT = 0.80`
- `regions_table_cells` as a 5th variant in `compare_reading_parsers.py`
- `reports/parser_comparison_2026-07-30/` re-run with 5 variants
- Synthetic tests now 12 (was 11); **full suite 53 passing**

**Measured result over 42 pages:**

```
eligible_ruled_tables:             12   (on 11 pages, after furniture filters)
substitutions:                      0   <-- never fires
table_without_region_match:         7
one_table_multiple_regions:         4
unique_match_verification_failed:   1
```

`regions_table_cells` equals `regions` on 42 of 42 pages. Preservation stayed
1.0 everywhere; zero unruled data-table pages were found (all 42 reviewed as
renders — the four with no ruled detection are AEO p2 and ACI p2/p3/p28, and
ACI p2 is a contents page correctly excluded as navigation).

**Independently verified in this session:** 53 tests pass; the pilot's 12-page
region counts are identical to the pre-change record; the summary discloses
rather than hides its weak spots (garbled raw-cell tokens on ACI p30 = 0.0778
and p32 = 0.0066, not subtracted).

**The decisive detail:** AEO p4 was the *only* table with a unique one-to-one
geometry match, and it still failed at region recall 0.9797 — because the
matched region contains text outside the ruled table. So even where the match
was clean, the region boundary and the table boundary were not the same thing.

**A zero result here is informative, not a failure.** It kills the wrong
approach cheaply and names the right one: the problem is the decomposition, not
the acceptance bar. Do not respond to this by loosening token thresholds.

### 6.1 A qualifier worth carrying forward

"Tables are weak" is too broad. Looking at the actual outputs, two different
things are happening:

- **Numeric data tables** (AEO p5, p6, p10) — the geometric reader already
  handles these acceptably. Data rows come out intact and in order
  (`2018 -10% 3 0.7`). Only the multi-line column headers get mangled.
- **Goals / prose tables** (ACI p29, p30) — these are the ones that fragment,
  and they are what the human review scored down.

This is an observation from reading the outputs, **not a measurement**. Worth
confirming before scoping the successor design, because it could narrow the
problem substantially.

⚠️ **Concurrency note for future parallel sessions:** a test run during that
session's writes produced "Ran 32 tests, FAILED (errors=17)" because the module
imported half-written. It was **not** a real breakage — a clean re-run gives 53
passing. If you see import errors, re-run before investigating.

## 7. Open decisions (Aziz's call)

1. **Is this module intended for production, and behind what gate?** Everything
   above is work on a default-off candidate. This determines whether the next
   step is a labelled validation set — the `PANEL_*` and `INNER_*` thresholds
   have never been validated against one, and Aziz's 19 scores are the first
   human signal the module has ever had.
2. **Ruled-lines-driven decomposition for tables** (§5.2) — the successor to the
   failed substitution approach.
3. **`PANEL_MAX_SHARED_LINE_SHARE` sensitivity sweep** (§5.3).
4. **Charts** — accept the ceiling and route chart pages to `esg_vlm_stage`?
5. **Unruled tables** — the `lines` strategy cannot see tables drawn with
   spacing alone. Nobody has counted how many exist.

## 8. Recommended order

1. ~~Close out the in-flight table work~~ — **done, §6.** Negative result recorded.
2. Answer decision 1 (production-bound or not) — it changes the shape of
   everything else.
3. `PANEL_MAX_SHARED_LINE_SHARE` sweep — cheap, and it calibrates confidence in
   all existing results.
4. Confirm or refute the numeric-vs-goals-table split in §6.1 before scoping any
   table work. If numeric tables are already fine, the successor design only has
   to handle prose/goals tables, which is a much smaller job.
5. Ruled-lines-driven (table-box) region splitting, only if 4 justifies it.

Reading order itself is in good shape; the remaining wins there are small. The
metrics that ESG retrieval actually needs live in tables and charts.

## 9. Commands

```bash
# 12 scoped pages -> bundle2 report
python scripts/run_reading_order_pilot.py

# full suite (53 tests, ~145s)
python -m unittest tests.test_esg_navigation tests.test_esg_reading_order tests.test_esg_reading_regions tests.test_esg_reading_regions_real_pages

# 448-page sweep (reproducible, seed 20260730)
python scripts/run_reading_order_peel_sweep.py

# parser comparison, defaults to AEO-2024 + ACI-2022
python scripts/compare_reading_parsers.py
python scripts/compare_reading_parsers.py --doc "TGT:TGT-TARGET CORP-2019.pdf:8,14,16"
```

## 10. Standing constraints

- Read-only. Do not reparse or write to the corpus. Do not wire into production.
- Do not modify `esg_navigation.py` or `pdf_parser.py`.
- No page-specific, ticker-specific or filename-specific logic anywhere.
- Keep the 53 tests green.
- A negative result is a valid outcome and more useful than a forced win.
