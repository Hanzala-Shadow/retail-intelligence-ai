from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.query_decomposition import (
    ContractError, FilingRecord, ProductionRetrieverAdapter, RetrievedChunk,
    SourceResolver, aggregate, build_subqueries, detect_claims, detect_entities,
    detect_filing_years, validate_citations,
)

TICKERS = {"COST", "NKE", "LULU"}
ALIASES = {"costco": "COST", "nike": "NKE", "lululemon": "LULU"}
RESOLVER = SourceResolver([
    FilingRecord("COST", 2023, "10-K", "cost-23"),
    FilingRecord("COST", 2024, "10-K", "cost-24"),
    FilingRecord("NKE", 2024, "10-K", "nke-24"),
    FilingRecord("LULU", 2024, "10-K", "lulu-24"),
])


class FakeAdapter:
    def __init__(self, empty=(), conflicting=False):
        self.empty = set(empty)
        self.conflicting = conflicting

    def retrieve(self, sq):
        if sq.comparison_side_id in self.empty:
            return []
        base = {"COST": 1000, "NKE": 2000, "LULU": 3000}[sq.ticker] + sq.filing_year
        return [RetrievedChunk(
            chunk_id=999 if self.conflicting else base * 10 + rank,
            semantic_rank=rank, cross_encoder_rank=rank,
            cross_encoder_score=1.0 - rank / 10, ticker=sq.ticker,
            filing_year=sq.filing_year, doc_type=sq.doc_type,
            accession_number=sq.accession_number, section_code=sq.section_code,
            chunk_text=f"chunk {rank}",
        ) for rank in range(1, 6)]


def test_temporal_subqueries_are_source_specific():
    kind, subs = build_subqueries("r", "How did Costco gross margin change from 2023 to 2024?", TICKERS, ALIASES, RESOLVER)
    assert kind.value == "temporal_comparison"
    assert {sq.filing_year for sq in subs} == {2023, 2024}
    assert all(str(sq.filing_year) in sq.question for sq in subs)
    assert all("Item_7" in sq.question for sq in subs)
    assert len({sq.comparison_side_id for sq in subs}) == 2


def test_cross_company_is_balanced_and_deterministic():
    _, subs = build_subqueries("r", "Compare Nike and Lululemon supply chain risk in 2024", TICKERS, ALIASES, RESOLVER)
    first = aggregate(subs, FakeAdapter())
    second = aggregate(subs, FakeAdapter())
    assert first == second
    sides = [x["comparison_side_id"] for x in first["evidence"]]
    assert all(sides.count(side) >= 2 for side in first["required_sides"])


def test_claims_pair_with_their_sections_not_cartesian_product():
    _, subs = build_subqueries("r", "For Costco 2024 compare revenue and gross margin", TICKERS, ALIASES, RESOLVER)
    assert {(sq.claim_key, sq.section_code) for sq in subs} == {("revenue", "Item_8"), ("gross margin", "Item_7")}
    assert len(subs) == 2


def test_ambiguous_multi_axis_fails_closed():
    with pytest.raises(ContractError, match="explicit company-year pairing"):
        build_subqueries("r", "Compare Nike and Lululemon revenue in 2023 and 2024", TICKERS, ALIASES, RESOLVER)


def test_explicit_long_multi_axis_pairing_is_resolved():
    resolver = SourceResolver([
        FilingRecord("NKE", 2023, "10-K", "nke-23"),
        FilingRecord("LULU", 2024, "10-K", "lulu-24"),
    ])
    _, subs = build_subqueries(
        "ignored", "How do Nike's 2023 10-K disclosures about liquidity and cash flow "
        "compare with those of Lululemon in its 2024 10-K?", TICKERS, ALIASES, resolver,
    )
    assert {(sq.ticker, sq.filing_year, sq.section_code) for sq in subs} == {
        ("NKE", 2023, "Item_7"), ("LULU", 2024, "Item_7")}


def test_punctuation_normalization_resolves_company_and_claim():
    assert detect_entities("O'REILLY AUTOMOTIVE INC", {"ORLY"}, {"o reilly automotive inc": "ORLY"}) == ("ORLY",)
    assert detect_claims("supply-chain disruption risk") == ("supply chain disruption risk",)


def test_explicit_10k_year_excludes_fiscal_content_year():
    assert detect_filing_years(
        "According to the 2025 10-K, why did expenses change in fiscal 2024?"
    ) == (2025,)
    assert detect_filing_years(
        "How did the strategy change between the 2024 and 2025 10-K filings?"
    ) == (2024, 2025)
    assert detect_filing_years(
        "How did gross margin change from fiscal 2023 to fiscal 2024?"
    ) == (2023, 2024)


def test_generic_novel_intent_families_map_to_expected_sections():
    assert detect_claims("What caused SG&A expenses to deleverage?") == (
        "operating expenses",
    )
    assert detect_claims("What progress was made opening new service clinics?") == (
        "business expansion and strategic progress",
    )
    assert detect_claims(
        "Report lease payments, commercial paper capacity, and short-term borrowings"
    ) == ("lease accounting", "debt and borrowings")
    assert detect_claims("Compare supplier concentration and continuity risks") == (
        "risk factors",
    )


def test_generic_same_section_multi_requirement_produces_two_routes():
    resolver = SourceResolver([FilingRecord("COST", 2024, "10-K", "cost-24")])
    kind, subs = build_subqueries(
        "r",
        "In Costco's 2024 10-K, report lease payments and commercial paper borrowings",
        TICKERS,
        ALIASES,
        resolver,
    )
    assert kind.value == "cross_section_synthesis"
    assert {(sq.claim_key, sq.section_code) for sq in subs} == {
        ("lease accounting", "Item_8"),
        ("debt and borrowings", "Item_8"),
    }


def test_new_intents_route_to_expected_sections_and_multi_requirement():
    assert detect_claims("digital commerce and regulatory and legal risk") == (
        "regulatory and legal risk", "digital commerce")
    _, subs = build_subqueries(
        "r", "What does Costco's 2024 10-K disclose about both digital commerce "
        "and regulatory and legal risk?", TICKERS, ALIASES, RESOLVER,
    )
    assert {(sq.claim_key, sq.section_code) for sq in subs} == {
        ("digital commerce", "Item_1"), ("regulatory and legal risk", "Item_1A")}


def test_subquery_ids_are_content_stable_not_request_id_dependent():
    question = "How did Costco gross margin change from 2023 to 2024?"
    first = build_subqueries("request-a", question, TICKERS, ALIASES, RESOLVER)[1]
    second = build_subqueries("request-b", question, TICKERS, ALIASES, RESOLVER)[1]
    assert [sq.subquery_id for sq in first] == [sq.subquery_id for sq in second]


def test_missing_side_returns_structured_insufficiency():
    _, subs = build_subqueries("r", "How did Costco gross margin change from 2023 to 2024?", TICKERS, ALIASES, RESOLVER)
    response = aggregate(subs, FakeAdapter(empty={subs[0].comparison_side_id}))
    assert response["status"] == "insufficient_evidence"
    assert response["error_code"] == "MISSING_COMPARISON_SIDE"


def test_conflicting_chunk_source_fails_integrity():
    _, subs = build_subqueries("r", "How did Costco gross margin change from 2023 to 2024?", TICKERS, ALIASES, RESOLVER)
    with pytest.raises(ContractError) as caught:
        aggregate(subs, FakeAdapter(conflicting=True))
    assert caught.value.code == "ROUTING_INTEGRITY_FAILED"


@dataclass(frozen=True)
class FakeSourceSpec:
    ticker: str
    filing_year: int
    accession_number: str
    section_code: str
    doc_type: str = "10-K"


class RawRetriever:
    def __init__(self, missing=False, mismatch=False):
        self.missing = missing
        self.mismatch = mismatch

    def retrieve(self, question, sources):
        source = sources[0]
        item = {
            "chunk_id": 1, "semantic_rank": 1,
            "cross_encoder_rank_within_source": 1,
            "cross_encoder_score": 0.7,
            "ticker": "NKE" if self.mismatch else source.ticker,
            "filing_year": source.filing_year, "doc_type": source.doc_type,
            "accession_number": source.accession_number,
            "section_code": source.section_code, "chunk_text": "text",
        }
        if self.missing:
            item.pop("cross_encoder_score")
        return {"evidence": [item]}


class AdaptiveRawRetriever(RawRetriever):
    def __init__(self):
        super().__init__()
        self.original_question = None

    def retrieve_requirement(self, subquery, *, original_question):
        self.original_question = original_question
        return self.retrieve(subquery.question, [FakeSourceSpec(
            subquery.ticker, subquery.filing_year, subquery.accession_number,
            subquery.section_code, subquery.doc_type,
        )])


def _one_subquery():
    return build_subqueries("r", "What was Costco revenue in 2024?", TICKERS, ALIASES, RESOLVER)[1][0]


def test_adapter_uses_exact_production_signature():
    result = ProductionRetrieverAdapter(RawRetriever(), FakeSourceSpec).retrieve(_one_subquery())
    assert result[0].cross_encoder_score == 0.7


def test_adapter_does_not_fabricate_required_fields():
    with pytest.raises(ContractError) as caught:
        ProductionRetrieverAdapter(RawRetriever(missing=True), FakeSourceSpec).retrieve(_one_subquery())
    assert caught.value.code == "ADAPTER_CONTRACT_FAILED"


def test_adapter_rejects_routing_mismatch():
    with pytest.raises(ContractError) as caught:
        ProductionRetrieverAdapter(RawRetriever(mismatch=True), FakeSourceSpec).retrieve(_one_subquery())
    assert caught.value.code == "ROUTING_INTEGRITY_FAILED"


def test_adapter_uses_adaptive_requirement_path_with_original_question():
    retriever = AdaptiveRawRetriever()
    subquery = _one_subquery()
    result = ProductionRetrieverAdapter(
        retriever, FakeSourceSpec, original_question="the original question"
    ).retrieve(subquery)
    assert result[0].chunk_id == 1
    assert retriever.original_question == "the original question"


def test_citations_require_all_fields_and_sides():
    _, subs = build_subqueries("r", "How did Costco gross margin change from 2023 to 2024?", TICKERS, ALIASES, RESOLVER)
    response = aggregate(subs, FakeAdapter())
    citations = [{field: item[field] for field in ("ticker", "filing_year", "doc_type", "accession_number", "section_code", "chunk_id", "aggregated_rank")} | {"page_unavailable_reason": "html_source"} for item in response["evidence"]]
    assert validate_citations(citations, response["evidence"], response["required_sides"])[0]
    assert not validate_citations([{"chunk_id": citations[0]["chunk_id"]}], response["evidence"], response["required_sides"])[0]
