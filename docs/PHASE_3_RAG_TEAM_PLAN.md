# Phase 3 RAG Team Plan

## Purpose

Phase 3 turns the Week 2 document corpus into a trusted retrieval and demo system. The goal is not only to build a chatbot. The goal is to prove that vector search retrieves the right chunks, excludes unsafe chunks, and produces answers with reliable citations.

This plan uses the current repo state as the starting point:

- ESG pipeline outputs are available in `data/00_reference/esg_chunks_index.csv`.
- ESG QA gates are available in `data/00_reference/esg_pipeline_qa.csv`.
- Initial RAG questions are available in `data/00_reference/rag_eval_questions_seed.csv`.
- RAG rules are documented in `docs/AI_RAG_ARCHITECTURE.md` and `docs/RAG_EVALUATION_PLAN.md`.
- Aisha and Suleyman can code.
- Melek and Naz should own non-coding review, research, coverage, and demo validation work.

## Source Materials Reviewed

- `2. Retail_Intelligence_2Week_Plan (1).docx`: confirms that AI/vector-search retrieval begins after Week 2, Aziz owns the ESG pipeline, Hanzala/Ibraheem own DB and EC2 work, Aisha/Suleyman own coding-heavy QA tasks, Melek owns taxonomy, and Naz owns coverage/source reporting.
- `ARCHITECTURE.md`: defines the project flow from source documents to QA reports, PostgreSQL, vector indexes, and RAG evaluation.
- `docs/ESG_PIPELINE.md`: defines the ESG parsing, sectioning, chunking, OCR handling, QA, and DB load process.
- `docs/AI_RAG_ARCHITECTURE.md`: defines the safe ESG index filter and retrieval behavior.
- `docs/RAG_EVALUATION_PLAN.md`: defines evaluation metrics, demo gates, and Aziz's RAG evaluation ownership.
- `docs/DATABASE.md`: defines the metadata fields needed for citation-aware retrieval.

## Recommended Role Structure

| Person | Phase 3 Role | Main Responsibility | Coding? |
| --- | --- | --- | --- |
| Aziz | RAG Evaluation Lead / ESG Vector Search Owner | Own retrieval quality, ESG index rules, evaluation gates, citation checks, and demo answer validation. | Yes |
| Hanzala | Infrastructure and DB Lead | Own EC2, PostgreSQL, environment variables, DB load verification, and deployment stability. | Yes |
| Ibraheem | Backend Retrieval Integration Lead | Own database/vector-store integration, retrieval API/query flow, SQL validation, and backend reliability. | Yes |
| Aisha | Retrieval Coding Support | Implement and test retrieval scripts, batch query runs, and result logging under Aziz/Ibraheem direction. | Yes |
| Suleyman | Evaluation Coding Support | Implement and test evaluation metrics, failure reports, and reproducible evaluation runs under Aziz direction. | Yes |
| Melek | Business Taxonomy and Answer Reviewer | Review taxonomy, business labels, answer usefulness, and whether retrieved evidence makes business sense. | No |
| Naz | Coverage, Source, and Demo QA Reviewer | Review source coverage, missing/wrong PDFs, OCR cases, and demo answer citations. | No |

## Aziz Ownership Scope

Aziz should own the decisions that determine whether the AI layer is trustworthy:

- Which ESG chunks are eligible for vector indexing.
- Which documents are excluded from ESG-only RAG.
- Which chunks require manual review before indexing.
- Which evaluation questions prove the retriever works.
- Which metrics must pass before the team demos the system.
- Whether an answer is grounded, cited, and safe to show.

This is stronger than prompt ownership because it controls the quality gate between raw documents and AI answers.

## Technical Workstreams

### 1. ESG Vector Index

Owner: Aziz  
Coding support: Aisha  
DB support: Hanzala / Ibraheem

Deliverables:

- `src/build_vector_index.py`
- `data/05_db/migrations/V4__Vector_Index.sql` if PostgreSQL/pgvector is used
- `data/00_reference/vector_index_manifest.csv`

Index only ESG chunks where:

```text
doc_type = sustainability
doc_quality_status = ok
rag_action = index_as_esg
citation_ready = true
token_count between 100 and 600
```

Exclude:

```text
doc_type = annual_report_with_esg
doc_quality_status = exclude_from_esg_rag
rag_action = exclude_from_esg_index
quality_flags contains possible_10k
```

Done when:

- Every indexed chunk has a stable `chunk_id`.
- No excluded chunk is inserted into the ESG vector index.
- The manifest reports total eligible, indexed, excluded, and manual-review chunks.
- Re-running the index build is idempotent.

### 2. Retrieval Layer

Owner: Ibraheem  
Quality owner: Aziz  
Coding support: Aisha

Deliverables:

- `src/retrieve_chunks.py`
- `src/rag_query.py` or equivalent API/query entry point
- `data/00_reference/retrieval_smoke_test_results.csv`

Required behavior:

- Accept query text and optional metadata filters such as ticker, year, document type, and section.
- Return top-k chunks with chunk IDs, scores, ticker, document name, section, page range, and QA status.
- Refuse or flag excluded material for ESG-only questions.
- Keep citations tied to `page_start` and `page_end`.

Done when:

- A query for a known ESG topic returns citation-ready chunks.
- An ESG-only query does not return `annual_report_with_esg` chunks.
- Retrieval results include enough metadata for answer generation and evaluation.

### 3. RAG Evaluation Harness

Owner: Aziz  
Coding support: Suleyman

Deliverables:

- `src/rag_evaluator.py`
- `data/00_reference/rag_eval_questions.csv`
- `data/00_reference/rag_eval_results.csv`
- `reports/rag_eval_summary.md`

Use `data/00_reference/rag_eval_questions_seed.csv` as the seed and expand it to at least 50 questions before final demo.

Metrics:

- `hit_at_5`
- `section_hit_at_5`
- `wrong_doc_type_rate`
- `citation_ready_rate`
- `grounded_answer_rate`
- `citation_accuracy_rate`
- `refusal_accuracy_rate`
- `comparison_completeness_rate`

Minimum demo gate:

- `hit_at_5 >= 0.80`
- `wrong_doc_type_rate = 0` for ESG-only questions
- `citation_ready_rate >= 0.95`
- No answer cites chunks where `rag_action != index_as_esg`.

Done when:

- Every evaluation question has retrieved chunk IDs, citation metadata, and pass/fail labels.
- Failures are categorized as data issue, retriever issue, citation issue, prompt issue, or missing source issue.
- The team can show a summary table explaining what passed and what failed.

### 4. Answer Generation and Citation Validation

Owner: Aziz  
Backend support: Ibraheem  
Coding support: Suleyman

Deliverables:

- `src/rag_answer.py`
- `src/validate_rag_answer.py`
- `data/00_reference/rag_answer_eval.csv`

Required answer rules:

- Answer only from retrieved evidence.
- Include ticker, year, source document, section, and page range.
- Do not cite chunks where `citation_ready=false`.
- Do not cite excluded ESG-folder annual reports in ESG-only answers.
- For comparison questions, cite every requested company.

Done when:

- Demo answers include traceable citations.
- Unsupported claims are flagged.
- The answer validator catches missing citations, wrong tickers, wrong document type, and excluded chunks.

### 5. Non-Coding Review and Demo QA

Owner: Melek and Naz  
Coordinator: Aziz

Deliverables:

- `data/00_reference/manual_pdf_review.csv`
- `data/00_reference/demo_question_review.csv`
- `reports/demo_readiness_notes.md`

Melek responsibilities:

- Review taxonomy labels and business categories.
- Confirm whether retrieved evidence answers the business question.
- Mark confusing or weak answers for revision.
- Help select demo questions that show business value.

Naz responsibilities:

- Check source coverage for demo companies.
- Track missing, wrong, or OCR-required PDFs.
- Verify citation page ranges for selected demo answers.
- Confirm flagged files such as ETSY 2021, ETSY 2024, TDUP 2023, and TDUP 2024.

Done when:

- Every demo query has a human-reviewed answer.
- Every cited PDF has been checked for source quality.
- Known missing/OCR/wrong-document cases are documented, not hidden.

## Proposed Phase 3 Timeline

### Day 1: Lock Inputs and Ownership

- Hanzala verifies DB row counts and EC2 readiness.
- Aziz freezes ESG indexing rules.
- Ibraheem confirms vector-store approach.
- Aisha and Suleyman review the coding tasks.
- Melek and Naz review non-coding QA responsibilities.

Outputs:

- Final role assignment posted to the team.
- DB and source readiness confirmed.
- RAG evaluation seed file reviewed.

### Day 2: Build First ESG Vector Index

- Aziz and Aisha build the ESG-only vector index.
- Hanzala/Ibraheem verify DB/vector-store connection.
- Naz checks flagged PDF issues.

Outputs:

- Vector index manifest.
- First successful retrieval smoke test.

### Day 3: Build Retrieval and Evaluation

- Ibraheem builds retrieval query flow.
- Suleyman builds the evaluation runner.
- Aziz defines pass/fail rules and reviews first failures.
- Melek reviews whether returned evidence is business-meaningful.

Outputs:

- Retrieval results CSV.
- First evaluation results CSV.
- Failure categories.

### Day 4: Answer Generation and Demo Validation

- Aziz validates answer grounding and citation quality.
- Ibraheem connects retrieval to answer generation.
- Suleyman adds answer validation checks.
- Melek and Naz review demo answers.

Outputs:

- Demo-ready answer set.
- Citation validation report.
- Known limitations list.

### Day 5: Final Demo Package

- Aziz owns final RAG evaluation summary.
- Hanzala owns deployment/runtime stability.
- Ibraheem owns backend query reliability.
- Melek and Naz own source/demo QA sign-off.

Outputs:

- `reports/rag_eval_summary.md`
- `reports/demo_readiness_notes.md`
- Final demo query list.
- Final list of limitations and next improvements.

## Deliverable Checklist

Required technical artifacts:

- `src/build_vector_index.py`
- `src/retrieve_chunks.py`
- `src/rag_evaluator.py`
- `src/rag_answer.py`
- `src/validate_rag_answer.py`
- `data/00_reference/vector_index_manifest.csv`
- `data/00_reference/rag_eval_questions.csv`
- `data/00_reference/rag_eval_results.csv`
- `data/00_reference/rag_answer_eval.csv`
- `reports/rag_eval_summary.md`

Required non-coding artifacts:

- `data/00_reference/manual_pdf_review.csv`
- `data/00_reference/demo_question_review.csv`
- `reports/demo_readiness_notes.md`

Required team proof:

- DB row counts verified.
- ESG vector index excludes flagged documents.
- Retrieval evaluation passes minimum demo gate.
- Demo answers cite source document and page range.
- Known OCR/wrong-document issues are documented.

## Message Aziz Can Send To Claim The Role

```text
For Phase 3, I would like to take ownership of RAG Evaluation and ESG Vector Search.

My proposed scope is to define which ESG chunks are safe to index, build the retrieval evaluation set, validate citation/page accuracy, and prepare the demo query quality checks. Since my ESG pipeline already produces chunk metadata, QA status, RAG actions, and citation readiness, this is the natural next step from my current work.

Aisha and Suleyman can support with coding the retrieval and evaluation scripts. Melek and Naz can support with non-coding review: taxonomy/business validation, PDF quality checks, source coverage, and demo answer review.
```

## Main Risk

The main risk is building a demo that appears to answer questions but retrieves weak or unsafe evidence. Phase 3 should therefore be led through evaluation gates first, not prompt design first.
