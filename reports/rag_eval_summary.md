# Retrieval Configuration Selection

Owner: Aziz — Document Intelligence and RAG Evaluation Lead

## Status

**Part 1 — selection criteria: written and final.**
**Part 2 — applied decision: pending evaluation results.**

Part 1 was written before any retrieval results existed. At the time of
writing, the repository contains no `rag_eval_results.csv`, no
`retrieval_smoke_test_results.csv`, and no retrieval configuration script. The
commit that adds this document precedes any commit carrying evaluation output,
and that ordering is checkable in the git history.

That sequence is the point. Criteria chosen after the numbers are visible are
not criteria, they are a preference with a justification attached. Everything
in Part 1 is fixed before the comparison table arrives, and Part 2 only applies
it.

## Scope

This document selects the **retrieval configuration**. It does not select the
embedding model.

| Decision | Owner of the rule | Where it lives |
| --- | --- | --- |
| Which retrieval configuration ships | criteria below | this document |
| Which embedding model ships (BGE Base vs BGE Large) | Ayse's 2026-07-16 rule | `docs/RAG_EVAL_HARNESS.md` |

The two are independent and must not be traded off against each other.

## Configurations under evaluation

1. `vector_only` — dense retrieval over the pgvector index
2. `metadata_filtered` — hard pre-filters on ticker, year, document_type and
   section_label, then dense retrieval
3. `bm25_lexical` — PostgreSQL `tsvector` full-text search
4. `hybrid` — vector + BM25

Configurations 1 and 2 are P1. Configurations 3 and 4 are P3 and may not exist
by the evaluation run. **Their absence does not delay the decision**; the
criteria are applied to whichever configurations are present, and any missing
configuration is recorded as not evaluated rather than as a loss.

## Evidence

- Question set: `data/00_reference/rag_eval_questions.csv` — the 24 approved
  benchmark questions. The 5 refusal questions are excluded here; they test
  answer-generation behaviour, not retrieval.
- Retrieval depth: top 5 per question.
- Scoring: `src/rag_eval_harness.py`, which reads each configuration's ranked
  results and reports the metrics below overall and per question group.

## Metrics recorded per configuration

`wrong_doc_type_rate`, `Recall@5`, `Hit Rate@5`, `MRR`, `nDCG@5` — each overall
and broken down by the six question groups.

## Selection procedure

Applied in this order. No step may be revisited after seeing a later step.

**Step 1 — Elimination.** Any configuration with `wrong_doc_type_rate > 0` is
eliminated, regardless of every other score. A configuration that returns a
chunk from the wrong document type has produced an answer the corpus does not
support. No retrieval quality compensates for that, and a configuration
eliminated here cannot be reinstated on the strength of its MRR.

**Step 2 — Rank.** Surviving configurations are ranked by **MRR**, overall
across all 24 questions. Highest wins.

**Step 3 — Ties.** If two configurations are within 0.005 MRR of each other,
they are treated as tied and resolved in this order:

1. higher `nDCG@5`
2. higher `Recall@5`
3. higher `Hit Rate@5`
4. the simpler configuration — preferring, in order: `vector_only`,
   `metadata_filtered`, `bm25_lexical`, `hybrid`

The last rule exists so that a tie never resolves on preference. Fewer moving
parts is the tiebreak, because a hybrid that only matches a simpler
configuration is added operational cost for nothing.

**Step 4 — No survivor.** If every configuration is eliminated at Step 1, no
configuration is selected. The result is reported as a failure of the
retrieval layer, not as a choice between bad options, and the cause is
diagnosed before any configuration is re-run.

**Step 5 — Unmeasurable gate.** If `wrong_doc_type_rate` cannot be computed —
for example the chunk metadata export is unavailable — the gate is recorded as
not evaluated and **no selection is made**. An unverified gate is not a passed
gate.

## Not criteria

The following carry no weight in this decision, and are recorded here so they
cannot be introduced later:

- retrieval latency or throughput
- implementation effort already spent on a configuration
- MTEB scores or any published leaderboard
- how sophisticated a configuration is

Latency matters for production sizing and is measured separately. It is not a
tiebreak here.

## Known caveats, recorded in advance

These are properties of the question set, known before the run. They are
written down now so that a configuration is not credited or penalised for them
after the fact.

- **I7-004 (Williams-Sonoma)** rests on a 65-token supporting chunk, the
  shortest in the set. A miss on this question may reflect chunk size rather
  than retrieval quality. Noted at Ayse's request.
- **XC-004** draws its two supporting chunks from different sections
  (`Item_7` and `Item_8`). Both are valid; a retrieval hit on either must not
  be flagged as a section mismatch.
- **XC-004's Grow Generation passage** is an elided quote whose ellipsis
  removes the year its expected answer depends on. Correction is pending
  approval. Until it is corrected, the harness flags this question's passage
  validation for every configuration equally, so it does not advantage or
  disadvantage any one of them.
- **Cross-company questions** have two relevant chunks each, so retrieving one
  of the two scores 0.5 recall, not 1.0. This is intended.

## Part 2 — Applied decision

*To be completed when the comparison table is available. Nothing above changes
at that point.*

| Configuration | wrong_doc_type_rate | Recall@5 | Hit Rate@5 | MRR | nDCG@5 | Eliminated? |
| --- | --- | --- | --- | --- | --- | --- |
| `vector_only` | | | | | | |
| `metadata_filtered` | | | | | | |
| `bm25_lexical` | | | | | | |
| `hybrid` | | | | | | |

**Selected configuration:** *pending*

**Rationale:** *pending — to cite the table above and the step at which each
eliminated configuration was removed.*

**Per-group breakdown:** *pending.*

## Dependencies

This decision is blocked until the following exist, none of which are owned by
this document:

- a built vector index over the 89,760 eligible chunks (Hanzala)
- at least one operational retrieval configuration (Ibraheem)
- a ranked-results CSV per configuration, in the format specified in
  `docs/RAG_EVAL_HARNESS.md`
- a chunk metadata export, without which the Step 1 gate cannot be evaluated
