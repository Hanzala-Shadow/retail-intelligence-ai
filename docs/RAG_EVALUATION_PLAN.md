# RAG Evaluation Plan

Owner role: Document Intelligence and RAG Evaluation Lead

## Purpose

The RAG phase should not start from a chatbot demo. It should start from a testable retrieval system that only indexes trusted chunks, returns source-backed answers, and refuses unsafe document types.

This plan defines the gates Aziz should own before the team uses ESG and 10-K chunks for answers.

## Required Inputs

- `data/00_reference/esg_pipeline_qa.csv`
- `data/00_reference/esg_chunks_index.csv`
- `data/00_reference/rag_eval_questions_seed.csv`
- 10-K chunk index when the 10-K side is connected to RAG

## RAG Indexing Rules

Only index ESG chunks where:

- `doc_type = sustainability`
- `doc_quality_status = ok`
- `rag_action = index_as_esg`
- `citation_ready = true`
- `token_count` is between 100 and 600

Do not index ESG-only chunks where:

- `rag_action = exclude_from_esg_index`
- `doc_quality_status = exclude_from_esg_rag`
- `quality_flags` contains `possible_10k`

Manual review is required before indexing chunks where:

- `rag_action = manual_review_before_indexing`
- `doc_quality_status = needs_review`
- `quality_flags` contains `garbled_text`, `low_readable_word_ratio`, or `low_text_per_page`
- `citation_ready = false`

## Evaluation Set

Use `data/00_reference/rag_eval_questions_seed.csv` as the first evaluation set. Expand it to at least 50 questions before final demo.

Each question should specify:

- expected ticker(s)
- expected year(s)
- required document type
- required section/topic
- whether a citation is required
- quality gate expected from the retriever

## Metrics

Retrieval quality:

- `hit_at_5`: expected source document appears in top 5 retrieved chunks.
- `section_hit_at_5`: expected section/topic appears in top 5 retrieved chunks.
- `wrong_doc_type_rate`: retrieved chunks from excluded doc types.
- `citation_ready_rate`: retrieved chunks with page and source span metadata.

Answer quality:

- `grounded_answer_rate`: answer is supported by retrieved evidence.
- `citation_accuracy_rate`: cited ticker/year/page matches the supporting chunk.
- `refusal_accuracy_rate`: model refuses or flags questions that require excluded documents.
- `comparison_completeness_rate`: comparison answers cover every requested ticker/year.

Minimum demo gate:

- `hit_at_5 >= 0.80`
- `wrong_doc_type_rate = 0` for ESG-only questions
- `citation_ready_rate >= 0.95`
- no answer may cite a chunk with `rag_action != index_as_esg`

## Evaluation Workflow

1. Build the ESG vector index from only eligible chunks.
2. Run every row in `rag_eval_questions_seed.csv`.
3. Save retrieved chunk IDs, answer text, citation list, and pass/fail labels.
4. Review all failures and label the cause:
   - missing source document
   - wrong document type
   - poor chunking
   - weak retriever
   - weak answer synthesis
   - citation failure
5. Fix the highest-frequency failure class before changing prompts.

## Leadership Checklist

Aziz should own these decisions:

- Which documents are eligible for ESG-only RAG.
- Which chunks are excluded or require manual review.
- Which questions prove the system works.
- Which retrieval metrics must pass before the team demos.
- Which failures are data problems versus model/prompt problems.

This role is stronger than prompt ownership because it controls whether the AI output is trustworthy.
