# Reading order Bundle 2 report

Date: 2026-07-30

This is a default-off candidate. `candidate_ready` is not proof that page order is correct.
Region counts are not proof either: BBWI-2023 p22 produced five regions while emitting three
panels a slice at a time. The regression tests assert the order of the reconstructed text.

## Thresholds added

- Panel gap: 3.5% of page width.
- Panel minimum: 10 words and 12% of page width on each side.
- Panel vertical overlap: at least 30% of the shorter side.
- Shared visual lines: at most 82% for an independent panel split.
- Structure window: 4 visual lines on each side of a possible boundary.
- Column-start change tolerance: 5.5% of content width, with a 10 point floor.
- Panel peel depth: at most 3 nested side-by-side splits per page.

## Ordering rules

- A region is peeled into side-by-side panels **before** it is cut into vertical runs, but
  only when one side reaches across a row gap that would otherwise split the other. That is
  the one layout a run-first reader cannot order: a panel spanning the row breaks beside it
  keeps any run boundary from falling between those rows, so every run holds one slice of
  each panel. Where nothing spans, runs-then-panels is left to do the work unchanged.
- A change in column structure is only a region boundary when the side that reads as
  full-width really consists of one-segment lines. A four-line window under-reports any
  column that happens to be sparse within it, which otherwise invents boundaries inside
  two-panel layouts whose lines interleave.

## Page results

### BBY — BBY-BEST BUY CO INC-2024.pdf page 46 (target)

- Status: `candidate_ready` — regions_classified
- Word preservation: pass (741 source words)
- Visual verdict: improved
- Navigation removed: 4 items
- Regions:
  1. bbox `[35.6, 91.4, 425.7, 125.4]`, heading, 0 column(s), full_width_heading_line
  2. bbox `[36.0, 158.0, 612.1, 202.0]`, single_column_prose, 1 column(s), insufficient_words_for_columns
  3. bbox `[36.0, 219.3, 755.5, 576.2]`, multi_column_prose, 4 column(s), p25_fill=0.441
- Landmarks in output order: Cybersecurity → Securing customer information

### BBWI — BBWI-BATH & BODY WORKS INC-2023.pdf page 22 (target)

- Status: `candidate_ready` — regions_classified
- Word preservation: pass (477 source words)
- Visual verdict: improved
- Navigation removed: 2 items
- Regions:
  1. bbox `[34.0, 59.1, 295.6, 77.1]`, heading, 0 column(s), full_width_heading_line
  2. bbox `[172.0, 126.1, 437.4, 168.6]`, single_column_prose, 1 column(s), insufficient_words_for_columns
  3. bbox `[89.7, 204.4, 498.7, 299.3]`, single_column_prose, 1 column(s), insufficient_words_for_columns
  4. bbox `[77.9, 297.5, 550.4, 366.4]`, single_column_prose, 1 column(s), insufficient_words_for_columns
  5. bbox `[34.0, 403.9, 205.3, 542.3]`, single_column_prose, 1 column(s), no_repeating_column_starts
  6. bbox `[261.1, 419.4, 550.6, 529.2]`, single_column_prose, 1 column(s), no_repeating_column_starts
  7. bbox `[586.8, 272.3, 759.7, 582.0]`, single_column_prose, 1 column(s), no_repeating_column_starts
- Landmarks in output order: WE EMBRACE DIVERSITY → IS ALL OF US

### BBWI — BBWI-BATH & BODY WORKS INC-2023.pdf page 36 (target)

- Status: `candidate_ready` — regions_classified
- Word preservation: pass (693 source words)
- Visual verdict: needs review
- Navigation removed: 2 items
- Regions:
  1. bbox `[34.0, 129.7, 365.7, 152.2]`, single_column_prose, 1 column(s), insufficient_words_for_columns
  2. bbox `[34.0, 149.7, 747.1, 529.8]`, multi_column_prose, 4 column(s), p25_fill=0.442
- Landmarks in output order: Supporting Purpose-Driven Marketing → ACS

### BBW — BBW-BUILD-A-BEAR WORKSHOP INC-2023.pdf page 2 (target)

- Status: `candidate_ready` — regions_classified
- Word preservation: pass (140 source words)
- Visual verdict: improved
- Navigation removed: 8 items
- Regions:
  1. bbox `[35.9, 36.1, 924.0, 355.4]`, multi_column_prose, 2 column(s), p25_fill=0.032
  2. bbox `[323.3, 408.2, 773.0, 471.4]`, single_column_prose, 1 column(s), no_repeating_column_starts
- Landmarks in output order: About This Report

### AMZN — AMZN-AMAZON.COM INC-2023.pdf page 63 (target)

- Status: `candidate_ready` — regions_classified
- Word preservation: pass (457 source words)
- Visual verdict: needs review
- Navigation removed: 11 items
- Regions:
  1. bbox `[72.0, 168.0, 929.6, 225.6]`, single_column_prose, 1 column(s), insufficient_words_for_columns
  2. bbox `[72.0, 226.3, 937.3, 980.7]`, multi_column_prose, 2 column(s), p25_fill=0.531
  3. bbox `[1014.0, 201.6, 1451.7, 277.6]`, single_column_prose, 1 column(s), insufficient_words_for_columns
  4. bbox `[1041.9, 851.1, 1773.6, 940.5]`, single_column_prose, 1 column(s), no_repeating_column_starts
- Landmarks in output order: Sustainability Solutions Hub → Looking Forward

### AEO — AEO-AMERICAN EAGLE OUTFITTERS INC-2023.pdf page 8 (target)

- Status: `candidate_ready` — regions_classified
- Word preservation: pass (50 source words)
- Visual verdict: improved
- Navigation removed: 0 items
- Regions:
  1. bbox `[35.0, 28.1, 646.6, 46.1]`, heading, 0 column(s), full_width_heading_line
  2. bbox `[59.8, 90.0, 223.9, 233.1]`, single_column_prose, 1 column(s), insufficient_words_for_columns
  3. bbox `[333.2, 82.6, 697.7, 300.3]`, single_column_prose, 1 column(s), insufficient_words_for_columns
- Landmarks in output order: 2023 PROGRESS → TOTAL PREFERRED FIBERS

### AEO — AEO-AMERICAN EAGLE OUTFITTERS INC-2024.pdf page 8 (target)

- Status: `candidate_ready` — regions_classified
- Word preservation: pass (58 source words)
- Visual verdict: improved
- Navigation removed: 0 items
- Regions:
  1. bbox `[35.0, 28.1, 646.6, 46.1]`, heading, 0 column(s), full_width_heading_line
  2. bbox `[59.8, 90.0, 224.1, 233.1]`, single_column_prose, 1 column(s), insufficient_words_for_columns
  3. bbox `[333.1, 82.6, 742.7, 300.3]`, single_column_prose, 1 column(s), insufficient_words_for_columns
- Landmarks in output order: 2024 PROGRESS → TOTAL PREFERRED FIBERS

### AEO — AEO-AMERICAN EAGLE OUTFITTERS INC-2023.pdf page 2 (control)

- Status: `candidate_ready` — regions_classified
- Word preservation: pass (52 source words)
- Visual verdict: unchanged
- Navigation removed: 0 items
- Regions:
  1. bbox `[36.0, 410.5, 353.2, 565.0]`, single_column_prose, 1 column(s), insufficient_words_for_columns
- Landmarks in output order: INTRODUCTION

### AEO — AEO-AMERICAN EAGLE OUTFITTERS INC-2023.pdf page 6 (control)

- Status: `candidate_ready` — regions_classified
- Word preservation: pass (152 source words)
- Visual verdict: unchanged
- Navigation removed: 0 items
- Regions:
  1. bbox `[35.0, 28.1, 744.3, 568.4]`, single_column_prose, 1 column(s), no_repeating_column_starts
- Landmarks in output order: 2023 REAL GOOD BY THE NUMBERS

### AEO — AEO-AMERICAN EAGLE OUTFITTERS INC-2023.pdf page 10 (control)

- Status: `candidate_ready` — regions_classified
- Word preservation: pass (135 source words)
- Visual verdict: unchanged
- Navigation removed: 0 items
- Regions:
  1. bbox `[34.0, 28.1, 461.2, 46.1]`, heading, 0 column(s), full_width_heading_line
  2. bbox `[60.8, 93.1, 270.0, 510.0]`, single_column_prose, 1 column(s), no_repeating_column_starts
  3. bbox `[327.7, 72.7, 741.5, 503.8]`, single_column_prose, 1 column(s), no_repeating_column_starts
- Landmarks in output order: 2023 SUSTAINABLE POLYESTER BREAKDOWN

### AEO — AEO-AMERICAN EAGLE OUTFITTERS INC-2023.pdf page 3 (control)

- Status: `candidate_ready` — regions_classified
- Word preservation: pass (308 source words)
- Visual verdict: unchanged
- Navigation removed: 0 items
- Regions:
  1. bbox `[36.1, 36.9, 746.8, 550.5]`, row_structured, 2 column(s), p25_fill=0.083
- Landmarks in output order: BUILDING A BETTER PLANET

### AAPL — AAPL-APPLE INC-2024.pdf page 97 (control)

- Status: `candidate_ready` — regions_classified
- Word preservation: pass (41 source words)
- Visual verdict: unchanged
- Navigation removed: 4 items
- Regions:
  1. bbox `[24.0, 54.0, 170.3, 156.4]`, single_column_prose, 1 column(s), insufficient_words_for_columns
- Landmarks in output order: In this section → Report notes
