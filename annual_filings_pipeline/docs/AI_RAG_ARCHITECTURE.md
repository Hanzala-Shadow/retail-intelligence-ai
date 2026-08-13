# AI and RAG Architecture

## Pipeline

```text
PDFs
-> parsed text + page maps
-> ESG/10-K sections
-> token-bounded chunks with metadata
-> QA gates
-> vector index
-> retriever
-> answer generator with citations
-> evaluation harness
```

## Core Principle

The vector index must not blindly ingest every chunk. It should ingest only chunks that pass document-quality and citation-readiness gates.

## ESG Index Filter

Use only rows from `data/00_reference/esg_chunks_index.csv` where:

```text
doc_type = sustainability
doc_quality_status = ok
rag_action = index_as_esg
citation_ready = true
```

Exclude:

```text
doc_quality_status = exclude_from_esg_rag
rag_action = exclude_from_esg_index
doc_type = annual_report_with_esg
```

Review manually:

```text
doc_quality_status = needs_review
rag_action = manual_review_before_indexing
citation_ready = false
```

## Required Chunk Metadata

Every indexed chunk should have:

- `chunk_id`
- `ticker`
- `doc_type`
- `pdf_stem`
- `section_code`
- `chunk_index`
- `token_count`
- `chunk_file`
- `source_section_file`
- `source_start_char`
- `source_end_char`
- `page_start`
- `page_end`
- `citation_ready`
- `doc_quality_status`
- `rag_action`

## Recommended Retrieval Behavior

For ESG-only questions:

1. Filter to `doc_type=sustainability`.
2. Filter to `rag_action=index_as_esg`.
3. Filter to `citation_ready=true`.
4. Retrieve top candidates.
5. Rerank by semantic similarity plus metadata match for ticker/year/section.
6. Generate an answer only from retrieved evidence.
7. Cite ticker, year, PDF, section, and page range.

For questions that hit excluded material:

- Do not answer from the ESG index.
- Explain that the matching document is flagged as `annual_report_with_esg` or `exclude_from_esg_rag`.
- Route to a separate annual-report/10-K index if the project builds one.

## Evaluation

Use `docs/RAG_EVALUATION_PLAN.md` and `data/00_reference/rag_eval_questions_seed.csv`.

The minimum demo gate is:

- no ESG-only answer cites excluded chunks
- no answer cites chunks with `citation_ready=false`
- comparisons cite every requested company
- all answers include source document and page range
