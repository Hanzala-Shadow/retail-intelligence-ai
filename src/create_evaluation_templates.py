from pathlib import Path
import pandas as pd

output_dir = Path("reports") / "evaluation"
output_dir.mkdir(parents=True, exist_ok=True)



citation_columns = [
    "question_id",
    "answer_model",
    "claim_id",
    "citation_index",
    "chunk_id",
    "ticker/company",
    "filing_year",
    "source_document",
    "accession_number",
    "section_code",
    "page_start",
    "page_end",
    "page_unavailable_reason",
    "claim_supported",
    "metadata_correct",
    "verifier_notes"
]


citation_example = [{
    "question_id": "TC-003",
    "answer_model": "GPT-4.1",
    "claim_id": "C1",
    "citation_index": 1,
    "chunk_id": "chunk_14522",
    "ticker/company": "AAPL",
    "filing_year": 2024,
    "source_document": "Apple 2024 Form 10-K",
    "accession_number": "0000320193-24-000123",
    "section_code": "Item 7",
    "page_start": 42,
    "page_end": 43,
    "page_unavailable_reason": "",
    "claim_supported": "Yes",
    "metadata_correct": "Yes",
    "verifier_notes": "Worked example only. Replace with actual evaluation data."
}]

citation_df = pd.DataFrame(citation_example, columns=citation_columns)

citation_df.to_csv(
    output_dir / "citation_check_template.csv",
    index=False
)



hci_columns = [
    "run_id",
    "question_id",
    "answer_model",
    "response_time_seconds",
    "answer_quality",
    "citation_quality",
    "claim_supported",
    "metadata_correct",
    "failure_type",
    "reviewer_notes"
]

pd.DataFrame(columns=hci_columns).to_csv(
    output_dir / "hci_log.csv",
    index=False
)


worked_example = """# Citation Check Worked Example

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
"""

with open(output_dir / "citation_check_worked_example.md", "w", encoding="utf-8") as f:
    f.write(worked_example)

print("Evaluation templates created successfully.")
print(f"Location: {output_dir.resolve()}")