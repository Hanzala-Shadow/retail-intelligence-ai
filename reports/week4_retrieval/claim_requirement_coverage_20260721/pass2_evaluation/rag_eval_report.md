# 10-K RAG Retrieval Evaluation

Generated: 2026-07-20T22:54:40.812796+00:00

- Scored questions: 24
- Refusal questions (not retrieval-scored): 5

## Hard gates

| Model | Overall | wrong_doc_type | gold_integrity | evidence_present |
| --- | --- | --- | --- | --- |
| claim_specific_requirement_aware | FAIL | PASS | PASS | FAIL |
| claim_specific_rewrite | FAIL | PASS | PASS | FAIL |
| original_depth20_control | FAIL | PASS | PASS | FAIL |
| source_claim_requirement_aware | FAIL | PASS | PASS | FAIL |
| source_claim_specific_rewrite | FAIL | PASS | PASS | FAIL |
| source_only_original_control | FAIL | PASS | PASS | FAIL |
| source_specific_rewrite | FAIL | PASS | PASS | FAIL |

## Overall scores

| Model | Recall@5 | Hit@5 | MRR@5 | nDCG@5 |
| --- | --- | --- | --- | --- |
| claim_specific_requirement_aware | 0.9583 | 1.0000 | 0.7917 | 0.8092 |
| claim_specific_rewrite | 0.9375 | 1.0000 | 0.7917 | 0.7897 |
| original_depth20_control | 0.6667 | 0.7083 | 0.5208 | 0.5303 |
| source_claim_requirement_aware | 0.8750 | 0.9167 | 0.6250 | 0.6734 |
| source_claim_specific_rewrite | 0.8750 | 0.9167 | 0.6215 | 0.6707 |
| source_only_original_control | 0.6667 | 0.7083 | 0.5208 | 0.5303 |
| source_specific_rewrite | 0.6667 | 0.6667 | 0.4583 | 0.5055 |

### claim_specific_requirement_aware by question group

| Group | Recall@5 | Hit@5 | MRR@5 | nDCG@5 |
| --- | --- | --- | --- | --- |
| Item_1 | 1.0000 | 1.0000 | 0.6875 | 0.7654 |
| Item_1A | 1.0000 | 1.0000 | 0.8750 | 0.9077 |
| Item_7 | 1.0000 | 1.0000 | 0.8333 | 0.8750 |
| Item_8 | 1.0000 | 1.0000 | 0.8333 | 0.8750 |
| cross_company | 0.8750 | 1.0000 | 1.0000 | 0.8525 |
| time_change | 0.8750 | 1.0000 | 0.5208 | 0.5794 |

### claim_specific_rewrite by question group

| Group | Recall@5 | Hit@5 | MRR@5 | nDCG@5 |
| --- | --- | --- | --- | --- |
| Item_1 | 1.0000 | 1.0000 | 0.6875 | 0.7654 |
| Item_1A | 1.0000 | 1.0000 | 0.8750 | 0.9077 |
| Item_7 | 1.0000 | 1.0000 | 0.8333 | 0.8750 |
| Item_8 | 1.0000 | 1.0000 | 0.8333 | 0.8750 |
| cross_company | 0.8750 | 1.0000 | 0.8750 | 0.7759 |
| time_change | 0.7500 | 1.0000 | 0.6458 | 0.5392 |

### original_depth20_control by question group

| Group | Recall@5 | Hit@5 | MRR@5 | nDCG@5 |
| --- | --- | --- | --- | --- |
| Item_1 | 1.0000 | 1.0000 | 0.6875 | 0.7654 |
| Item_1A | 1.0000 | 1.0000 | 0.8750 | 0.9077 |
| Item_7 | 0.5000 | 0.5000 | 0.3333 | 0.3750 |
| Item_8 | 0.5000 | 0.5000 | 0.3333 | 0.3750 |
| cross_company | 0.5000 | 0.7500 | 0.7500 | 0.5259 |
| time_change | 0.5000 | 0.5000 | 0.1458 | 0.2327 |

### source_claim_requirement_aware by question group

| Group | Recall@5 | Hit@5 | MRR@5 | nDCG@5 |
| --- | --- | --- | --- | --- |
| Item_1 | 1.0000 | 1.0000 | 0.6875 | 0.7654 |
| Item_1A | 1.0000 | 1.0000 | 0.8750 | 0.9077 |
| Item_7 | 0.7500 | 0.7500 | 0.5833 | 0.6250 |
| Item_8 | 1.0000 | 1.0000 | 0.7083 | 0.7827 |
| cross_company | 0.7500 | 1.0000 | 0.6875 | 0.6013 |
| time_change | 0.7500 | 0.7500 | 0.2083 | 0.3580 |

### source_claim_specific_rewrite by question group

| Group | Recall@5 | Hit@5 | MRR@5 | nDCG@5 |
| --- | --- | --- | --- | --- |
| Item_1 | 1.0000 | 1.0000 | 0.6875 | 0.7654 |
| Item_1A | 1.0000 | 1.0000 | 0.8750 | 0.9077 |
| Item_7 | 0.7500 | 0.7500 | 0.4583 | 0.5327 |
| Item_8 | 1.0000 | 1.0000 | 0.8333 | 0.8750 |
| cross_company | 0.7500 | 1.0000 | 0.6458 | 0.5746 |
| time_change | 0.7500 | 0.7500 | 0.2292 | 0.3686 |

### source_only_original_control by question group

| Group | Recall@5 | Hit@5 | MRR@5 | nDCG@5 |
| --- | --- | --- | --- | --- |
| Item_1 | 1.0000 | 1.0000 | 0.6875 | 0.7654 |
| Item_1A | 1.0000 | 1.0000 | 0.8750 | 0.9077 |
| Item_7 | 0.5000 | 0.5000 | 0.3333 | 0.3750 |
| Item_8 | 0.5000 | 0.5000 | 0.3333 | 0.3750 |
| cross_company | 0.5000 | 0.7500 | 0.7500 | 0.5259 |
| time_change | 0.5000 | 0.5000 | 0.1458 | 0.2327 |

### source_specific_rewrite by question group

| Group | Recall@5 | Hit@5 | MRR@5 | nDCG@5 |
| --- | --- | --- | --- | --- |
| Item_1 | 1.0000 | 1.0000 | 0.6875 | 0.7654 |
| Item_1A | 1.0000 | 1.0000 | 0.8750 | 0.9077 |
| Item_7 | 0.5000 | 0.5000 | 0.3333 | 0.3750 |
| Item_8 | 0.7500 | 0.7500 | 0.4583 | 0.5327 |
| cross_company | 0.2500 | 0.2500 | 0.2500 | 0.2193 |
| time_change | 0.5000 | 0.5000 | 0.1458 | 0.2327 |

## Model selection decision

Status: NOT_EVALUATED - decision rule needs both bge_base_en_v1_5 and bge_large_en_v1_5; got ['claim_specific_requirement_aware', 'claim_specific_rewrite', 'original_depth20_control', 'source_claim_requirement_aware', 'source_claim_specific_rewrite', 'source_only_original_control', 'source_specific_rewrite']

