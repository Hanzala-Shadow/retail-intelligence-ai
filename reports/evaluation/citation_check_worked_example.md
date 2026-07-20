# Citation Check Worked Example

## Example

| Field | Value |
|------|------|
| question_id | TC-003 |
| answer_model | GPT-4.1 |
| claim_id | C1 |
| citation_index | 1 |
| chunk_id | chunk_14522 |
| ticker/company | AAPL |
| filing_year | 2024 |
| source_document | Apple 2024 Form 10-K |
| accession_number | 0000320193-24-000123 |
| section_code | Item 7 |
| page_start | 42 |
| page_end | 43 |
| page_unavailable_reason | |
| claim_supported | Yes |
| metadata_correct | Yes |
| verifier_notes | Worked example only. |

---

## Important

These two checks are independent.

### Case 1

Metadata Correct = Yes

Claim Supported = No

The citation points to the correct filing, company, year, and section,
but the cited passage does not support the answer.

### Case 2

Metadata Correct = No

Claim Supported = Yes

The cited passage supports the claim, but the metadata
(company, filing year, section, etc.) is incorrect.
