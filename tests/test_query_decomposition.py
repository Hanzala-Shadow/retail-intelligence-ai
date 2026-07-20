from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.query_decomposition import (
    ContractError, FilingRecord, ProductionRetrieverAdapter, RetrievedChunk,
    SourceResolver, aggregate, build_subqueries, validate_citations,
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


def test_citations_require_all_fields_and_sides():
    _, subs = build_subqueries("r", "How did Costco gross margin change from 2023 to 2024?", TICKERS, ALIASES, RESOLVER)
    response = aggregate(subs, FakeAdapter())
    citations = [{field: item[field] for field in ("ticker", "filing_year", "doc_type", "accession_number", "section_code", "chunk_id", "aggregated_rank")} | {"page_unavailable_reason": "html_source"} for item in response["evidence"]]
    assert validate_citations(citations, response["evidence"], response["required_sides"])[0]
    assert not validate_citations([{"chunk_id": citations[0]["chunk_id"]}], response["evidence"], response["required_sides"])[0]
