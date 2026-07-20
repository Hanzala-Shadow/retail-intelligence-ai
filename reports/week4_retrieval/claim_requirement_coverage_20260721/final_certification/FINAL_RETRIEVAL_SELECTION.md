# Final Retrieval Selection — Claim Requirement Coverage

**Status:** VERIFIED IN-SAMPLE WINNER; HELD-OUT EVALUATION REQUIRED

**Selected method:** `claim_specific_requirement_aware`

## Metrics

| Method | Hit@5 | MRR@5 | Recall@5 | nDCG@5 |
|---|---:|---:|---:|---:|
| Control | 0.708333 | 0.520833 | 0.666667 | 0.530280 |
| Winner | 1.000000 | 0.791667 | 0.958333 | 0.809173 |

Recovered questions: 10K-V2-I7-002, 10K-V2-I7-004, 10K-V2-I8-002, 10K-V2-I8-003, 10K-V2-TC-003, 10K-V2-TC-004, 10K-V2-XC-002. Lost questions: none.

Incomplete multi-source coverage remains for `10K-V2-XC-003` and `10K-V2-TC-004`; answer generation must fail closed when a required side is absent.

All wrong-document and gold-integrity gates pass. The common evidence-present failure is the known `10K-V2-XC-004` elided-quote contract exception.

Do not tune further on frozen-24. The authorized custodian must run the restricted held-out evaluation before production promotion.
