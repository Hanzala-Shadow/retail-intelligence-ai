# Uncertainty-Aware AI Query Planner

## Purpose

The planner adds one structured AI planning call before 10-K retrieval. It
handles natural wording, spelling errors, vague requests, and comparisons. It
does not replace source routing, vector retrieval, reranking, or citation
checks.

## Safe flow

1. The backend reads the approved company and filing catalog from
   `rag_eligible_10k_chunks`.
2. The AI receives the user question and a safe copy of that catalog. It never
   receives database credentials.
3. The AI returns strict JSON with a query type, normalized intent, and one
   requirement for every company-year-claim-section need.
4. If the request is unclear, the API returns `clarification_required` and no
   retrieval is run.
5. The backend validates every ticker, year, document type, section, and search
   phrase. It resolves and locks the exact SEC accession number.
6. The existing retriever uses the original, focused, claim-profile, and AI
   search views. Every view keeps the same hard source filters.
7. Existing aggregation rejects missing comparison sides. Existing citation
   validation still requires exact evidence metadata.

The AI never writes SQL, chooses an accession number, changes a locked source,
or claims that evidence exists.

## Configuration

The existing deterministic planner stays the default. Enable the AI planner
with:

```text
QUERY_PLANNER_ENABLED=true
OPENAI_API_KEY=...
QUERY_PLANNER_MODEL=gpt-5-mini
QUERY_PLANNER_TIMEOUT_SECONDS=30
```

`OPENAI_BASE_URL` may be set for a compatible endpoint. Unit tests inject a
fake planner and never make a paid call.

## Current boundary

This version plans only approved 10-K retrieval. ESG needs its own eligible
source catalog and resolver before it can use the same interface. The current
gate proves source and requirement coverage; a claim-support score must not be
added until its threshold is calibrated on allowed development data.

## Evaluation

Compare the AI planner with the deterministic baseline on allowed development
questions. Report parsing accuracy, unnecessary clarification, source and
query drift, Recall@5, requirement coverage, latency, and cost. Do not tune on
the sealed final benchmark.
