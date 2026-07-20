# Phase 2 AI/RAG Two-Week Execution Plan

## Purpose

This plan turns the Week 2 database-ready document corpus into a working AI/vector-search retrieval system with measurable RAG quality. The goal is not only to create a demo chatbot. The goal is to prove that retrieval returns trusted evidence, citations are valid, and generated answers are grounded in the right document chunks.

This plan is designed for the next two-week phase after the Week 2 data sprint closes.

## Source Materials Reviewed

- `Retail_Intelligence_2Week_Plan_different.pdf`
- `DB_RAG_Quality_Report.docx`
- `Retail_Intelligence_Complete_DB_ESG_Teammate_Handoff.docx`
- `Coverage Report.xlsx`
- `Sustainability Report Tracker.xlsx`
- `Retail_Intelligence_Taxonomy_Template.xlsx`
- `docs/AI_RAG_ARCHITECTURE.md`
- `docs/RAG_EVALUATION_PLAN.md`
- `reports/rag_readiness_summary.md`
- `data/00_reference/vector_index_manifest.csv`
- `data/00_reference/esg_chunks_index.csv`
- `data/00_reference/esg_pipeline_qa.csv`

## Current Standing

The DB/RAG quality report shows the 10-K database is structurally ready:

- 193 companies
- 568 annual filings
- 568 parsed 10-K documents
- 9,101 DB sections
- 86,034 DB chunks
- All loaded 10-K chunks are within token bounds
- Relationship integrity checks passed

The same report explicitly says full RAG quality is not proven yet. Still required:

- embedding generation
- one current embedding per eligible chunk
- benchmark questions with known supporting passages
- Recall@K, Hit Rate@K, MRR, and nDCG
- metadata-filtered retrieval tests
- answer generation and citation evaluation
- ESG QA repeated after ESG insertion

Aziz's ESG readiness layer adds the missing ESG control data:

- 32,262 ESG chunks reviewed
- 31,323 index-eligible ESG chunks
- 398 ESG chunks requiring manual review
- 541 ESG chunks excluded from ESG RAG
- 442 ESG documents/report rows marked `index_as_esg`
- 2 report rows excluded as likely wrong document type
- 2 OCR-required PDFs

## Leadership Position

Aziz should own:

```text
RAG Evaluation Lead / ESG Vector Search Owner
```

Reason:

The existing database work proves structural readiness. It does not prove semantic retrieval quality. Aziz's ESG pipeline already produces the safety metadata needed for RAG: `doc_type`, `doc_quality_status`, `rag_action`, `quality_flags`, `token_count`, `page_start`, `page_end`, and `citation_ready`. That makes Aziz the natural owner of safe-indexing rules, retrieval evaluation, citation validation, and demo answer quality.

## Team Roles

| Person | Phase 2 Role | Responsibilities | Primary Outputs |
| --- | --- | --- | --- |
| Aziz | RAG Evaluation Lead / ESG Vector Search Owner | Own RAG evaluation design, ESG vector index eligibility, citation rules, benchmark questions, demo answer validation, and failure analysis. | `vector_index_manifest.csv`, `rag_eval_questions.csv`, `rag_eval_results.csv`, `rag_eval_summary.md`, citation QA |
| Hanzala | Infrastructure / DB / Embedding Ops Lead | Own EC2, DB environment, final ESG DB load, migrations, vector DB setup, embedding job runtime, row counts, and deployment stability. | DB row counts, embedding table/index, runtime logs, deployment checklist |
| Ibraheem | Retrieval Backend Lead | Own retrieval query flow, metadata filters, vector-store connection, backend API/query scripts, SQL validation, and retrieval reliability. | `retrieve_chunks.py`, retrieval API/query interface, DB validation SQL, retrieval smoke tests |
| Aisha | Retrieval Implementation Engineer | Code support for vector index build, embedding manifests, retrieval smoke tests, and batch query outputs. | `build_vector_index.py`, retrieval smoke result CSVs, unit tests |
| Suleyman | Evaluation Automation Engineer | Code support for evaluation runner, ranking metrics, result aggregation, and reproducible reports. | `rag_evaluator.py`, metrics outputs, failure-analysis tables |
| Melek | Taxonomy / Business Review Lead | Non-coding review of taxonomy labels, business categories, answer usefulness, and whether evidence supports the business question. | taxonomy review notes, accepted/rejected label list, business answer review |
| Naz | Coverage / Source QA Lead | Non-coding review of source coverage, missing PDFs, wrong PDFs, OCR files, and demo citation page checks. | source QA sheet, PDF review notes, demo citation review |

## Role Boundaries

Hanzala and Ibraheem should keep infrastructure, DB, and backend ownership.

Aziz should not compete for EC2 or DB control. Aziz should own the quality layer that decides whether AI answers are trustworthy.

Aisha and Suleyman can code and should support implementation. Melek and Naz should not be assigned coding work; they should review taxonomy, source coverage, PDFs, and demo answers.

## Two-Week Timeline

If Week 2 closes on Day 14, this plan starts on Day 15.

### Week 1: Build Retrieval Foundation

#### Day 15 - Ownership, Inputs, and DB Readiness

Primary goal: freeze ownership and confirm the data source.

Tasks:

- Hanzala verifies final DB standing after ESG load.
- Hanzala confirms whether `V3__RAG_Metadata.sql` is applied.
- Aziz freezes ESG RAG eligibility rules from `vector_index_manifest.csv`.
- Ibraheem confirms vector-store approach: PostgreSQL/pgvector, external vector DB, or local FAISS prototype.
- Aisha and Suleyman review script responsibilities.
- Melek reviews whether the 34 taxonomy labels should be consolidated toward the original 15-20 label target.
- Naz reviews source coverage gaps, OCR files, and wrong-document flags.

Outputs:

- Final role assignment posted to team.
- DB row-count screenshot/report.
- Vector-store decision.
- Final ESG index filter.
- `manual_pdf_review.csv` started.

Done when:

- Everyone knows their owner/support role.
- DB source of truth is clear.
- RAG index eligibility rules are accepted.

#### Day 16 - Embedding and Vector Index Design

Primary goal: design the index before generating embeddings.

Tasks:

- Aziz defines index inclusion/exclusion rules for ESG and 10-K.
- Hanzala sets up vector storage and confirms credentials/runtime.
- Ibraheem defines retrieval result schema.
- Aisha starts `src/build_vector_index.py`.
- Suleyman starts metric definitions in `src/rag_evaluator.py`.
- Melek maps taxonomy labels to likely query categories.
- Naz checks ETSY 2021, ETSY 2024, TDUP 2023, and TDUP 2024 status.

Outputs:

- `src/build_vector_index.py` draft.
- `data/00_reference/vector_index_manifest.csv` accepted as ESG index control file.
- vector table/index schema or local index design.
- retrieval result schema.

Done when:

- The team knows exactly which chunks will be embedded first.
- No excluded ESG chunks can enter the ESG vector index.

#### Day 17 - First Index Build and Retrieval Smoke Test

Primary goal: create the first searchable index.

Tasks:

- Aisha runs the first index build on a limited subset, then full eligible ESG chunks if runtime allows.
- Hanzala monitors runtime, memory, disk, and embedding cost.
- Ibraheem implements `src/retrieve_chunks.py`.
- Aziz writes first 20 evaluation questions using known ESG companies and sections.
- Suleyman logs top-k retrieval outputs.
- Melek validates whether sample retrieved evidence is business-meaningful.
- Naz checks citations/page ranges for sample outputs.

Outputs:

- first vector index build log.
- `src/retrieve_chunks.py` draft.
- `data/00_reference/retrieval_smoke_test_results.csv`.
- first 20 RAG evaluation questions.

Done when:

- At least 10 known ESG questions return citation-ready chunks.
- Excluded ETSY annual-report-like chunks do not appear for ESG-only queries.

#### Day 18 - Evaluation Harness

Primary goal: move from demo queries to measurable retrieval quality.

Tasks:

- Suleyman implements `src/rag_evaluator.py`.
- Aziz expands `rag_eval_questions_seed.csv` into `rag_eval_questions.csv`.
- Ibraheem adds metadata filters by ticker, doc_type, year/report, and section.
- Aisha adds tests/smoke checks for index eligibility.
- Melek reviews taxonomy coverage of evaluation questions.
- Naz reviews source availability for demo companies.

Outputs:

- `src/rag_evaluator.py`.
- `data/00_reference/rag_eval_questions.csv`.
- first `data/00_reference/rag_eval_results.csv`.
- metadata-filtered retrieval tests.

Done when:

- Evaluation can run from one command.
- Results include query, expected ticker/year/section, retrieved chunk IDs, score, page range, and pass/fail fields.

#### Day 19 - Week 1 Quality Review

Primary goal: identify the biggest failure class before answer generation.

Tasks:

- Aziz reviews failures and labels causes: missing source, wrong doc type, weak chunking, weak retriever, citation issue, taxonomy mismatch.
- Ibraheem improves retrieval filters or ranking.
- Aisha reruns index/retrieval tests after fixes.
- Suleyman summarizes metrics.
- Melek reviews whether failures are business-label problems.
- Naz updates source/PDF issue list.
- Hanzala confirms infra stability and index counts.

Outputs:

- Week 1 `reports/rag_eval_summary.md` draft.
- failure category table.
- updated source QA notes.

Done when:

- The team knows what to fix first in Week 2.
- Retrieval quality is measured, not guessed.

### Week 2: Answer Quality, Demo, and Handoff

#### Day 20 - Answer Generation Prototype

Primary goal: generate answers only from retrieved evidence.

Tasks:

- Ibraheem builds `src/rag_answer.py` or API equivalent.
- Aziz defines answer rules: cite ticker, year, source document, section, page range.
- Suleyman starts `src/validate_rag_answer.py`.
- Aisha prepares repeatable demo query runs.
- Melek reviews answer usefulness.
- Naz checks citations against source pages.

Outputs:

- `src/rag_answer.py`.
- `src/validate_rag_answer.py` draft.
- `data/00_reference/rag_answer_eval.csv`.

Done when:

- At least 10 answers include retrieved evidence and citations.
- No answer cites excluded ESG chunks.

#### Day 21 - Quantitative Retrieval Tuning

Primary goal: improve ranking and filtering before final demo.

Tasks:

- Aziz reviews metrics against demo gates.
- Ibraheem tests vector-only vs metadata-filtered vs hybrid retrieval.
- Suleyman calculates Recall@K, Hit Rate@K, MRR, nDCG, wrong_doc_type_rate, and citation_ready_rate.
- Aisha reruns full evaluation after each retrieval change.
- Melek checks whether taxonomy labels help or confuse retrieval.
- Naz checks whether coverage gaps explain failures.

Outputs:

- retrieval comparison table.
- updated `rag_eval_results.csv`.
- ranked list of remaining failure causes.

Done when:

- ESG-only wrong_doc_type_rate is zero.
- citation_ready_rate is at least 0.95 for demo candidate queries.
- hit_at_5 target is reached or failure reasons are documented.

#### Day 22 - Demo Scenario Build

Primary goal: select demo queries that show value without hiding limitations.

Tasks:

- Aziz selects final demo query set.
- Melek confirms demo questions are meaningful for retail/business analysis.
- Naz confirms cited files are available and not wrong-document/OCR-only.
- Ibraheem makes query flow reliable enough for live or recorded demo.
- Aisha prepares deterministic demo outputs.
- Suleyman prepares evaluation summary visuals/tables.
- Hanzala confirms runtime/deployment state.

Outputs:

- `data/00_reference/demo_queries.csv`.
- `data/00_reference/demo_answer_review.csv`.
- `reports/demo_readiness_notes.md`.

Done when:

- Every demo answer has source-backed evidence.
- Every citation has document and page range.
- Limitations are written clearly.

#### Day 23 - Final QA and Handoff

Primary goal: make the system reviewable by Ayse and teammates.

Tasks:

- Aziz finalizes `reports/rag_eval_summary.md`.
- Suleyman finalizes metrics tables.
- Ibraheem finalizes retrieval/backend instructions.
- Hanzala finalizes deployment/runtime instructions.
- Aisha finalizes script usage notes and tests.
- Melek signs off on business/taxonomy interpretation.
- Naz signs off on source/citation review.

Outputs:

- final `reports/rag_eval_summary.md`.
- final `reports/demo_readiness_notes.md`.
- final script runbook.
- list of known limitations.

Done when:

- Another teammate can run the evaluation and reproduce the main results.
- The team can explain both what works and what is still limited.

#### Day 24 - Presentation and Decision Review

Primary goal: present a defensible RAG system, not only a chatbot demo.

Tasks:

- Aziz presents evaluation results, safe-indexing logic, citation quality, and failure analysis.
- Ibraheem presents retrieval/backend architecture.
- Hanzala presents DB/runtime readiness.
- Melek presents taxonomy/business interpretation.
- Naz presents coverage and source QA.
- Aisha/Suleyman present implementation/test support.

Outputs:

- final demo.
- final role/deliverable summary.
- next-phase improvement backlog.

Done when:

- Ayse can see the RAG system is tested, cited, and grounded.
- The team has a clear next backlog for production hardening.

## Required Deliverables

### Technical Deliverables

- `src/build_vector_index.py`
- `src/retrieve_chunks.py`
- `src/rag_evaluator.py`
- `src/rag_answer.py`
- `src/validate_rag_answer.py`
- vector table/index or local index artifact
- `data/00_reference/vector_index_manifest.csv`
- `data/00_reference/rag_eval_questions.csv`
- `data/00_reference/rag_eval_results.csv`
- `data/00_reference/retrieval_smoke_test_results.csv`
- `data/00_reference/rag_answer_eval.csv`
- `reports/rag_eval_summary.md`

### Non-Coding Deliverables

- `data/00_reference/manual_pdf_review.csv`
- `data/00_reference/demo_queries.csv`
- `data/00_reference/demo_answer_review.csv`
- `reports/demo_readiness_notes.md`
- taxonomy review notes
- source coverage review notes

### DB / Infrastructure Deliverables

- final 10-K and ESG DB row counts
- embedding row counts
- vector index count
- migration notes
- environment/runbook notes

## Minimum Demo Gates

The demo should not be considered ready unless:

- No ESG-only query retrieves chunks with `rag_action != index_as_esg`.
- No ESG-only query retrieves `doc_type = annual_report_with_esg`.
- No cited answer uses `citation_ready=false` chunks.
- Every answer includes source document and page range.
- Comparison answers cite every requested company.
- Retrieval results are measured using benchmark questions.
- Known OCR, wrong-document, missing-source, and citation issues are documented.

Target metrics:

- `hit_at_5 >= 0.80`
- `wrong_doc_type_rate = 0` for ESG-only questions
- `citation_ready_rate >= 0.95`
- `refusal_accuracy_rate >= 0.90` for excluded or unsupported questions

## Why This Plan Makes Aziz The Right Lead

The DB/RAG report says the current corpus is database-ready and chunk-QA-compliant, but retrieval-level RAG validation is pending. That pending layer is exactly Aziz's strongest area:

- Aziz built ESG parsing, sectioning, chunking, and QA metadata.
- Aziz created the RAG eligibility manifest.
- Aziz can decide which chunks are safe to index.
- Aziz can connect taxonomy, coverage, source quality, citations, and evaluation metrics.
- Aziz can prevent the team from showing a demo that retrieves weak or unsafe evidence.

This role does not take DB or backend ownership away from Hanzala or Ibraheem. It fills the missing quality/evaluation leadership layer that RAG needs before it can be trusted.

## Message To Send The Team

```text
I reviewed the current DB/RAG quality report, coverage tracker, sustainability tracker, taxonomy template, and the ESG chunk metadata.

My proposal for the next two-week AI/RAG phase is that we split work by ownership:

- Hanzala: infrastructure, DB, embeddings runtime, deployment
- Ibraheem: retrieval backend and metadata-filtered query flow
- Aziz: RAG Evaluation Lead / ESG Vector Search Owner
- Aisha: retrieval/indexing coding support
- Suleyman: evaluation metrics coding support
- Melek: non-coding taxonomy and business review
- Naz: non-coding source coverage, PDF, and citation QA

The reason I am proposing to own RAG evaluation / ESG vector search quality is that my ESG pipeline already produces the metadata needed to decide what is safe to index: doc_type, quality flags, rag_action, page ranges, token counts, and citation_ready.

The DB is structurally ready, but the RAG report itself says retrieval-level validation is still pending. I can own that layer: benchmark questions, safe index rules, retrieval metrics, citation validation, and demo answer quality.
```

