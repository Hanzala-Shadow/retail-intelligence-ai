# RAG Retrieval Evaluation Harness

Owner: Aziz — Document Intelligence and RAG Evaluation Lead

Script: `src/rag_eval_harness.py`
Tests: `tests/test_rag_eval_harness.py`

## Purpose

`src/embedding_model_benchmark.py` measures throughput, memory and vector
health. It states its own limit:

```text
This is an engineering throughput and resource benchmark. It does not measure
retrieval relevance. The production model must not be selected from speed or
supplied MTEB scores alone. Final selection requires the approved query set,
relevance judgments, and retrieval metrics including MRR@10, Recall@5,
nDCG@10, and wrong-document-type rate.
```

This harness is that missing step. It turns each model's ranked retrieval
results into the gated metrics that decide the production model.

## Pipeline position

```text
approved question set  ->|
                         |
each model's top-k       ->|  rag_eval_harness  ->  metrics + gates + verdict
                         |
chunk metadata export  ->|
```

## Inputs

### 1. Question set (`--questions`)

Defaults to `data/00_reference/rag_eval_questions.csv`, which mirrors the
"10-K RAG Benchmark Questions" sheet in the Drive `Evaluation/Annual Reports
Evaluation` folder. The sheet is the source of truth; the repository copy must
be exported from it, never edited directly.

24 scored questions across 6 groups, plus 5 refusal questions.

Multi-value fields are pipe-separated and **positional**: value *i* describes
supporting chunk *i*. `required_doc_type` is the exception — it is read as the
set of permitted document types, so `10-K` and `10-K|10-K` behave identically.

### 2. Retrieval results (`--retrieval`, required)

**This is the file the bake-off must produce.** One row per retrieved chunk.

| Column        | Meaning                                    |
| ------------- | ------------------------------------------ |
| `model_id`    | Must match the ids in `config/embedding_benchmark_models.yaml`, e.g. `bge_base_en_v1_5` |
| `question_id` | From the question set, e.g. `10K-V2-XC-004` |
| `rank`        | 1-based position in the ranking            |
| `chunk_id`    | The retrieved chunk                        |

```csv
model_id,question_id,rank,chunk_id
bge_base_en_v1_5,10K-V2-I1-001,1,238872
bge_base_en_v1_5,10K-V2-I1-001,2,293893
```

Rejected on load, rather than scored wrongly:

- ranks that are not contiguous from 1 within a `(model_id, question_id)`
- the same `chunk_id` twice within one question
- missing required columns

A `score` column is permitted and ignored.

**Depth matters.** MRR@10 and nDCG@10 cannot be computed from 5 results. If
depth is under 10 the harness reports the `@10` figures as truncated and warns;
it does not present them as true `@10`. See Open questions.

### 3. Chunk metadata (`--chunk-metadata`)

A CSV export of the `rag_eligible_10k_chunks` view. Optional to run, but
**required for the hard gates** — without it every gate reports
`NOT_EVALUATED` and no verdict is produced.

| Column         | Enables                                   |
| -------------- | ----------------------------------------- |
| `chunk_id`     | mandatory                                 |
| `doc_type`     | the `wrong_doc_type` gate                 |
| `ticker`       | `gold_integrity`                          |
| `filing_year`  | `gold_integrity`                          |
| `section_code` | `gold_integrity`                          |
| `chunk_text`   | `evidence_present`                        |

`--text-field` selects the text column and defaults to `chunk_text`. Do not
point it at `embedding_text`: that column is regex-cleaned for embedding and
may not contain a quoted passage verbatim.

## Metrics

Binary relevance. A question's relevant set is its `supporting_chunk_ids`, so a
cross-company question has two relevant chunks and finding one scores 0.5
recall.

| Metric        | Definition                                        |
| ------------- | ------------------------------------------------- |
| `recall_at_5` | fraction of relevant chunks appearing in the top 5 |
| `hit_at_5`    | 1 if any relevant chunk is in the top 5            |
| `mrr_at_10`   | 1 / rank of the first relevant chunk within 10     |
| `ndcg_at_10`  | standard nDCG, ideal capped at k                   |

`mrr_at_5` and `ndcg_at_5` are also reported. Every metric is given overall and
per question group.

Refusal questions have no supporting chunks and are **excluded from retrieval
scoring**. They test answer-generation behaviour, not the retriever.

## Hard gates

Gates are evaluated before scores. **A model whose gates do not pass cannot win
on score**, however good its numbers.

| Status          | Meaning                                              |
| --------------- | ---------------------------------------------------- |
| `PASS`          | checked and clean                                     |
| `FAIL`          | checked and violated                                  |
| `NOT_EVALUATED` | could not be checked — blocks a verdict, never passes |

`NOT_EVALUATED` is deliberate. A gate that cannot see its data reports that it
saw nothing; it does not report success.

### `wrong_doc_type`

Every retrieved chunk's `doc_type` must be permitted by its question.
`wrong_doc_type_rate` must be 0 — a single chunk of the wrong document type
eliminates the model regardless of score.

A retrieved chunk absent from the metadata export is `NOT_EVALUATED`, not
ignored: an unknown chunk cannot be certified as the right type.

### `gold_integrity`

Every supporting chunk named by the question set must actually belong to the
ticker, filing year and section the set claims for it. This is a property of
the contract, not of a model, so it is identical for every model — a broken
contract invalidates the whole bake-off.

It also fails when a positional field's length disagrees with the number of
supporting chunks, since such a row cannot be checked at all.

### `evidence_present`

Every `supporting_passage` must appear **verbatim and unbroken** in the chunk
cited as its evidence.

Folded before comparison, because extraction artifacts are not quoting errors:

- typographic quotes, apostrophes and dashes (`U+201C`, `U+2019`, `U+2014`, …)
- non-breaking spaces and collapsed whitespace
- a space before punctuation (`"reputation ."`), common in extracted filings

Not accepted: an **elided quote** — one splicing non-adjacent excerpts with an
ellipsis. Elision can silently drop the qualifier a figure depends on. The
known case is XC-004's Grow Generation passage, whose ellipsis removes "for the
year ended December 31, 2022" — the year its own expected answer asserts. The
fix belongs in the sheet, not in the matcher.

## Model selection rule

From Ayse Cetinel's 2026-07-16 email:

> BGE Base is the default winner. BGE Large replaces it only if two conditions
> are both met: MRR improves by at least 0.03 absolute points, and that
> improvement holds across at least 4 of the 6 question groups.

Implemented exactly:

- default `bge_base_en_v1_5`, challenger `bge_large_en_v1_5`
- condition 1: overall improvement in the decision metric >= 0.03
- condition 2: at least 4 of the 6 groups improve by `--per-group-threshold`
- both conditions must hold, or BGE Base wins
- gates first: if either model's gates are not `PASS`, no verdict is issued

## Usage

```bash
python src/rag_eval_harness.py \
  --retrieval reports/week3/bakeoff_results.csv \
  --chunk-metadata reports/week3/rag_eligible_chunks.csv \
  --out reports/week3/rag_eval
```

Options: `--decision-metric {mrr_at_10,mrr_at_5}` (default `mrr_at_10`),
`--per-group-threshold` (default `0.03`), `--text-field` (default
`chunk_text`), `--questions`.

## Outputs

Written to `--out` (default `reports/rag_eval`):

- `rag_eval_report.json` — full result including per-question detail
- `rag_eval_report.md` — gates, overall and per-group tables, the verdict
- `rag_eval_per_question.csv` — one row per model/question

Exit code is `0` only when every model's gates `PASS`. Any `FAIL`,
`NOT_EVALUATED`, or a run that scored no models exits `1`, so a caller checking
only the exit code cannot mistake an uncertified run for success.

## Open questions for the project owner

Two points in the spec are ambiguous. The harness takes a documented position
rather than guessing silently; both should be confirmed.

1. **Retrieval depth.** The bake-off procedure returns "a ranked list of 5
   chunk IDs per question", but the metrics named are MRR@10 and nDCG@10 (and
   nDCG@5 in an earlier message). `@10` cannot be computed from 5 results.
   The harness reports both depths and marks `@10` truncated when depth < 10.
   Either the retrieval goes to 10, or the recorded metrics are `@5`.

2. **The 4-of-6 condition.** "That improvement holds across at least 4 of the
   6 question groups" — must each of those groups also improve by >= 0.03, or
   does any improvement count? The harness requires 0.03 per group, adjustable
   via `--per-group-threshold`.

Contract note: `required_doc_type` reads `10-K|10-K` on XC-001, XC-002, TC-003
and TC-004 but `10-K` on XC-003 and XC-004. Both behave identically here; worth
normalising in the sheet.

## Validation status

- 133 unit tests, including the boundaries of the selection rule (exactly 0.03,
  exactly 4 groups) and the real-text cases above.
- Validated against `SAMPLES_DB/diverse_500_chunks_20260714T235643Z.tar.gz`, a
  500-chunk export of `rag_eligible_10k_chunks` and the artifact the question
  set cites as its source. All 30 supporting chunks resolve; all 30
  `supporting_file_sha256` values match the real files; `gold_integrity` passes.
- **Not yet exercised on real retrieval output** — no model has run. Scoring is
  unit-tested only.
- The `wrong_doc_type` FAIL path is unit-tested only: the sample is entirely
  10-K, so no real ESG chunk has been observed leaking into it.

The archive is not tracked in git. Obtain it from Hanzala. Note that Git Bash
`tar` fails to read it; use Python's `tarfile`.
