# Handoff: region-based PDF reading-order candidate (Bundle 2)

Date: 2026-07-30 · Repo: `retail-intelligence-ESG-works` · Branch: `Phase_4.1_Aziz`

> **SUPERSEDED.** The current master handoff is
> `reports/READING_ORDER_SESSION_HANDOFF_2026-07-30.md`. This document is still
> accurate for the code-change detail below, but it predates the 448-page
> sweep, the human Better/Same/Worse review, and the table investigation.
> Start with the master handoff.

## What this is

`src/esg_reading_regions.py` (`reconstruct_by_regions`) is a **default-off, read-only
candidate** module. It is not wired into production and changes no corpus data. It sits on
top of the already-validated single-column-count reconstructor
`src/esg_reading_order.py` (`reconstruct_column_order`), reusing its line-grouping,
gutter-splitting and column-detection primitives.

The job was to fix region/column reading-order bugs on four page layouts **without
page-specific code**, then strengthen the real-page regression tests to check actual text
order, and refresh the bundle2 report.

### Constraints (all respected)

- General fixes only — no per-ticker, per-filename or per-page branching anywhere.
- Every cleaned body word appears exactly once: `preservation_ratio == 1.0` on all 12 pages.
- `src/esg_navigation.py` and `src/pdf_parser.py` untouched.
- Not wired into production; corpus not reparsed.

**All five files touched are untracked** (`??` in git) — there is no git baseline to diff
against, so this document is the record of what changed.

## State inherited from the previous session

Two fixes were already in place and are unchanged by this session:

1. **`_split_structure_changes` cut selection.** The candidate scan range was widened from
   `range(window, len(run) - window + 1)` to `range(1, len(run))` with clamped slices, and
   the cut is now chosen by a direct per-line segment-count check within the candidate
   group's index range rather than by the group's midpoint. Fixed BBY-2024 p46's mixed
   4-column line, and fixed the AEO-2023 p3 goals-table regression as a side effect.
2. **`_merge_orphan_panel_titles`.** Reattaches a run holding nothing but two panels' short
   title lines to the run that completes the panel split, peeling genuine leading headings
   off first using a `content_width` fixed once from the run's original lines. Fixed
   AEO-2023 p8.

Open blocker at handover: **AEO-2024 p8 still interleaved** the left progress card with the
right chart, and **BBWI-2023 p22 had not been worked on**.

---

## Change A — corroborate the "full-width" side of a structure change

**File:** `src/esg_reading_regions.py` · new `_is_single_column_slice`, used in
`_split_structure_changes`.

### Root cause (AEO-2024 p8)

The page is a heading over a progress card (left) and a bar chart (right), whose lines
interleave. `_split_structure_changes` cut the card+chart block in two at line index 9
(`top≈198.6`), so regions came out as
`[heading][card-top][chart-top][card-tail][chart-bottom]` — the whole chart wedged inside
the card.

The cut came from `_different_structure` seeing `before=[59.8]` (one repeated start) and
`after=[59.8, 333.2]` (two), which it reads as "a full-width block changing to columns".
But the "before" lines are **not** full-width — the window slice was:

```
sustainable sources | 60%      <- 2 segments
54%                            <- 1
53%                            <- 1
2028 GOAL: | 46%               <- 2 segments
```

`_rough_starts` requires a start to repeat **twice** before it reports a cluster at all.
Inside a slice only `STRUCTURE_WINDOW_LINES` (4) lines tall, that guard silently
under-reports any column that happens to be sparse there, and a side whose second column
merely failed to repeat is indistinguishable from a genuinely full-width one.

### Fix

A candidate now additionally requires the one-cluster side to consist entirely of
one-segment lines — the direct evidence, and the same signal the cut selection downstream
already trusts to pinpoint the boundary line.

### Effect

- **AEO-2024 p8 fixed**: 5 → 3 regions — heading, complete card, complete chart. Verified
  against a rendered screenshot of the page (bar labels 3/9/23/46/53/54/62/75%, years
  2018–2024 + 2028 GOAL all land in the chart region, in order).
- BBY-2024 p46 unchanged: its candidate group `[2,3,4]` loses only index 4, and the cut
  still resolves to line 3 — the first line of the 4-column body.
- AEO-2023 p8 unchanged (its first candidate group is unaffected).
- AEO-2023 p3 now stays one region **by principle** rather than by luck: previously all its
  candidates survived and the split was only prevented by the `MIN_REGION_RUN_LINES` size
  guard; now every candidate is correctly rejected, because neither side is ever
  single-column in that table.

---

## Change B — peel side-by-side panels before cutting into vertical runs

**File:** `src/esg_reading_regions.py` · `_regions_from_lines` (now recursive), new
`_large_gap`, `_row_gap_middles`, `_bridges_row_gap`, new `require` parameter on
`_panel_split`, new `MAX_PANEL_PEEL_DEPTH = 3`.

### Root cause (BBWI-2023 p22)

The page has a heading, an intro, a diversity infographic, lower-left DEI prose, a centre
awards panel, and a **right pull-quote that runs the height of the page alongside all of
them**. It scored 5 regions — the "expected" count — while being badly wrong: the
infographic labels, the awards panel and the quote were emitted a slice at a time, e.g.

```
RACE We want associates to feel part of something
IS ALL OF US
bigger than their day-to-day roles. We want
```

The architecture split the body into vertical runs first, then looked for side-by-side
panels **within** each run. That assumes every panel starts and ends at the same heights as
its neighbours. The pull-quote bridges the row gap between the infographic and the row
below it, so no run boundary can fall there; the whole lower two-thirds became one run, and
splitting it panel-by-panel interleaved everything.

### Why the obvious selection rules do not work

Within that run two blank vertical strips qualify — 52.3pt at x≈231 (the left-column
gutter) and 36.2pt at x≈569 (the quote gutter). Only the second is correct. Measured, and
all rejected:

| Rule | x≈231 | x≈569 | Picks correctly? |
|---|---|---|---|
| Widest gap (current behaviour) | 52.3 | 36.2 | no |
| Fewest shared visual lines | 0.317 | 0.463 | no |
| One side atomic + other has a row break | holds | holds | no (not discriminating) |
| XY-cut on most significant separator | widest x-gap wins | — | no |
| Row alignment of shared lines | — | median 1.64pt | too brittle to rely on |

Multi-way splitting at every qualifying strip, and recursing after a single split, both
also fail: they tear the infographic into left-labels and right-labels with the DEI prose
emitted between them.

### Fix

Two parts.

**1. Peel first, at whole-region scope.** `_regions_from_lines` attempts a panel split on
the whole set of lines *before* `_split_into_runs`, and recurses into each side (depth-capped
at `MAX_PANEL_PEEL_DEPTH`; each peel strictly shrinks both sides, so it terminates anyway).
At whole-body scope the heading and intro reach across x≈231, so that gutter is not blank
page-wide and is not a candidate — only the quote gutter is. Measured across the 12 pages, a
whole-body strip qualifies on exactly 2 of them (BBWI p22, AMZN p63).

**2. Gate the peel on bridging.** `_bridges_row_gap` requires that one side reach across a
row gap that would otherwise split the other, reusing the same large-gap policy as
`_split_into_runs` (factored out into `_large_gap`). This is the one configuration a
run-first reader **cannot** order; where nothing spans, runs-then-panels already works and
is left alone.

The gate is necessary, not decorative: without it the peel tears the last column off the
synthetic 4-column grid in
`test_two_different_layouts_on_one_page_become_two_regions` (the grid's own rows sit
entirely below the prose block's row gap, so nothing bridges → correctly rejected).

`_panel_split` gained an optional `require` predicate, applied after its existing guards,
so candidate strips are tried widest-first and the search continues past strips that fail
it. Run-level panel splitting passes no predicate and is byte-identical to before.

### Effect

- **BBWI-2023 p22 fixed**: all five content blocks are now continuous and in visual order —
  heading, `WE EMBRACE DIVERSITY` + intro, infographic, lower-left DEI prose (whole),
  centre awards panel (whole), right pull-quote (whole, through
  `Kelie Charles / Chief Diversity Officer`).
- **AMZN-2023 p63 improved** (still held, see below): the right-hand feature panel's
  heading `Discover Product Sustainability Features` used to be pulled into the top region,
  hundreds of points from its own panel; it now sits with its own panel. Verified against a
  render.
- No other page's output changed.

---

## Tried and reverted

**Enforcing `MIN_REGION_RUN_LINES` as a structure-persistence check** — requiring the first
`MIN_REGION_RUN_LINES` lines of the new region to each carry the new structure. Motivation:
it would have merged BBWI p22's infographic into a single region. It **regressed AEO-2023 p8
from 3 regions to 1**, because that page's needed cut is not followed by three
multi-segment lines. Reverted; the source carries no trace of it.

---

## Results — all 12 scoped pages

| Role | Page | Regions | Status | Verdict | Note |
|---|---|---|---|---|---|
| target | BBY p46 | 3 | candidate_ready | improved | 4-column body, one column at a time |
| target | BBWI p22 | 7 | candidate_ready | improved | 5 blocks, all continuous (see caveat) |
| target | BBWI p36 | 2 | candidate_ready | needs review | held, untouched |
| target | BBW p2 | 2 | candidate_ready | improved | unchanged output |
| target | AMZN p63 | 4 | candidate_ready | needs review | held; right panel now separated |
| target | AEO 2023 p8 | 3 | candidate_ready | improved | card before chart |
| target | AEO 2024 p8 | 3 | candidate_ready | improved | **fixed this session** |
| control | AEO p2 | 1 | candidate_ready | unchanged | |
| control | AEO p6 | 1 | candidate_ready | unchanged | |
| control | AEO p10 | 3 | candidate_ready | unchanged | |
| control | AEO p3 | 1 | candidate_ready | unchanged | goals table stays row-structured |
| control | AAPL p97 | 1 | candidate_ready | unchanged | |

Word preservation passes on all 12 (`ratio == 1.0`).

Pages that changed this session: AEO-2024 p8 (5→3), BBWI p22 (5→7, content fixed),
AMZN p63 (3→4, improved). Everything else is byte-identical.

---

## Tests

`tests/test_esg_reading_regions_real_pages.py` was **rewritten**. It previously asserted
region counts and bboxes only — which is why BBWI p22 passed while emitting three panels
in slices. It now asserts:

- `assert_reads_in_order(...)` — landmark substrings in output order.
- `assert_block_uninterrupted(block, foreign)` — the interleaving check. Ordering alone
  cannot catch a panel emitted in two pieces with another panel between them, because each
  piece can still be in the right relative order.
- BBY p46: the sentence broken across the first column break is rejoined
  (`...from attempted` + `attacks. Our cybersecurity operations`), and no single output line
  mixes markers from separate columns.
- AEO p8, both years: the card is complete before the chart, and the chart never interrupts it.
- BBWI p22: all five blocks in visual order, and each block checked against every other
  block's landmarks.
- AEO p3: goal/status/progress row integrity, including a row further down the table.
- Controls: explicit reading order per page. Held pages: still flagged.

Landmarks are ASCII-safe — the extracted text carries replacement characters where the
source PDFs use typographic apostrophes, so no landmark spans one. Two landmarks needed
care and are commented in place: `Total AEO` is also a chart axis label on AEO p6, and
`Initial work underway` recurs across several AEO p3 rows (asserted as a contiguous phrase
instead).

Also added: `SpanningPanelTests` in `tests/test_esg_reading_regions.py` (2 tests) — a
synthetic tall sidebar beside two stacked blocks, so the peel mechanism is covered
synthetically and not only by a real page.

Performance: the pilot bundle is now built **once per module** via a cached `bundle_rows()`
instead of once per test class. Per-class `setUpClass` made the suite take minutes, because
building a navigation profile walks each source report end to end.

### Test status

```bash
python -m unittest tests.test_esg_navigation tests.test_esg_reading_order tests.test_esg_reading_regions tests.test_esg_reading_regions_real_pages
```

**52 tests, all passing** (~144s; the real-page module reads 7 source PDFs).
Breakdown: navigation + reading_order (20), `test_esg_reading_regions` 11/11,
`test_esg_reading_regions_real_pages` 21/21.

### Mutation checks — the new tests are load-bearing

Each fix was reverted by monkeypatching and the suites re-run:

| Reverted | Tests that fail |
|---|---|
| `_is_single_column_slice` → always `True` | both `SideBySidePanelTests`, **only** the `year=2024` subtests |
| `MAX_PANEL_PEEL_DEPTH = 0` | 4 real-page (`MultiPanelPageTests` order + infographic/pull_quote continuity, AMZN panel) and both `SpanningPanelTests` |
| `_bridges_row_gap` → always `True` | `test_two_different_layouts_on_one_page_become_two_regions` |

---

## Known remaining imperfections (not regressions)

1. **BBWI p22 emits 7 regions, not 5.** The five *content blocks* are each continuous and
   correctly ordered, but the heading and intro are two regions, and the infographic is two
   **adjacent** regions (`y 204–299` and `y 297–366`) rather than one. The infographic split
   is a `_split_structure_changes` cut at `EDUCATION`, where staggered labels around the
   diversity diamond put one two-segment line among one-segment ones. It costs an extra
   region boundary but **reorders nothing**. The persistence check that would have merged it
   regressed AEO-2023 p8 (see "Tried and reverted"). If picked up again, it needs a rule
   that does not depend on the new structure persisting for N lines.
2. **BBWI p22's infographic reads by visual row**, mixing left-side and right-side diamond
   labels (`FAMILY STATUS SEXUAL ORIENTATION / LANGUAGES / ...`). Column detection legitimately
   declines here — the labels are right-aligned inside their coloured bars, so their starts
   do not repeat. Reading by visual row is the honest fallback and the module's policy is not
   to guess. The infographic's structure lives in the **drawn rectangles**, which
   `reconstruct_by_regions` never sees (it takes words, page width and height only).
   Using page graphics would need a signature change and is a separate decision.
3. **AMZN p63 stays held.** Its two prose columns still mix at the top
   (`Procter & Gamble (including its brands Pampers, Oral-B, and` + `The Sustainability
   Solutions Hub`), and the three bottom-right captions read across. The peel fixed the
   cross-panel heading only.
4. **BBWI p36 stays held**, untouched — its left/right column prose still mixes.
5. **BBW p2**: the `Table of Contents` heading interleaves with the first two contents
   entries (`Table of A Company with 3 / Contents Message from Our CEO 4`). Pre-existing and
   unchanged; the page is otherwise correct (left column, right column, then the report note).
6. `PANEL_*` and `INNER_*` thresholds remain **unvalidated** against a labelled sample — as
   the module docstring already states. `MAX_PANEL_PEEL_DEPTH = 3` is a bound for
   inspectability, not a fitted value.
7. The pilot lists BBW p2 with `role="target"`, so its verdict reads `improved` even though
   its output did not change. Pre-existing bookkeeping quirk in
   `scripts/run_reading_order_pilot.py`.

## Files touched this session

| File | Change |
|---|---|
| `src/esg_reading_regions.py` | Changes A and B |
| `tests/test_esg_reading_regions_real_pages.py` | rewritten — text-order assertions |
| `tests/test_esg_reading_regions.py` | + `SpanningPanelTests` (9 → 11 tests) |
| `scripts/run_reading_order_pilot.py` | report generator: ordering-rules section, peel-depth threshold |
| `reports/reading_order_bundle2_2026-07-30/bundle2_summary.json`, `bundle2_report.md` | regenerated |

## Reproduce

```bash
python scripts/run_reading_order_pilot.py
```
