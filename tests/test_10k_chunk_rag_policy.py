from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from src.chunk_rag_policy import (
    chunk_rag_metadata,
    detected_sec_items,
    is_high_confidence_item1_toc,
    is_reserved_item6_boilerplate,
)


TOC_TEXT = """
Item 1. Business
Item 1A. Risk Factors
Item 2. Properties
Item 3. Legal Proceedings
Item 5. Market for Registrant's Common Equity
Item 6. Reserved
Item 7. Management's Discussion and Analysis
Item 7A. Quantitative and Qualitative Disclosures
Item 8. Financial Statements
"""


def test_detected_sec_items_are_distinct():
    text = TOC_TEXT + "\nItem 7. Cross reference"
    items = detected_sec_items(text)

    assert "1" not in items
    assert "1A" in items
    assert "7A" in items
    assert "8" in items
    assert len(items) == 8


def test_item1_chunk_zero_with_item_listing_is_toc():
    assert is_high_confidence_item1_toc(
        doc_type="10-K",
        section_code="Item_1",
        chunk_index=0,
        chunk_text=TOC_TEXT,
    )


def test_detector_is_limited_to_first_item1_chunk():
    assert not is_high_confidence_item1_toc(
        doc_type="10-K",
        section_code="Item_1",
        chunk_index=1,
        chunk_text=TOC_TEXT,
    )

    assert not is_high_confidence_item1_toc(
        doc_type="10-K",
        section_code="Item_7",
        chunk_index=0,
        chunk_text=TOC_TEXT,
    )


def test_toc_chunk_is_excluded_from_rag():
    metadata = chunk_rag_metadata(
        doc_type="10-K",
        section_code="Item_1",
        chunk_index=0,
        chunk_text=TOC_TEXT,
        token_count=500,
    )

    assert metadata == {
        "doc_quality_status": "passed",
        "rag_action": "exclude_boilerplate",
        "quality_flags": '["retrieval_boilerplate","sec_item_toc"]',
        "citation_ready": False,
    }


def test_substantive_chunk_remains_eligible():
    metadata = chunk_rag_metadata(
        doc_type="10-K",
        section_code="Item_7",
        chunk_index=0,
        chunk_text=(
            "Item 7. Management's Discussion and Analysis. "
            + "Operating performance and liquidity analysis. " * 40
        ),
        token_count=200,
    )

    assert metadata == {
        "doc_quality_status": "passed",
        "rag_action": "include",
        "quality_flags": "[]",
        "citation_ready": True,
    }


def test_headers_and_invalid_chunks_are_not_eligible():
    header = chunk_rag_metadata(
        doc_type="10-K",
        section_code="HEADER",
        chunk_index=0,
        chunk_text="Annual report cover information " * 20,
        token_count=100,
    )
    invalid = chunk_rag_metadata(
        doc_type="10-K",
        section_code="Item_7",
        chunk_index=0,
        chunk_text="Too small",
        token_count=2,
    )

    assert header["rag_action"] == "exclude_boilerplate"
    assert header["citation_ready"] is False
    assert invalid["rag_action"] == "review"
    assert invalid["citation_ready"] is False

def test_reserved_item6_page_layout_is_boilerplate():
    text = (
        "Item 6. [Reserved] [TABLE:table_54] "
        "TARGET CORPORATION 2023 Form 10-K 21 "
        "[TABLE:table_55] MANAGEMENT'S DISCUSSION "
        "AND ANALYSIS Table of Contents "
        "EXECUTIVE OVERVIEW & FINANCIAL SUMMARY "
        "Index to Financial Statements"
    )

    assert is_reserved_item6_boilerplate(
        doc_type="10-K",
        section_code="Item_6",
        chunk_text=text,
    )

    metadata = chunk_rag_metadata(
        doc_type="10-K",
        section_code="Item_6",
        chunk_index=0,
        chunk_text=text,
        token_count=62,
    )

    assert metadata == {
        "doc_quality_status": "passed",
        "rag_action": "exclude_boilerplate",
        "quality_flags": (
            '["retrieval_boilerplate",'
            '"reserved_item6_boilerplate"]'
        ),
        "citation_ready": False,
    }


def test_substantive_nonreserved_item6_remains_eligible():
    text = (
        "Item 6. Selected Financial Data. "
        + "Historical financial performance information. " * 40
    )

    metadata = chunk_rag_metadata(
        doc_type="10-K",
        section_code="Item_6",
        chunk_index=0,
        chunk_text=text,
        token_count=200,
    )

    assert metadata["rag_action"] == "include"
    assert metadata["citation_ready"] is True

