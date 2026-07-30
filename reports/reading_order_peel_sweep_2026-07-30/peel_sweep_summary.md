# Reading-order peel sweep — blast radius measurement

Date: 2026-07-30
Seed: 20260730

This is a measurement-only pass over `src/esg_reading_regions.py`'s panel-peel
change (peel-before-runs, gated by `_bridges_row_gap`, capped by
`MAX_PANEL_PEEL_DEPTH = 3`). No source file was modified or reparsed; nothing here
is wired into production. Each sampled page was run twice in-process: once as
shipped, once with `MAX_PANEL_PEEL_DEPTH` monkeypatched to 0.

## Sample: 448 pages

Documents sampled: 32 across 32 tickers,
one document per ticker, 14 body pages targeted per document; pages with no body words skipped.
Documents by report year: 2016: 1, 2018: 2, 2019: 3, 2020: 1, 2021: 5, 2022: 5, 2023: 6, 2024: 8, 2025: 1
Exact page list: see `sampled_pages.txt` in this directory.

## 1. Peel fire rate by recursion depth

| Depth | Attempts reached | Fired attempts | Pages fired | Page fire rate |
|---|---|---|---|---|
| 0 | 431 | 69 | 69 | 15.4% |
| 1 | 138 | 4 | 4 | 0.89% |
| 2 | 8 | 0 | 0 | 0.0% |

Pages where the peel fired at least once (any depth): 69 / 448 (15.4%)

## 2. Region-count delta distribution (enabled minus disabled)

| Delta | Pages |
|---|---|
| -2 | 1 |
| -1 | 3 |
| +0 | 388 |
| +1 | 15 |
| +2 | 14 |
| +3 | 13 |
| +4 | 6 |
| +5 | 2 |
| +6 | 2 |
| +7 | 1 |
| +8 | 3 |

## 3. Text-change rate

69 / 448 pages (15.4%) had a different `candidate.text` between the
two configurations.

## 4. Status flips

- `needs_review -> candidate_ready`: 1
- `candidate_ready -> needs_review`: 0

| Page | Flip (disabled -> enabled) |
|---|---|
| JWN|JWN-NORSTROM-2024.pdf|5 | needs_review->candidate_ready |

## 5. Word preservation

`preservation_ratio == 1.0` in both configurations on all 448 sampled pages.

## 6. Gate effectiveness (`_bridges_row_gap`)

Counts only strips that already cleared every other `_panel_split` guard (gap width,
word count, side width, vertical overlap, shared-line share) — i.e. every call here
is a case the gate alone decided.
The instrumentation scans every such strip at each reached peel node, including strips
that the shipped widest-first selection would not reach after accepting an earlier strip.

- Total qualifying strips evaluated: 137
- Accepted (bridges a row gap, peel allowed): 89
- Rejected (does not bridge, peel blocked): 48
- Pages with at least one qualifying strip REJECTED by the gate: 43
- Pages with at least one qualifying strip ACCEPTED by the gate: 69

Rejected count is non-trivial: the gate is doing real, load-bearing work rejecting strips that pass every other guard but do not bridge a row gap.

## Changed-text pages (candidates for the paired-render review set)

69 pages had a text difference. The paired, unscored review set has
15 pages: `renders/review_set/review_set.html`.

- `WWW|WWW-WOLVERINE WORLD WIDE-2023.pdf|5` — regions 3 -> 5 (+2), status candidate_ready -> candidate_ready
- `WWW|WWW-WOLVERINE WORLD WIDE-2023.pdf|17` — regions 7 -> 8 (+1), status candidate_ready -> candidate_ready
- `BBW|BBW-BUILD-A-BEAR WORKSHOP INC-2023.pdf|26` — regions 5 -> 8 (+3), status candidate_ready -> candidate_ready
- `BBW|BBW-BUILD-A-BEAR WORKSHOP INC-2023.pdf|8` — regions 3 -> 4 (+1), status candidate_ready -> candidate_ready
- `BBW|BBW-BUILD-A-BEAR WORKSHOP INC-2023.pdf|31` — regions 3 -> 6 (+3), status candidate_ready -> candidate_ready
- `ORLY|ORLY-O'REILLY AUTOMOTIVE INC-2024.pdf|12` — regions 3 -> 5 (+2), status candidate_ready -> candidate_ready
- `ORLY|ORLY-O'REILLY AUTOMOTIVE INC-2024.pdf|30` — regions 2 -> 4 (+2), status candidate_ready -> candidate_ready
- `KSS|KSS-KOHL'S-2024.pdf|27` — regions 7 -> 6 (-1), status candidate_ready -> candidate_ready
- `KSS|KSS-KOHL'S-2024.pdf|7` — regions 3 -> 4 (+1), status candidate_ready -> candidate_ready
- `KSS|KSS-KOHL'S-2024.pdf|18` — regions 5 -> 8 (+3), status candidate_ready -> candidate_ready
- `KSS|KSS-KOHL'S-2024.pdf|22` — regions 1 -> 7 (+6), status candidate_ready -> candidate_ready
- `CASY|CASY-CASEYS GENERAL STORES INC-2022.pdf|18` — regions 4 -> 4 (+0), status candidate_ready -> candidate_ready
- `CASY|CASY-CASEYS GENERAL STORES INC-2022.pdf|11` — regions 5 -> 6 (+1), status candidate_ready -> candidate_ready
- `CVS|CVS-CVS HEALTH CORP-2020.pdf|92` — regions 2 -> 5 (+3), status candidate_ready -> candidate_ready
- `CVS|CVS-CVS HEALTH CORP-2020.pdf|49` — regions 1 -> 3 (+2), status candidate_ready -> candidate_ready
- `CVS|CVS-CVS HEALTH CORP-2020.pdf|28` — regions 3 -> 3 (+0), status candidate_ready -> candidate_ready
- `BBWI|BBWI-BATH & BODY WORKS INC-2023.pdf|69` — regions 2 -> 2 (+0), status candidate_ready -> candidate_ready
- `BBWI|BBWI-BATH & BODY WORKS INC-2023.pdf|37` — regions 1 -> 7 (+6), status candidate_ready -> candidate_ready
- `JWN|JWN-NORSTROM-2024.pdf|5` — regions 2 -> 6 (+4), status needs_review -> candidate_ready
- `JWN|JWN-NORSTROM-2024.pdf|33` — regions 4 -> 5 (+1), status candidate_ready -> candidate_ready
- `JWN|JWN-NORSTROM-2024.pdf|40` — regions 2 -> 4 (+2), status candidate_ready -> candidate_ready
- `AN|AN-AUTONATION INC-2023.pdf|28` — regions 2 -> 3 (+1), status candidate_ready -> candidate_ready
- `DKS|DKS-DICKS SPORTING GOODS INC-2018.pdf|73` — regions 2 -> 5 (+3), status candidate_ready -> candidate_ready
- `DKS|DKS-DICKS SPORTING GOODS INC-2018.pdf|62` — regions 3 -> 7 (+4), status candidate_ready -> candidate_ready
- `DKS|DKS-DICKS SPORTING GOODS INC-2018.pdf|75` — regions 3 -> 8 (+5), status candidate_ready -> candidate_ready
- `ACI|ACI-ALBERTSONS COS INC-2022.pdf|29` — regions 1 -> 9 (+8), status candidate_ready -> candidate_ready
- `ACI|ACI-ALBERTSONS COS INC-2022.pdf|4` — regions 2 -> 3 (+1), status candidate_ready -> candidate_ready
- `ACI|ACI-ALBERTSONS COS INC-2022.pdf|30` — regions 5 -> 13 (+8), status candidate_ready -> candidate_ready
- `ACI|ACI-ALBERTSONS COS INC-2022.pdf|13` — regions 5 -> 9 (+4), status candidate_ready -> candidate_ready
- `SGI|SGI-SOMNIGROUP INTERNATIONAL INC-2021.pdf|6` — regions 2 -> 4 (+2), status candidate_ready -> candidate_ready
- `SGI|SGI-SOMNIGROUP INTERNATIONAL INC-2021.pdf|5` — regions 4 -> 6 (+2), status candidate_ready -> candidate_ready
- `SGI|SGI-SOMNIGROUP INTERNATIONAL INC-2021.pdf|16` — regions 3 -> 3 (+0), status candidate_ready -> candidate_ready
- `SGI|SGI-SOMNIGROUP INTERNATIONAL INC-2021.pdf|18` — regions 3 -> 3 (+0), status candidate_ready -> candidate_ready
- `SGI|SGI-SOMNIGROUP INTERNATIONAL INC-2021.pdf|19` — regions 3 -> 7 (+4), status candidate_ready -> candidate_ready
- `SGI|SGI-SOMNIGROUP INTERNATIONAL INC-2021.pdf|10` — regions 4 -> 5 (+1), status candidate_ready -> candidate_ready
- `SGI|SGI-SOMNIGROUP INTERNATIONAL INC-2021.pdf|25` — regions 3 -> 5 (+2), status candidate_ready -> candidate_ready
- `LESL|LESL-LESLIE'S INC-2021.pdf|10` — regions 4 -> 6 (+2), status candidate_ready -> candidate_ready
- `LESL|LESL-LESLIE'S INC-2021.pdf|21` — regions 3 -> 4 (+1), status candidate_ready -> candidate_ready
- `LESL|LESL-LESLIE'S INC-2021.pdf|13` — regions 3 -> 5 (+2), status candidate_ready -> candidate_ready
- `LESL|LESL-LESLIE'S INC-2021.pdf|33` — regions 2 -> 6 (+4), status candidate_ready -> candidate_ready
- `NWL|NWL-NEWELL BRANDS INC-2025.pdf|28` — regions 2 -> 3 (+1), status candidate_ready -> candidate_ready
- `NWL|NWL-NEWELL BRANDS INC-2025.pdf|15` — regions 1 -> 4 (+3), status candidate_ready -> candidate_ready
- `NWL|NWL-NEWELL BRANDS INC-2025.pdf|33` — regions 4 -> 7 (+3), status candidate_ready -> candidate_ready
- `FOSL|FOSL-FOSSIL GROUP INC-2021.pdf|32` — regions 7 -> 5 (-2), status candidate_ready -> candidate_ready
- `FOSL|FOSL-FOSSIL GROUP INC-2021.pdf|33` — regions 6 -> 6 (+0), status candidate_ready -> candidate_ready
- `KR|KR-KROGER CO-2023.pdf|42` — regions 4 -> 9 (+5), status candidate_ready -> candidate_ready
- `KR|KR-KROGER CO-2023.pdf|71` — regions 3 -> 6 (+3), status candidate_ready -> candidate_ready
- `KR|KR-KROGER CO-2023.pdf|48` — regions 4 -> 8 (+4), status candidate_ready -> candidate_ready
- `RCKY|RCKY-ROCKY BRANDS INC-2023.pdf|40` — regions 3 -> 6 (+3), status candidate_ready -> candidate_ready
- `RCKY|RCKY-ROCKY BRANDS INC-2023.pdf|42` — regions 2 -> 5 (+3), status candidate_ready -> candidate_ready
- `RCKY|RCKY-ROCKY BRANDS INC-2023.pdf|33` — regions 3 -> 6 (+3), status candidate_ready -> candidate_ready
- `RCKY|RCKY-ROCKY BRANDS INC-2023.pdf|45` — regions 4 -> 5 (+1), status candidate_ready -> candidate_ready
- `RCKY|RCKY-ROCKY BRANDS INC-2023.pdf|28` — regions 4 -> 6 (+2), status candidate_ready -> candidate_ready
- `RCKY|RCKY-ROCKY BRANDS INC-2023.pdf|34` — regions 3 -> 3 (+0), status candidate_ready -> candidate_ready
- `TPR|TPR-TAPESTRY INC-2022.pdf|59` — regions 4 -> 3 (-1), status candidate_ready -> candidate_ready
- `BIRD|BIRD-ALLBIRDS INC-2022.pdf|16` — regions 4 -> 5 (+1), status candidate_ready -> candidate_ready
- `BIRD|BIRD-ALLBIRDS INC-2022.pdf|13` — regions 3 -> 5 (+2), status candidate_ready -> candidate_ready
- `TGT|TGT-TARGET CORP-2019.pdf|8` — regions 3 -> 11 (+8), status candidate_ready -> candidate_ready
- `TGT|TGT-TARGET CORP-2019.pdf|14` — regions 7 -> 10 (+3), status candidate_ready -> candidate_ready
- `TGT|TGT-TARGET CORP-2019.pdf|111` — regions 3 -> 4 (+1), status candidate_ready -> candidate_ready
- `TGT|TGT-TARGET CORP-2019.pdf|16` — regions 5 -> 8 (+3), status candidate_ready -> candidate_ready
- `TGT|TGT-TARGET CORP-2019.pdf|104` — regions 3 -> 4 (+1), status candidate_ready -> candidate_ready
- `PLCE|PLCE-The Childrens Place-2021.pdf|70` — regions 5 -> 7 (+2), status candidate_ready -> candidate_ready
- `PLCE|PLCE-The Childrens Place-2021.pdf|82` — regions 4 -> 5 (+1), status candidate_ready -> candidate_ready
- `JILL|JILL-J.Jill-2024.pdf|18` — regions 5 -> 4 (-1), status candidate_ready -> candidate_ready
- `JILL|JILL-J.Jill-2024.pdf|35` — regions 4 -> 11 (+7), status candidate_ready -> candidate_ready
- `COLM|COLM-COLUMBIA SPORTSWEAR CO-2019.pdf|29` — regions 3 -> 3 (+0), status candidate_ready -> candidate_ready
- `WOOF|WOOF-PETCO HEALTH AND WELLNESS CO-2022.pdf|25` — regions 3 -> 3 (+0), status candidate_ready -> candidate_ready
- `WOOF|WOOF-PETCO HEALTH AND WELLNESS CO-2022.pdf|44` — regions 2 -> 4 (+2), status candidate_ready -> candidate_ready