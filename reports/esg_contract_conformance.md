# ESG contract conformance

Checks derived from `esg_chunk_handoff_100_20260728/`: the six integrity
counters in `sampling_manifest.json`, the `Required` rows of
`field_dictionary.csv`, and the header shape observed on all 100 reference
chunks. The reference sample is run first as calibration — it is known-good,
so a failure there would mean the checker is wrong.

## Calibration

- Recomputed integrity counters match `sampling_manifest.json`: **True**
- Reference checks failing: **0** (expected 0)

## Integrity and structure

### Reference sample (contract's own 100) — 100 chunks

| check | failures |
|---|---:|
| `missing_source_chunk_id` | 0 |
| `duplicate_source_chunk_id` | 0 |
| `duplicate_chunk_id` | 0 |
| `empty_chunk_text` | 0 |
| `chunk_text_sha256_failures` | 0 |
| `embedding_text_sha256_failures` | 0 |
| `embedding_text_missing_blank_line_separator` | 0 |
| `header_missing_mandatory_keys_in_order` — Company | Ticker | Document | Fiscal year | SEC section | Subsection | Content type | 0 |
| `embedding_text_minus_header_ne_chunk_text` | 0 |
| `quality_flags_blank_not_empty_collection` | 0 |
| `token_count_within_pipeline_bounds` — n/a — pipeline-local, not a contract rule | n/a |
| `citation_ready_not_boolean` | 0 |
| `rag_action_empty` | 0 |

### ESG corpus — 16341 chunks

| check | failures |
|---|---:|
| `missing_source_chunk_id` | 0 |
| `duplicate_source_chunk_id` | 0 |
| `duplicate_chunk_id` | 0 |
| `empty_chunk_text` | 0 |
| `chunk_text_sha256_failures` | 0 |
| `embedding_text_sha256_failures` | 0 |
| `embedding_text_missing_blank_line_separator` | 0 |
| `header_missing_mandatory_keys_in_order` — Company | Ticker | Document | Reporting year | ESG topic | Subsection | Content type | 0 |
| `embedding_text_minus_header_ne_chunk_text` | 0 |
| `quality_flags_blank_not_empty_collection` | **921** |
| `token_count_outside_[100..600]` — short_evidence floor 25 | 0 |
| `citation_ready_not_boolean` | 0 |
| `rag_action_empty` | 0 |

## Required-field coverage

Driven by the `esg_requirement` column of `field_dictionary.csv`.

| contract field | requirement | ESG field | status |
|---|---|---|---|
| `chunk_id` | Required locally | `chunk_id` | ok |
| `source_chunk_id` | Required | `chunk_id` | ok |
| `doc_id` | Required locally | `source_id` | ok |
| `company_id` | Required locally | `-` | **MISSING** |
| `ticker` | Required when available | `ticker` | ok |
| `coverage_year` | Required equivalent | `report_year` | ok |
| `doc_type` | Required | `doc_type` | ok |
| `section_code` | Required equivalent | `section_code` | ok |
| `chunk_index` | Required | `chunk_index` | ok |
| `chunk_text` | Required | `chunk_file` | ok |
| `embedding_text` | Required | `embedding_text_ctx_file` | ok |
| `token_count` | Required | `token_count` | ok |
| `doc_quality_status` | Required | `doc_quality_status` | ok |
| `rag_action` | Required | `rag_action` | ok |
| `citation_ready` | Required | `citation_ready` | ok |
| `quality_flags` | Required | `quality_flags` | ok |
| `chunk_text_sha256` | Required | `chunk_text_sha256` | ok |
| `embedding_text_sha256` | Required | `embedding_text_ctx_sha256` | ok |
| `dataset_id` | Required | `-` | **MISSING** |

Required fields present: **17/19**

