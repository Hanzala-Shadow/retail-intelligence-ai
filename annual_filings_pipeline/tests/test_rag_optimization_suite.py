import pytest

from src.evidence_sufficiency import (
    Candidate,
    Requirement,
    SupportLabel,
    allocate_evidence,
    build_support_contract,
    dynamic_evidence_budget,
)


def req(key, ticker="AAA", year=2025, section="Item_1", side="side-a"):
    return Requirement(
        requirement_id=key, comparison_side_id=side, claim_key=key,
        ticker=ticker, filing_year=year, doc_type="10-K",
        accession_number=f"{ticker}-{year}", section_code=section,
    )


def cand(chunk, support, ticker="AAA", year=2025, section="Item_1", rank=1):
    return Candidate(
        chunk_id=chunk, ticker=ticker, filing_year=year, doc_type="10-K",
        accession_number=f"{ticker}-{year}", section_code=section,
        relevance_rank=rank, support=support,
    )


def test_dynamic_budget_covers_simple_comparison_and_complex_synthesis():
    assert dynamic_evidence_budget(1, 1) == 5
    assert dynamic_evidence_budget(4, 2) == 6
    assert dynamic_evidence_budget(6, 3) == 8


def test_one_passage_is_selected_once_and_credited_to_two_requirements():
    requirements = [req("products"), req("sales-channels")]
    shared = cand(1, {
        "products": SupportLabel.DIRECT,
        "sales-channels": SupportLabel.DIRECT,
    })
    selected = allocate_evidence(requirements, [shared], limit=5)
    contract = build_support_contract("cross_section_synthesis", requirements, selected)
    assert [item.chunk_id for item in selected] == [1]
    assert contract["overall_support_status"] == "satisfied"
    assert all(row["evidence"][0]["chunk_id"] == 1 for row in contract["requirements"])


def test_topical_mention_does_not_become_direct_support():
    requirements = [req("products"), req("customers")]
    candidate = cand(1, {
        "products": SupportLabel.DIRECT,
        "customers": SupportLabel.CONTEXT,
    })
    contract = build_support_contract(
        "single_source", requirements,
        allocate_evidence(requirements, [candidate], limit=5),
    )
    by_id = {row["requirement_id"]: row for row in contract["requirements"]}
    assert by_id["products"]["support_status"] == "satisfied"
    assert by_id["customers"]["support_status"] == "partial"
    assert contract["overall_support_status"] == "partial"
    assert contract["support_confidence"] is None


def test_wrong_company_year_and_section_credit_fail_closed():
    requirement = req("revenue", ticker="AAA", year=2025, section="Item_7")
    wrong = cand(
        9, {"revenue": SupportLabel.DIRECT},
        ticker="BBB", year=2024, section="Item_8",
    )
    with pytest.raises(ValueError, match="source or section mismatch"):
        allocate_evidence([requirement], [wrong])


def test_duplicate_chunk_from_two_routes_uses_one_slot():
    requirements = [req("products"), req("channels")]
    first = cand(7, {"products": SupportLabel.DIRECT}, rank=1)
    second = cand(7, {"channels": SupportLabel.DIRECT}, rank=2)
    selected = allocate_evidence(requirements, [first, second], limit=5)
    assert len(selected) == 1
    assert set(selected[0].support) == {"products", "channels"}


def test_allocator_preserves_both_comparison_sides():
    requirements = [
        req("revenue-a", ticker="AAA", side="side-a", section="Item_7"),
        req("revenue-b", ticker="BBB", side="side-b", section="Item_7"),
    ]
    candidates = [
        cand(1, {"revenue-a": SupportLabel.DIRECT}, ticker="AAA", section="Item_7"),
        cand(2, {"revenue-b": SupportLabel.DIRECT}, ticker="BBB", section="Item_7"),
    ]
    contract = build_support_contract(
        "cross_company_comparison",
        requirements,
        allocate_evidence(requirements, candidates, limit=6),
    )
    assert contract["overall_support_status"] == "satisfied"
    assert {row["comparison_side_id"] for row in contract["requirements"]} == {
        "side-a", "side-b",
    }


def test_unsupported_requirement_is_never_omitted_or_upgraded_by_rank():
    requirements = [req("products"), req("suppliers")]
    ranked_first = cand(1, {}, rank=1)
    direct = cand(2, {"products": SupportLabel.DIRECT}, rank=2)
    contract = build_support_contract(
        "single_source", requirements,
        allocate_evidence(requirements, [ranked_first, direct], limit=5),
    )
    assert len(contract["requirements"]) == 2
    by_id = {row["requirement_id"]: row for row in contract["requirements"]}
    assert by_id["suppliers"]["support_status"] == "unsupported"
    assert by_id["suppliers"]["evidence"] == []
