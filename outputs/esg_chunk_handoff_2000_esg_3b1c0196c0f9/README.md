# ESG chunk validation sample

**Dataset id:** `esg_3b1c0196c0f9`
**Chunks:** 2,000 — drawn from a safe retrieval pool of 9,259
**Documents:** 188 · **Companies:** 105 · **Topics:** 18
**Quality tiers:** layout_sensitive 285, narrative 1715
**Reporting years:** 2023 1041, 2024 944, 2025 15

## What this is

Chunks of text extracted from corporate sustainability reports, for hand
validation. `chunk_text` is what you are validating. `embedding_text` is the
same text with a metadata header on top — that is what the search engine reads.

This is a **designed sample, not a random one**. Every ESG topic in the corpus
gets a minimum allocation so none is missed; the rest is spread in proportion to
how common each topic is. Within a topic the narrative / layout_sensitive mix
follows the corpus, and no single report contributes more than
15 chunks. Seed `20260731` — the same corpus and seed
reproduce this exact sample.

## Coverage

| ESG topic | in sample | in corpus |
|---|---:|---:|
| about_this_report | 113 | 528 |
| appendix | 83 | 347 |
| ceo_letter | 31 | 38 |
| climate | 81 | 336 |
| community | 145 | 714 |
| data_summary | 58 | 198 |
| diversity_equity_inclusion | 118 | 555 |
| emissions | 96 | 427 |
| energy | 90 | 387 |
| environmental | 158 | 792 |
| ethics_compliance | 128 | 613 |
| governance | 146 | 724 |
| human_capital | 304 | 1,666 |
| other | 31 | 35 |
| social | 40 | 89 |
| supply_chain_ethics | 184 | 951 |
| waste | 129 | 621 |
| water | 65 | 238 |

**Every topic in the corpus is represented.**

## How to review and return results

Use `review_template.csv`. Keep both `chunk_id` and `chunk_text_sha256`, then
fill in:

- `review_status`: `pass` or `fail`
- `issue_type`: short category such as `order`, `cutoff`, `metadata`, or `table`
- `notes`: a short explanation for failed rows

Please return both `chunk_id` and `chunk_text_sha256`.

This matters. The corpus is going to be rebuilt, and `chunk_id` values will
change when it is rebuilt.

`chunk_text_sha256` is a fingerprint of the text itself, so it does not change
unless the text changes. If your returned file carries both columns, every
judgement whose text is unchanged is carried forward automatically after the
rebuild, and only genuinely changed chunks need looking at again.

If only `chunk_id` comes back, the work cannot be matched up after the rebuild.

## Known issues — please do not spend time reporting these

- **Messy `section_title`.** Some are table fragments rather than headings,
  e.g. `| Workers' compensation claims | 94 | 162 | NA |`. Already logged.
- **`layout_sensitive` chunks** come from tables and dense layouts. Row and
  column relationships may be weakened, so a number may sit further from its
  label than it did on the page. Flag these only if the *meaning* is wrong.
- **Scope.** The source corpus has 202 reports. This handoff contains chunks
  from 188 of them and covers reporting years 2023–2025.

## Do tell us about

- Text that is scrambled, out of order, or has words split oddly
- A chunk whose `report_year`, `company_name` or `ticker` looks wrong
- A chunk that is unusable as evidence for answering a question
- Anything that reads as though it came from a different company or year
