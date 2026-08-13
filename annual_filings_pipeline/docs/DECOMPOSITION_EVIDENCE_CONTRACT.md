# Decomposition and Evidence Contract 1.0.0

This package wraps the locked production retriever; it does not change its BGE
Base model, cross-encoder, hard source routing, candidate depth, final depth,
SQL, tables, or indexes.

## Request paths

- Explicit `filters`: preserve the existing single-source production path.
- No filters: detect entities, years, claims and sections; resolve each source
  uniquely from `rag_eligible_10k_chunks`; run one independently hard-routed
  retrieval per subquery; aggregate source-balanced evidence.

## Fail-closed rules

- Missing/ambiguous entity, year, source, claim/section or company-year pairing
  returns a structured error.
- The adapter requires real ranks, score, source fields and text from
  `ProductionRetriever`; it never fabricates them.
- A chunk whose metadata differs from its authorized `SourceSpec` fails with
  `ROUTING_INTEGRITY_FAILED`.
- A chunk ID appearing under conflicting sources or requirements also fails.
- Missing evidence for any required comparison side prevents comparison answer
  generation.
- Gold chunk IDs and supporting passages are not accepted by any interface.

## Citation fields

Every citation must contain ticker, filing year, document type, accession
number, section, chunk ID and aggregated rank. HTML filings use null page fields
with `page_unavailable_reason="html_source"`.

## Evaluation boundary

Frozen-24 results are in-sample. Restricted held-out questions and gold labels
must not be supplied to these modules during tuning.
