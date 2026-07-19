# 10-K RAG Retrieval Evaluation

Generated: 2026-07-19T11:53:50.751740+00:00

- Scored questions: 24
- Refusal questions (not retrieval-scored): 5

## Hard gates

| Model | Overall | wrong_doc_type | gold_integrity | evidence_present |
| --- | --- | --- | --- | --- |
| bge_base_en_v1_5 | FAIL | PASS | PASS | FAIL |

## Overall scores

| Model | Recall@5 | Hit@5 | MRR@5 | nDCG@5 |
| --- | --- | --- | --- | --- |
| bge_base_en_v1_5 | 0.6667 | 0.7083 | 0.5208 | 0.5303 |

### bge_base_en_v1_5 by question group

| Group | Recall@5 | Hit@5 | MRR@5 | nDCG@5 |
| --- | --- | --- | --- | --- |
| Item_1 | 1.0000 | 1.0000 | 0.6875 | 0.7654 |
| Item_1A | 1.0000 | 1.0000 | 0.8750 | 0.9077 |
| Item_7 | 0.5000 | 0.5000 | 0.3333 | 0.3750 |
| Item_8 | 0.5000 | 0.5000 | 0.3333 | 0.3750 |
| cross_company | 0.5000 | 0.7500 | 0.7500 | 0.5259 |
| time_change | 0.5000 | 0.5000 | 0.1458 | 0.2327 |

## Model selection decision

Status: NOT_EVALUATED - decision rule needs both bge_base_en_v1_5 and bge_large_en_v1_5; got ['bge_base_en_v1_5']

