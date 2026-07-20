from src.detector_coverage_audit import PROHIBITED_GOLD_FIELDS, audit_question
from src.query_decomposition import FilingRecord, SourceResolver

TICKERS = {"EBAY", "ORBS", "TBHC", "VFC"}
ALIASES = {"ebay": "EBAY", "eightco": "ORBS", "timberland": "TBHC", "vf corporation": "VFC"}
RESOLVER = SourceResolver([
    FilingRecord("EBAY", 2024, "10-K", "ebay-24"),
    FilingRecord("EBAY", 2026, "10-K", "ebay-26"),
    FilingRecord("ORBS", 2024, "10-K", "orbs-24"),
    FilingRecord("ORBS", 2025, "10-K", "orbs-25"),
    FilingRecord("ORBS", 2026, "10-K", "orbs-26"),
    FilingRecord("TBHC", 2024, "10-K", "tbhc-24"),
    FilingRecord("VFC", 2024, "10-K", "vfc-24"),
])


def row(question, tickers, years, sections, accessions):
    return {
        "question_id": "tc", "question_group": "time_change", "question": question,
        "expected_tickers": tickers, "expected_years": years,
        "required_doc_type": "10-K", "required_sections": sections,
        "supporting_accession_numbers": accessions, "refusal_expected": "FALSE",
    }


def test_exact_temporal_route():
    result = audit_question(
        row("What changed in eBay risk between its 2024 and 2026 filings?",
            "EBAY|EBAY", "2024|2026", "Item_1A|Item_1A", "ebay-24|ebay-26"),
        TICKERS, ALIASES, RESOLVER,
    )
    assert result["scoring"]["routing_exact"] is True


def test_content_year_mismatch_is_explicit():
    result = audit_question(
        row("What changed in Eightco revenue from 2024 to 2025?",
            "ORBS", "2026", "Item_7", "orbs-26"),
        TICKERS, ALIASES, RESOLVER,
    )
    assert result["scoring"]["routing_exact"] is False
    assert result["scoring"]["year_interpretation"] == "content_year_filing_year_mismatch"


def test_alias_collision_and_gold_exclusion_are_explicit():
    result = audit_question(
        row("How did VF Corporation's 2024 revenue vary by Timberland brand?",
            "VFC", "2024", "Item_7", "vfc-24"),
        TICKERS, ALIASES, RESOLVER,
    )
    assert result["detected"]["entities"] == ["TBHC", "VFC"]
    assert result["scoring"]["routing_exact"] is False
    assert {"expected_answer", "supporting_chunk_ids", "supporting_passages"}.issubset(
        PROHIBITED_GOLD_FIELDS
    )
