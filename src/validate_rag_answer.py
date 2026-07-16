import csv
from pathlib import Path

REQUIRED_FIELDS = ["company_name", "filing_year", "source_document", "section_label"]

def validate_answer(record):
    """Check one answer record for complete, citable metadata.

    Returns (citation_ready: bool, missing_fields: list[str])
    """
    missing = []

    for field in REQUIRED_FIELDS:
        value = record.get(field)
        if value is None or str(value).strip() == "":
            missing.append(field)

    # page_range is required UNLESS the source is an .htm file (10-Ks have no
    # real page numbers once parsed from HTML) - but it must be explicitly
    # flagged as such, not just silently missing.
    source_doc = str(record.get("source_document", "")).lower()
    page_range = record.get("page_range")
    is_htm_source = source_doc.endswith(".htm") or source_doc.endswith(".html")

    page_missing = page_range is None or str(page_range).strip() == ""

    if page_missing:
        if is_htm_source:
            # Acceptable - but only if explicitly flagged
            if not record.get("page_range_na_flag", False):
                missing.append("page_range_na_flag (missing page_range on .htm source must be explicitly flagged)")
        else:
            missing.append("page_range")

    citation_ready = len(missing) == 0
    return citation_ready, missing


def build_test_records():
    """10 synthetic answer records covering the realistic failure cases."""
    return [
        # 1. Fully complete, 10-K htm source, page_range correctly N/A-flagged
        {
            "answer_id": 1,
            "company_name": "Amazon.com Inc",
            "filing_year": 2024,
            "source_document": "AMZN__10-K__0001018724-24-000008.htm",
            "section_label": "Item_1A",
            "page_range": None,
            "page_range_na_flag": True,
        },
        # 2. Fully complete, ESG PDF source with real page range
        {
            "answer_id": 2,
            "company_name": "Apple Inc",
            "filing_year": 2024,
            "source_document": "AAPL-APPLE INC-2024.pdf",
            "section_label": "Environmental",
            "page_range": "23-24",
        },
        # 3. Missing company_name
        {
            "answer_id": 3,
            "company_name": "",
            "filing_year": 2023,
            "source_document": "GAP__10-K__0001158449-24-000048.htm",
            "section_label": "Item_7",
            "page_range": None,
            "page_range_na_flag": True,
        },
        # 4. Missing filing_year
        {
            "answer_id": 4,
            "company_name": "Target Corp",
            "filing_year": None,
            "source_document": "TGT__10-K__example.htm",
            "section_label": "Item_1",
            "page_range": None,
            "page_range_na_flag": True,
        },
        # 5. PDF source missing page_range (should fail - not htm, no excuse)
        {
            "answer_id": 5,
            "company_name": "Amazon.com Inc",
            "filing_year": 2023,
            "source_document": "AMZN-Amazon-2023.pdf",
            "section_label": "Social",
            "page_range": None,
        },
        # 6. htm source missing page_range but NOT flagged (should fail - undeclared)
        {
            "answer_id": 6,
            "company_name": "Kohl's Corp",
            "filing_year": 2024,
            "source_document": "KSS__10-K__example.htm",
            "section_label": "Item_8",
            "page_range": None,
            "page_range_na_flag": False,
        },
        # 7. Missing source_document entirely
        {
            "answer_id": 7,
            "company_name": "Nike Inc",
            "filing_year": 2024,
            "source_document": "",
            "section_label": "Item_1A",
            "page_range": None,
            "page_range_na_flag": True,
        },
        # 8. Missing section_label
        {
            "answer_id": 8,
            "company_name": "Advance Auto Parts",
            "filing_year": 2024,
            "source_document": "AAP__10-K__0001158449-24-000048.htm",
            "section_label": None,
            "page_range": None,
            "page_range_na_flag": True,
        },
        # 9. Fully complete, another clean htm example
        {
            "answer_id": 9,
            "company_name": "eBay Inc",
            "filing_year": 2024,
            "source_document": "EBAY__10-K__0001065088-24-000036.htm",
            "section_label": "Item_7A",
            "page_range": None,
            "page_range_na_flag": True,
        },
        # 10. Multiple fields missing at once
        {
            "answer_id": 10,
            "company_name": "TJX Companies",
            "filing_year": None,
            "source_document": "",
            "section_label": "",
            "page_range": None,
        },
    ]


def main():
    records = build_test_records()
    results = []

    for record in records:
        citation_ready, missing = validate_answer(record)
        results.append({
            "answer_id": record["answer_id"],
            "citation_ready": citation_ready,
            "missing_fields": "; ".join(missing) if missing else "",
        })

    # Print report
    print(f"{'answer_id':<12}{'citation_ready':<18}missing_fields")
    print("-" * 80)
    for r in results:
        print(f"{r['answer_id']:<12}{str(r['citation_ready']):<18}{r['missing_fields']}")

    ready_count = sum(1 for r in results if r["citation_ready"])
    print("-" * 80)
    print(f"\n{ready_count}/{len(results)} answers are citation_ready")

    # Save report
    output_path = Path("reports/citation_validation_report.csv")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["answer_id", "citation_ready", "missing_fields"])
        writer.writeheader()
        writer.writerows(results)

    print(f"Report saved to {output_path}")


if __name__ == "__main__":
    main()