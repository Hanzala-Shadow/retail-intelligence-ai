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
    assert detect_filing_years(
        "How did the 2024 and 2025 10-Ks explain results for fiscal 2023?"
    ) == (2024, 2025)


def test_fy_prefixed_years_are_detected():
    assert detect_filing_years(
        "What net sales did Amazon report in its FY2024 10-K?"
    ) == (2024,)
    assert detect_filing_years(
        "Compare Amazon FY2023 and FY2024 net sales"
    ) == (2023, 2024)
    assert detect_filing_years(
        "How did results change from FY 2023 to FY 2024?"
    ) == (2023, 2024)


def test_possessive_normalization_resolves_corpus_aliases():
    assert detect_entities(
        "According to Retailer's 2025 10-K",
        {"RTL"},
        {"retailer s": "RTL"},
    ) == ("RTL",)


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


def test_generic_section_ontology_fallback_is_question_id_independent():
    assert detect_claims(
        "Describe the product portfolio, customer channels, and store footprint"
    ) == ("generic business disclosure",)
    assert detect_claims(
        "Explain the decrease in sales and the factors driving gross profit"
    ) == ("generic performance analysis",)
    assert detect_claims(
        "What share-based compensation expense remained unrecognized?"
    ) == ("generic financial-note disclosure",)
    assert detect_claims(
        "What could adversely affect the company if customer demand weakens?"
    ) == ("generic risk disclosure",)


def test_broad_same_source_business_question_is_decomposed_atomically():
    kind, subs = build_subqueries(
        "arbitrary-request",
        "In Costco's 2024 10-K Item 1, describe its products, customers, "
        "suppliers, sales channels, and store footprint.",
        TICKERS,
        ALIASES,
        RESOLVER,
    )
    assert kind.value == "cross_section_synthesis"
    assert [sq.claim_key for sq in subs] == [
        "products and services",
        "customers and demand",
        "suppliers and sourcing",
        "sales channels",
        "operating footprint",
    ]
    assert {sq.section_code for sq in subs} == {"Item_1"}
    assert all(sq.claim_key in sq.comparison_side_id.replace("-", " ") for sq in subs)
    assert len({sq.subquery_id for sq in subs}) == 5


def test_broad_financial_note_question_splits_amount_method_and_terms():
    _, subs = build_subqueries(
        "not-a-benchmark-id",
        "For Costco's 2024 10-K Item 8, what amounts were reported, how were "
        "they measured, and what were the agreement terms and maturities?",
        TICKERS,
        ALIASES,
        RESOLVER,
    )
    assert [sq.claim_key for sq in subs] == [
        "reported amounts",
        "measurement and estimates",
        "contractual terms",
    ]
    assert all(sq.section_code == "Item_8" for sq in subs)


def test_atomic_refinement_does_not_cross_the_resolved_section():
    _, subs = build_subqueries(
        "random",
        "In Costco's 2024 10-K Item 1A, explain the risk exposure, potential "
        "consequences, and mitigating controls.",
        TICKERS,
        ALIASES,
        RESOLVER,
    )
    assert [sq.claim_key for sq in subs] == [
        "risk exposure",
        "risk consequences",
        "risk mitigation",
    ]
    assert {sq.section_code for sq in subs} == {"Item_1A"}


def test_single_generic_concept_gets_a_focused_prompt_without_overdecomposition():
    _, subs = build_subqueries(
        "random",
        "What does Costco's 2024 10-K Item 1 say about its workforce?",
        TICKERS,
        ALIASES,
        RESOLVER,
    )
    assert len(subs) == 1
    assert subs[0].claim_key == "workforce"
    assert "employees" in subs[0].question


def test_five_atomic_requirements_each_receive_an_evidence_slot():
    _, subs = build_subqueries(
        "random",
        "In Costco's 2024 10-K Item 1, describe its products, customers, "
        "suppliers, sales channels, and store footprint.",
        TICKERS,
        ALIASES,
        RESOLVER,
    )
    response = aggregate(subs, FakeAdapter(), evidence_limit=5)
    assert response["status"] == "success"
    assert response["requirement_count"] == 5
    assert response["all_requirements_represented"] is True
    assert set(response["requirement_coverage"]) == {
        sq.comparison_side_id for sq in subs
    }
    assert all(count >= 1 for count in response["requirement_coverage"].values())


def test_sufficiency_feature_is_conservative_until_direct_labels_exist():
    _, subs = build_subqueries(
        "irrelevant-request-id",
        "Compare Costco's 2023 and 2024 10-K strategy.",
        TICKERS,
        ALIASES,
        RESOLVER,
    )
    response = aggregate(subs, FakeAdapter(), sufficiency_enabled=True)
    assert response["support"]["overall_support_status"] == "partial"
    assert response["support"]["support_confidence"] is None
    assert all(
        item["support_status"] == "partial"
        for item in response["support"]["requirements"]
    )


def test_sufficiency_feature_accepts_only_explicit_direct_support_labels():
    _, subs = build_subqueries(
        "irrelevant-request-id",
        "Compare Costco's 2023 and 2024 10-K strategy.",
        TICKERS,
        ALIASES,
        RESOLVER,
    )
    adapter = FakeAdapter()
    labels = {}
    for subquery in subs:
        chunk = adapter.retrieve(subquery)[0]
        labels[(chunk.chunk_id, subquery.comparison_side_id)] = "direct"
    response = aggregate(
        subs,
        adapter,
        sufficiency_enabled=True,
        direct_support_labels=labels,
    )
    assert response["support"]["overall_support_status"] == "satisfied"


def test_conditional_adverse_language_routes_to_risk_not_brand_business():
    _, subs = build_subqueries(
        "generic",
        "According to Costco's 2024 10-K, what could happen if the company "
        "cannot protect its brand or retain customer loyalty?",
        TICKERS,
        ALIASES,
        RESOLVER,
    )
    assert [(sq.claim_key, sq.section_code) for sq in subs] == [
        ("risk factors", "Item_1A"),
    ]


def test_impairment_charge_language_overrides_continuing_operations():
    _, subs = build_subqueries(
        "generic",
        "In Costco's 2024 10-K, what impairment charges were recorded for "
        "continuing operations and what was the largest component?",
        TICKERS,
        ALIASES,
        RESOLVER,
    )
    assert [(sq.claim_key, sq.section_code) for sq in subs] == [
        ("goodwill and impairment", "Item_8"),
    ]


def test_loyalty_revenue_accounting_uses_recognition_prompt():
    _, subs = build_subqueries(
        "generic",
        "In Costco's 2024 10-K, how does its loyalty program work, and how "
        "is revenue accounted for as customers earn points and store notes?",
        TICKERS,
        ALIASES,
        RESOLVER,
    )
    assert [(sq.claim_key, sq.section_code) for sq in subs] == [
        ("revenue recognition policy", "Item_8"),
    ]
    assert "loyalty points" in subs[0].question


def test_revenue_mix_and_location_count_become_two_sections():
    _, subs = build_subqueries(
        "generic",
        "According to Costco's 2024 10-K, what percentage of total revenue "
        "came from fuel and how many charging stations did it operate?",
        TICKERS,
        ALIASES,
        RESOLVER,
    )
    assert {(sq.claim_key, sq.section_code) for sq in subs} == {
        ("revenue composition", "Item_7"),
        ("operating footprint", "Item_1"),
    }


def test_customer_service_is_not_misread_as_products_or_customer_demand():
    _, subs = build_subqueries(
        "generic",
        "In Costco's 2024 10-K, describe the distribution and "
        "customer-service features of the business.",
        TICKERS,
        ALIASES,
        RESOLVER,
    )
    assert {(sq.claim_key, sq.section_code) for sq in subs} == {
        ("distribution network", "Item_1"),
        ("customer service", "Item_1"),
    }


def test_clause_local_company_year_pairing_beats_global_distance():
    resolver = SourceResolver([
        FilingRecord("AAA", 2023, "10-K", "aaa-23"),
        FilingRecord("BBB", 2026, "10-K", "bbb-26"),
    ])
    _, subs = build_subqueries(
        "arbitrary-request",
        "How did Alpha Retail in its 2023 10-K and Beta Retail in its 2026 10-K "
        "describe gross margin performance?",
        {"AAA", "BBB"},
        {"alpha retail": "AAA", "beta retail": "BBB"},
        resolver,
    )
    assert {(sq.ticker, sq.filing_year) for sq in subs} == {
        ("AAA", 2023),
        ("BBB", 2026),
    }


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
