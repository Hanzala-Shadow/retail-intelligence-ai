# ESG corpus token recount under the BGE tokenizer

**Date:** 2026-07-29  
**Repository state measured:** `Phase_4.1_Aziz` at `d5ff4617b085167dc51b5c1179a6a094c0f3c586`  
**Scope:** 3,329 chunks from 50 ESG documents  
**Gate:** measurement only; no embedding, enrichment write, reparse, or re-chunk

## Technical summary

- On normalized text, **85 of 3,329 chunks (2.55%) exceed 512 BGE tokens**, including `[CLS]` and `[SEP]`. Against the requested 510 threshold, **93 (2.79%)** exceed the threshold.
- Normalized overflow totals **3,606 tokens beyond 512** and **3,789 tokens beyond 510**. The report gives the same measures for raw text below.
- The reusable calibration ratio `bge_normalized / cl100k_recount` is **0.8808 mean, 0.8852 p50, and 0.9880 p95**. This is below the brief's expected 1.05-1.30 band, so it was checked with the independent slow BERT tokenizer and whitespace diagnostics before reporting.
- The control fully reconciled: **3,329/3,329** `cl100k_base` recounts match the index, with zero discrepancies. The BGE base and large token ID sequences also match on all 3,329 raw texts and all 3,329 normalized texts.
- These numbers measure exposure only. The gate leaves any re-chunk or truncation decision to Aziz and does not set a new chunk size.

## The BGE over-limit population is measurable at both thresholds

The BGE counts below include two special tokens. `>512` is the hard total-input limit. `>510` is also shown as requested to expose the 510-token content budget after reserving two positions for `[CLS]` and `[SEP]`. “Loss >10%” means `(count - cap) / count > 10%`, using the same total token count and a strict greater-than test.

| Text counted | Cap | Chunks over | Share | Total tokens beyond cap | Chunks losing >10% | Share losing >10% |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Raw | 510 | 94 | 2.82% | 3,793 | 12 | 0.36% |
| Raw | 512 | 85 | 2.55% | 3,609 | 11 | 0.33% |
| Normalized | 510 | 93 | 2.79% | 3,789 | 12 | 0.36% |
| Normalized | 512 | 85 | 2.55% | 3,606 | 11 | 0.33% |

Raw text has 85 chunks above 512. In-memory normalization changes that to 85. Normalization reduced the BGE count on 237 chunks, left 2,944 unchanged, and increased 148; its corpus-wide net change is -545 tokens.

## Token-count distributions

Percentiles use the deterministic linear interpolation rule `h=(n-1)q` (Hyndman-Fan type 7). BGE columns include `[CLS]` and `[SEP]`; the cl100k control does not.

| Count | Min | P25 | Median | P75 | P90 | P95 | Max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| cl100k recount (raw; no special tokens) | 55 | 242 | 470 | 500 | 500 | 525.6 | 600 |
| BGE raw (+[CLS]/[SEP]) | 48 | 209 | 382 | 451 | 477 | 498 | 1,153 |
| BGE normalized (+[CLS]/[SEP]) | 48 | 209 | 382 | 451 | 477 | 497 | 1,153 |

`bge_normalized >= cl100k_recount` for only **121/3,329 chunks (3.63%)**, and it is strictly greater for **109/3,329 (3.27%)**. This conflicts with the brief's expected direction, so it is treated as an investigated corpus finding rather than assumed to be correct from plausibility alone.

## The calibration ratio for future planning

The ratio is computed per chunk as `bge_normalized / cl100k_recount`, then summarized across all 3,329 chunks. It includes BGE special tokens in the numerator because those positions are part of the real model input.

| Statistic | Ratio |
| --- | ---: |
| Mean | 0.8808 |
| P50 | 0.8852 |
| P95 | 0.9880 |

This is a measured conversion factor for this corpus. It is not used here to propose a new `CHUNK_SIZE`.

## The below-one ratio is real, not a tokenizer substitution

The expected 1.05-1.30 band did not hold. Four checks rule out the stated failure modes:

1. The tokenizer came from the pinned official `BAAI/bge-base-en-v1.5` snapshot. Its class is `BertTokenizerFast`, vocabulary size is 30,522, and its vocabulary hash is recorded below.
2. Hugging Face's independent slow `BertTokenizer` produced **zero raw and zero normalized token-ID mismatches** against the fast tokenizer across all 3,329 chunks.
3. BGE base and large produced zero token-ID mismatches across the same raw and normalized texts.
4. A normal prose probe, “The quick brown fox jumps over the lazy dog.”, produces 10 cl100k tokens and 12 BGE tokens including special tokens. The expected direction appears on ordinary prose.

The corpus is extracted PDF layout text with frequent line breaks and spacing. BERT's whitespace pre-tokenizer does not allocate tokens to whitespace, while cl100k often allocates tokens to line breaks and layout spacing. An auxiliary whitespace-collapse check moved the ratio close to 1, supporting layout whitespace as a major cause, though not the only cause. Its detailed evidence is saved in `tmp/esg_task1_20260729/bge_ratio_investigation.json`; it does not replace or alter any requested CSV-derived aggregate in this report.

## Truncation is not evenly distributed

At the hard 512-token limit after normalization, affected chunks appear in **17/18 section codes** and **34/50 documents**. The top three section codes account for **49.41% of affected chunks** and **69.08% of normalized overflow tokens beyond 512**. The top five documents account for **42.35% of affected chunks** and **64.48% of overflow tokens**. The full tables make the concentration auditable.

### Breakdown by section code

Rows are sorted by normalized overflow tokens beyond 512, then affected chunks. Rates use each section's own chunk count as denominator.

| section_code | Chunks | Raw >510 | Raw >512 | Norm >510 | Norm >512 | Norm >512 rate | Norm overflow >512 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| other | 33 | 6 | 6 | 6 | 6 | 18.18% | 1,552 |
| human_capital | 674 | 28 | 26 | 27 | 26 | 3.86% | 525 |
| social | 29 | 2 | 2 | 2 | 2 | 6.90% | 414 |
| about_this_report | 210 | 8 | 8 | 8 | 8 | 3.81% | 226 |
| supply_chain_ethics | 260 | 5 | 5 | 5 | 5 | 1.92% | 203 |
| environmental | 302 | 9 | 8 | 9 | 8 | 2.65% | 175 |
| appendix | 144 | 6 | 5 | 6 | 5 | 3.47% | 135 |
| governance | 215 | 2 | 2 | 2 | 2 | 0.93% | 91 |
| ethics_compliance | 167 | 3 | 2 | 3 | 2 | 1.20% | 64 |
| community | 235 | 7 | 6 | 7 | 6 | 2.55% | 50 |
| waste | 256 | 2 | 2 | 2 | 2 | 0.78% | 43 |
| emissions | 169 | 2 | 2 | 2 | 2 | 1.18% | 41 |
| energy | 166 | 5 | 4 | 5 | 4 | 2.41% | 34 |
| diversity_equity_inclusion | 218 | 3 | 3 | 3 | 3 | 1.38% | 18 |
| water | 98 | 2 | 1 | 2 | 1 | 1.02% | 16 |
| climate | 77 | 2 | 2 | 2 | 2 | 2.60% | 11 |
| data_summary | 58 | 2 | 1 | 2 | 1 | 1.72% | 8 |
| ceo_letter | 18 | 0 | 0 | 0 | 0 | 0.00% | 0 |

### Breakdown by document

All 50 documents are shown. Rows are sorted by normalized overflow tokens beyond 512, then affected chunks.

| pdf_stem | Chunks | Raw >510 | Raw >512 | Norm >510 | Norm >512 | Norm >512 rate | Norm overflow >512 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| TPR-TAPESTRY INC-2020 | 83 | 3 | 3 | 3 | 3 | 3.61% | 680 |
| CRMT-AMERICA'S CAR-MART INC-2024 | 46 | 3 | 3 | 3 | 3 | 6.52% | 447 |
| CRMT-AMERICA'S CAR-MART INC-2023 | 46 | 3 | 3 | 3 | 3 | 6.52% | 411 |
| BURL-BURLINGTON STORES INC-2018 | 34 | 1 | 1 | 1 | 1 | 2.94% | 411 |
| PSMT-PRICESMART INC-2021 | 68 | 19 | 17 | 19 | 17 | 25.00% | 376 |
| LESL-LESLIE'S INC-2024 | 43 | 6 | 4 | 6 | 4 | 9.30% | 173 |
| HOFT-HOOKER FURNISHINGS CORP-2021 | 8 | 1 | 1 | 1 | 1 | 12.50% | 148 |
| PRTS-CARPARTS COM INC-2022 | 37 | 5 | 5 | 5 | 5 | 13.51% | 124 |
| LULU-LULULEMON ATHLETICA INC-2024 | 84 | 5 | 5 | 5 | 5 | 5.95% | 115 |
| TPR-TAPESTRY INC-2016 | 77 | 1 | 1 | 1 | 1 | 1.30% | 78 |
| WOOF-PETCO HEALTH AND WELLNESS CO-2023 | 75 | 6 | 5 | 6 | 5 | 6.67% | 68 |
| HD-HOME DEPOT INC-2023 | 167 | 2 | 2 | 2 | 2 | 1.20% | 67 |
| UEIC-UNIVERSAL ELECTRONICS INC-2024 | 47 | 3 | 2 | 3 | 2 | 4.26% | 56 |
| RL-RALPH LAUREN CORP-2021 | 112 | 4 | 4 | 4 | 4 | 3.57% | 53 |
| TPR-TAPESTRY INC-2022 | 98 | 3 | 3 | 3 | 3 | 3.06% | 45 |
| TPR-TAPESTRY INC-2023 | 116 | 2 | 2 | 2 | 2 | 1.72% | 41 |
| ETSY-Etsy-2024 | 47 | 2 | 2 | 2 | 2 | 4.26% | 37 |
| TJX-TJX COS INC (THE)-2017 | 106 | 2 | 2 | 2 | 2 | 1.89% | 33 |
| ULTA-ULTA BEAUTY INC-2024 | 57 | 2 | 2 | 2 | 2 | 3.51% | 32 |
| DECK-DECKERS OUTDOOR CORP-2023 | 322 | 1 | 1 | 1 | 1 | 0.31% | 29 |
| UAA-UNDER ARMOUR INC-2023 | 27 | 1 | 1 | 1 | 1 | 3.70% | 29 |
| WWW-WOLVERINE WORLD WIDE-2020 | 67 | 2 | 2 | 2 | 2 | 2.99% | 23 |
| XOM-EXXON MOBIL CORP-2019 | 65 | 1 | 1 | 1 | 1 | 1.54% | 22 |
| HD-HOME DEPOT INC-2016 | 76 | 1 | 1 | 1 | 1 | 1.32% | 17 |
| EBAY-eBay-2021-Report | 43 | 1 | 1 | 1 | 1 | 2.33% | 15 |
| VSXY-Victorias Secret-2021 | 40 | 1 | 1 | 1 | 1 | 2.50% | 15 |
| MHK-MOHAWK INDUSTRIES INC-2023 | 93 | 1 | 1 | 1 | 1 | 1.08% | 13 |
| AZO-AUTOZONE INC-2023 | 96 | 2 | 2 | 2 | 2 | 2.08% | 10 |
| BURL-BURLINGTON STORES INC-2019 | 49 | 4 | 2 | 4 | 2 | 4.08% | 10 |
| PVH-PVH CORP-2022 | 180 | 1 | 1 | 1 | 1 | 0.56% | 9 |
| COLM-COLUMBIA SPORTSWEAR CO-2023 | 54 | 1 | 1 | 1 | 1 | 1.85% | 8 |
| COLM-COLUMBIA SPORTSWEAR CO-2015 | 42 | 1 | 1 | 1 | 1 | 2.38% | 7 |
| ULTA-ULTA BEAUTY INC-2020 | 47 | 1 | 1 | 1 | 1 | 2.13% | 3 |
| SGC-SUPERIOR GROUP OF COS INC-2024 | 47 | 1 | 1 | 1 | 1 | 2.13% | 1 |
| ABG-ASBURY AUTOMOTIVE GROUP INC-2023 | 68 | 0 | 0 | 0 | 0 | 0.00% | 0 |
| AZO-AUTOZONE INC-2022 | 94 | 1 | 0 | 0 | 0 | 0.00% | 0 |
| AZO-AUTOZONE INC-2025 | 37 | 0 | 0 | 0 | 0 | 0.00% | 0 |
| CAL-CALERES INC-2024 | 12 | 0 | 0 | 0 | 0 | 0.00% | 0 |
| CPNG-Coupang-2022 | 19 | 0 | 0 | 0 | 0 | 0.00% | 0 |
| CRI-CARTER'S INC-2024 | 100 | 0 | 0 | 0 | 0 | 0.00% | 0 |
| FIVE-FIVE BELOW INC-2025 | 11 | 0 | 0 | 0 | 0 | 0.00% | 0 |
| GPI-GROUP 1 AUTOMOTIVE INC-2021 | 38 | 0 | 0 | 0 | 0 | 0.00% | 0 |
| LOW-LOWE_S COS INC-2019 | 75 | 0 | 0 | 0 | 0 | 0.00% | 0 |
| SHOO-MADDEN STEVEN LTD-2019 | 61 | 0 | 0 | 0 | 0 | 0.00% | 0 |
| SHOO-MADDEN STEVEN LTD-2024 | 41 | 0 | 0 | 0 | 0 | 0.00% | 0 |
| SONO-SONOS INC-2018 | 13 | 0 | 0 | 0 | 0 | 0.00% | 0 |
| SVV-Savers Value Village-2025 | 46 | 0 | 0 | 0 | 0 | 0.00% | 0 |
| TDUP-THREDUP INC-2021 | 58 | 0 | 0 | 0 | 0 | 0.00% | 0 |
| VSXY-Victorias Secret-2024 | 48 | 0 | 0 | 0 | 0 | 0.00% | 0 |
| WSM-WILLIAMS-SONOMA INC-2016 | 9 | 0 | 0 | 0 | 0 | 0.00% | 0 |

## Worst 20 chunks by normalized overflow

Ranking uses `max(bge_normalized - 510, 0)`, the requested effective-budget threshold. Ties are broken by normalized count and then `chunk_id`. Both 510- and 512-based overflow are shown.

| chunk_id | section_code | pdf_stem | cl100k | BGE raw | BGE normalized | Overflow >510 | Overflow >512 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| TPR__TPR_TAPESTRY_INC_2020__other__0001__chunk_0000 | other | TPR-TAPESTRY INC-2020 | 284 | 1,153 | 1,153 | 643 | 641 |
| BURL__BURL_BURLINGTON_STORES_INC_2018__social__0001__chunk_0000 | social | BURL-BURLINGTON STORES INC-2018 | 189 | 923 | 923 | 413 | 411 |
| CRMT__CRMT_AMERICA_S_CAR_MART_INC_2023__other__0001__chunk_0000 | other | CRMT-AMERICA'S CAR-MART INC-2023 | 511 | 873 | 873 | 363 | 361 |
| CRMT__CRMT_AMERICA_S_CAR_MART_INC_2024__other__0001__chunk_0000 | other | CRMT-AMERICA'S CAR-MART INC-2024 | 514 | 872 | 872 | 362 | 360 |
| HOFT__HOFT_HOOKER_FURNISHINGS_CORP_2021__other__0001__chunk_0001 | other | HOFT-HOOKER FURNISHINGS CORP-2021 | 282 | 660 | 660 | 150 | 148 |
| LESL__LESL_LESLIE_S_INC_2024__appendix__0002__chunk_0002 | appendix | LESL-LESLIE'S INC-2024 | 583 | 601 | 601 | 91 | 89 |
| TPR__TPR_TAPESTRY_INC_2016__about_this_report__0001__chunk_0000 | about_this_report | TPR-TAPESTRY INC-2016 | 586 | 590 | 590 | 80 | 78 |
| PSMT__PSMT_PRICESMART_INC_2021__governance__0001__chunk_0001 | governance | PSMT-PRICESMART INC-2021 | 523 | 575 | 574 | 64 | 62 |
| LULU__LULU_LULULEMON_ATHLETICA_INC_2024__environmental__0007__chunk_0000 | environmental | LULU-LULULEMON ATHLETICA INC-2024 | 593 | 572 | 572 | 62 | 60 |
| PRTS__PRTS_CARPARTS_COM_INC_2022__about_this_report__0002__chunk_0001 | about_this_report | PRTS-CARPARTS COM INC-2022 | 575 | 572 | 572 | 62 | 60 |
| CRMT__CRMT_AMERICA_S_CAR_MART_INC_2024__human_capital__0003__chunk_0006 | human_capital | CRMT-AMERICA'S CAR-MART INC-2024 | 590 | 570 | 570 | 60 | 58 |
| HD__HD_HOME_DEPOT_INC_2023__ethics_compliance__0003__chunk_0000 | ethics_compliance | HD-HOME DEPOT INC-2023 | 596 | 567 | 567 | 57 | 55 |
| UEIC__UEIC_UNIVERSAL_ELECTRONICS_INC_2024__supply_chain_ethics__0003__chunk_0001 | supply_chain_ethics | UEIC-UNIVERSAL ELECTRONICS INC-2024 | 588 | 566 | 566 | 56 | 54 |
| CRMT__CRMT_AMERICA_S_CAR_MART_INC_2023__human_capital__0004__chunk_0013 | human_capital | CRMT-AMERICA'S CAR-MART INC-2023 | 587 | 559 | 559 | 49 | 47 |
| LESL__LESL_LESLIE_S_INC_2024__supply_chain_ethics__0001__chunk_0001 | supply_chain_ethics | LESL-LESLIE'S INC-2024 | 588 | 553 | 553 | 43 | 41 |
| TPR__TPR_TAPESTRY_INC_2022__supply_chain_ethics__0009__chunk_0002 | supply_chain_ethics | TPR-TAPESTRY INC-2022 | 561 | 552 | 552 | 42 | 40 |
| PSMT__PSMT_PRICESMART_INC_2021__human_capital__0003__chunk_0003 | human_capital | PSMT-PRICESMART INC-2021 | 499 | 551 | 551 | 41 | 39 |
| RL__RL_RALPH_LAUREN_CORP_2021__supply_chain_ethics__0004__chunk_0000 | supply_chain_ethics | RL-RALPH LAUREN CORP-2021 | 595 | 551 | 551 | 41 | 39 |
| LULU__LULU_LULULEMON_ATHLETICA_INC_2024__waste__0004__chunk_0000 | waste | LULU-LULULEMON ATHLETICA INC-2024 | 576 | 548 | 549 | 39 | 37 |
| PSMT__PSMT_PRICESMART_INC_2021__human_capital__0003__chunk_0002 | human_capital | PSMT-PRICESMART INC-2021 | 500 | 548 | 548 | 38 | 36 |

## The 500 target can produce up to 600 cl100k tokens

The configured target and overlap are `CHUNK_SIZE = 500` and `OVERLAP = 50`, but the hard chunk validity ceiling is separately set to `MAX_CHUNK_TOKENS = 600` (`src/esg_chunker.py:19-23`). The code therefore treats 500 as a target, not a maximum.

There are three bounded paths that allow overshoot:

1. A whole source section from 100 through 600 tokens is returned as one range (`src/esg_chunker.py:508-513`). This keeps a 501-600-token section intact.
2. While splitting a longer section, a final remainder below 100 tokens is absorbed when the current source-aligned span remains at or below 600 (`src/esg_chunker.py:518-526`). A normal 500-token range can therefore grow by up to 99 tokens through this tail rule.
3. The source-aligned slice is re-tokenized and accepted whenever it is between 100 and 600 tokens (`src/esg_chunker.py:565-583`). The later guard also checks against 600, not 500 (`src/esg_chunker.py:1071-1095`). Source-alignment effects may therefore use the full ceiling.

The absolute configured overshoot is **100 tokens**, from the 500 target to the 600 maximum. Tail absorption alone reaches at most 599 because the remainder test is strictly `< 100`; whole-section preservation or source-aligned re-tokenization can reach 600. Any future target would need to account for this separate ceiling and tail rule, but this gate does not choose that target.

## Tokenizer identity is pinned and covers base and large

Only tokenizer files were downloaded. No model weights, embeddings, or vectors were created.

| Model | Resolved Hugging Face commit | Vocab size | vocab.txt SHA256 | tokenizer.json SHA256 | Max length |
| --- | ---: | ---: | ---: | ---: | ---: |
| BAAI/bge-base-en-v1.5 | a5beb1e3e68b9ab74eb54cfd186867f64f240e1a | 30522 | 07eced375cec144d27c900241f3e339478dec958f92fddbc551f295c992038a3 | d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66 | 512 |
| BAAI/bge-large-en-v1.5 | d4aa6901d3a41ba39fb536a557fa166f842b0e09 | 30522 | 07eced375cec144d27c900241f3e339478dec958f92fddbc551f295c992038a3 | d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66 | 512 |

The two vocabularies are byte-identical, and the serialized `tokenizer.json` files are also byte-identical. `tokenizer_config.json` and `special_tokens_map.json` match as well; only model architecture configuration differs. As an end-to-end check, base and large produced zero token-ID sequence mismatches across all raw and normalized corpus texts. The reported counts therefore cover both `BAAI/bge-base-en-v1.5` and `BAAI/bge-large-en-v1.5` at the pinned commits.

Software used: Python 3.13.2, `tiktoken` 0.13.0, `transformers` 4.57.6, and `tokenizers` 0.22.2.

## The cl100k control reconciles exactly

The index was read by column name from a single byte snapshot. It parsed on attempt 1 with 3,329 rows. Every file existed, decoded as strict UTF-8, was non-empty, and matched its indexed character count. All 3,329 rows have `status=ok`.

| Control check | Measured | Expected baseline | Result |
| --- | ---: | ---: | --- |
| Minimum | 55 | 55 | Pass |
| Median | 470 | 470 | Pass |
| Maximum | 600 | 600 | Pass |
| `>448` | 1,718 | 1,718 | Pass |
| `>480` | 1,628 | 1,628 | Pass |
| `>512` | 192 | 192 | Pass |
| `>576` | 51 | 51 | Pass |
| Exact row matches | 3,329/3,329 | 3,329/3,329 | Pass |

Discrepancy list: **none**.

## Method and output definitions

- Iteration order is ascending `chunk_id`.
- Raw chunk files are read as strict UTF-8. Missing, unreadable, or empty text writes `tmp/esg_task1_20260729/esg_bge_recount_failure.json` and stops the run.
- `cl100k_recount` is `cl100k_base` over raw text with no added special tokens.
- `bge_raw` is the pinned BGE tokenizer over raw text with `add_special_tokens=True` and `truncation=False`.
- `bge_normalized` applies `normalize_for_embedding()` from `src/esg_p1_enrichment.py:228-241` in memory, then uses the same BGE settings.
- CSV `over_510`, `over_512`, and `overflow_tokens` refer to `bge_normalized`; `overflow_tokens = max(bge_normalized - 510, 0)`. Raw-threshold aggregates remain directly derivable from `bge_raw`.
- Input index SHA256: `b4dc5776e39d5a75ce5213fd51f876749b4b6323ad5077c79c9a4b027482a813`.
- Chunk corpus SHA256 before and after each count run: `7541395efea6a96358528a5ed44984e5466847a376b48e3fdfdf6e8ccda86249`.
- Normalizer file SHA256: `aab730ec1146fb9ddaf2cc28da22707938c904b8674588ab70f1345f564804fc`.
- Primary CSV SHA256: `d5dd06301f8aade3bb5ed7999e30cb2e2aa9fd0730fb85254145607803cd5c46`.
- Auxiliary tokenizer-ratio diagnostic SHA256: `eb5604fbd2e22310de146430ed1378ce8e88e8356920ee0607fca240ac76e4a3`.

Exact tables are used instead of charts because this is an audit-focused Markdown deliverable and the requested evidence is exact counts, thresholds, group rows, and chunk identifiers. A chart would not replace any required table.

## Robustness and deterministic rerun

The recount was run twice into separate CSV files. Both files have SHA256 `d5dd06301f8aade3bb5ed7999e30cb2e2aa9fd0730fb85254145607803cd5c46` and are byte-identical. Each run also hashed all 3,329 chunk paths and bytes before and after counting; the corpus hash stayed `7541395efea6a96358528a5ed44984e5466847a376b48e3fdfdf6e8ccda86249`.

The measurement is descriptive. It does not test retrieval quality, embedding quality, or the semantic effect of truncation. It also does not assume that accepting truncation or re-chunking is the right decision.

## Exact reproducibility commands run

Run from the repository root in PowerShell:

```powershell
git status --short --branch
git rev-parse --abbrev-ref HEAD
git rev-parse HEAD
Get-ChildItem -LiteralPath 'data\04_chunks\esg' -Recurse -File -Filter '*.txt' | Measure-Object | Select-Object -ExpandProperty Count
& '.\venv\Scripts\python.exe' -m venv 'tmp\esg_task1_20260729\venv_bge'
& '.\tmp\esg_task1_20260729\venv_bge\Scripts\python.exe' -m pip install 'tiktoken==0.13.0' 'transformers==4.57.6'
$env:HF_HOME = (Resolve-Path 'tmp\esg_task1_20260729').Path + '\hf_home'
$env:HF_HUB_DISABLE_XET = '1'
& '.\tmp\esg_task1_20260729\venv_bge\Scripts\python.exe' '.\tmp\esg_task1_20260729\fetch_bge_tokenizers.py'
& '.\tmp\esg_task1_20260729\venv_bge\Scripts\python.exe' '.\tmp\esg_task1_20260729\measure_bge_tokens.py'
& '.\tmp\esg_task1_20260729\venv_bge\Scripts\python.exe' '.\tmp\esg_task1_20260729\measure_bge_tokens.py' --output '.\tmp\esg_task1_20260729\esg_bge_token_counts.rerun.csv' --metadata-output '.\tmp\esg_task1_20260729\esg_bge_recount_metadata.rerun.json'
& '.\tmp\esg_task1_20260729\venv_bge\Scripts\python.exe' '.\tmp\esg_task1_20260729\investigate_tokenizer_ratio.py'
$first = Get-FileHash -Algorithm SHA256 -LiteralPath 'tmp\esg_task1_20260729\esg_bge_token_counts.csv'
$second = Get-FileHash -Algorithm SHA256 -LiteralPath 'tmp\esg_task1_20260729\esg_bge_token_counts.rerun.csv'
[System.Linq.Enumerable]::SequenceEqual([System.IO.File]::ReadAllBytes((Resolve-Path $first.Path)), [System.IO.File]::ReadAllBytes((Resolve-Path $second.Path)))
& '.\tmp\esg_task1_20260729\venv_bge\Scripts\python.exe' '.\tmp\esg_task1_20260729\summarize_bge_counts.py'
& '.\tmp\esg_task1_20260729\venv_bge\Scripts\python.exe' '.\tmp\esg_task1_20260729\validate_bge_report.py'
git status --short --branch
git rev-parse HEAD
```

## Gate boundary

I did **not** run P1 enrichment as a script, write normalized chunk files, run the chunker or parser, reparse, re-chunk, embed text, create vectors, change `requirements.txt`, modify the project virtual environment, or perform any Git pull, merge, rebase, checkout, stage, commit, or push. I made no recommendation for a new chunk size or embedding model. This report is the stopping point requested by the gate.
