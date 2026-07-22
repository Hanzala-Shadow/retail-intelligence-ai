from __future__ import annotations

import json

import pytest

from src.ai_query_planner import (
    OpenAIQueryPlanner,
    build_planner_context,
    validate_and_lock_plan,
)
from src.query_decomposition import ContractError, FilingRecord, SourceResolver


TICKERS = {"COST", "WMT"}
ALIASES = {"costco": "COST", "walmart": "WMT"}
SECTIONS = {
    ("COST", 2024, "10-K", "cost-24"): {"Item_7", "Item_8"},
    ("WMT", 2024, "10-K", "wmt-24"): {"Item_7", "Item_8"},
}
RESOLVER = SourceResolver(
    [
        FilingRecord("COST", 2024, "10-K", "cost-24"),
        FilingRecord("WMT", 2024, "10-K", "wmt-24"),
    ],
    SECTIONS,
)


def requirement(
    ticker="COST",
    claim="revenue",
    section="Item_8",
    queries=None,
    company_mention=None,
):
    return {
        "company_mention": company_mention or ("Costco" if ticker == "COST" else "Walmart"),
        "ticker": ticker,
        "year_mention": "2024",
        "filing_year": 2024,
        "document_type": "10-K",
        "claim_mention": "revenue" if claim == "revenue" else claim,
        "claim_key": claim,
        "section_code": section,
        "search_queries": queries or ["reported revenue and sales"],
    }


def ready_plan(requirements, query_type="single_source"):
    return {
        "status": "ready",
        "query_type": query_type,
        "normalized_intent": "Find the requested filing evidence.",
        "requirements": requirements,
        "clarification": None,
    }


def test_misspelled_company_can_be_normalized_then_locked_to_exact_filing():
    plan = validate_and_lock_plan(
        ready_plan([requirement(company_mention="Coscto")]),
        question="What was Coscto revenue in 2024?",
        known_tickers=TICKERS,
        aliases=ALIASES,
        resolver=RESOLVER,
    )
    assert plan.subqueries[0].ticker == "COST"
    assert plan.subqueries[0].accession_number == "cost-24"
    assert plan.subqueries[0].search_queries == ("reported revenue and sales",)


def test_comparison_plan_creates_one_locked_requirement_per_side():
    raw = ready_plan(
        [
            requirement("COST", queries=["Costco revenue performance"]),
            requirement("WMT", queries=["Walmart revenue performance"]),
        ],
        query_type="cross_company_comparison",
    )
    plan = validate_and_lock_plan(
        raw,
        question="Compare Costco and Walmart revenue in 2024",
        known_tickers=TICKERS,
        aliases=ALIASES,
        resolver=RESOLVER,
    )
    assert {(item.ticker, item.accession_number) for item in plan.subqueries} == {
        ("COST", "cost-24"),
        ("WMT", "wmt-24"),
    }
    assert len({item.comparison_side_id for item in plan.subqueries}) == 2


def test_database_backed_clarification_is_accepted_without_authorizing_search():
    raw = {
        "status": "clarification_required",
        "query_type": "unsupported_or_ambiguous",
        "normalized_intent": "Compare revenue but the company is unclear.",
        "requirements": [],
        "clarification": {
            "field": "company",
            "question": "Did you mean Costco or Walmart?",
            "reason": "The company name could not be resolved safely.",
            "options": ["COST", "WMT"],
        },
    }
    plan = validate_and_lock_plan(
        raw,
        question="Compare the retailer's revenue",
        known_tickers=TICKERS,
        aliases=ALIASES,
        resolver=RESOLVER,
    )
    assert plan.status == "clarification_required"
    assert plan.subqueries == ()
    assert plan.clarification.options == ("COST", "WMT")


def test_clarification_cannot_offer_company_outside_approved_corpus():
    raw = {
        "status": "clarification_required",
        "query_type": "unsupported_or_ambiguous",
        "normalized_intent": "Company is unclear.",
        "requirements": [],
        "clarification": {
            "field": "company",
            "question": "Which company?",
            "reason": "More information is needed.",
            "options": ["TSLA"],
        },
    }
    with pytest.raises(ContractError) as caught:
        validate_and_lock_plan(
            raw,
            question="How did the company perform?",
            known_tickers=TICKERS,
            aliases=ALIASES,
            resolver=RESOLVER,
        )
    assert caught.value.code == "PLANNER_RESPONSE_INVALID"


def test_search_query_cannot_drift_to_another_company_or_year():
    for unsafe_query in ("Walmart revenue in 2024", "Costco revenue in 2023"):
        raw = ready_plan([requirement(queries=[unsafe_query])])
        with pytest.raises(ContractError) as caught:
            validate_and_lock_plan(
                raw,
                question="Costco revenue in 2024",
                known_tickers=TICKERS,
                aliases=ALIASES,
                resolver=RESOLVER,
            )
        assert caught.value.code == "PLANNER_SOURCE_DRIFT"


def test_section_must_exist_in_the_locked_filing():
    raw = ready_plan([requirement(section="Item_1A", claim="risk")])
    with pytest.raises(ContractError) as caught:
        validate_and_lock_plan(
            raw,
            question="Costco risk in 2024",
            known_tickers=TICKERS,
            aliases=ALIASES,
            resolver=RESOLVER,
        )
    assert caught.value.code == "PLANNER_SECTION_INVALID"


def test_query_type_is_recomputed_from_locked_requirements():
    raw = ready_plan(
        [requirement("COST"), requirement("WMT")],
        query_type="single_source",
    )
    with pytest.raises(ContractError) as caught:
        validate_and_lock_plan(
            raw,
            question="Compare Costco and Walmart revenue in 2024",
            known_tickers=TICKERS,
            aliases=ALIASES,
            resolver=RESOLVER,
        )
    assert caught.value.code == "PLANNER_TYPE_MISMATCH"


def test_company_normalization_must_be_grounded_in_user_wording():
    raw = ready_plan([requirement("COST", company_mention="Walmart")])
    with pytest.raises(ContractError) as caught:
        validate_and_lock_plan(
            raw,
            question="What was Walmart revenue in 2024?",
            known_tickers=TICKERS,
            aliases=ALIASES,
            resolver=RESOLVER,
        )
    assert caught.value.code == "PLANNER_GROUNDING_FAILED"


def test_known_claim_cannot_be_normalized_into_another_claim():
    raw = ready_plan(
        [
            {
                **requirement(claim="gross margin", section="Item_7"),
                "claim_mention": "revenue",
            }
        ]
    )
    with pytest.raises(ContractError) as caught:
        validate_and_lock_plan(
            raw,
            question="What was Costco revenue in 2024?",
            known_tickers=TICKERS,
            aliases=ALIASES,
            resolver=RESOLVER,
        )
    assert caught.value.code == "PLANNER_GROUNDING_FAILED"


def test_context_contains_catalog_facts_but_not_database_credentials():
    context = build_planner_context(TICKERS, ALIASES, RESOLVER)
    assert {item["ticker"] for item in context["eligible_sources"]} == TICKERS
    assert "database_url" not in json.dumps(context).casefold()
    cost = next(item for item in context["eligible_sources"] if item["ticker"] == "COST")
    assert cost["sections"] == ["Item_7", "Item_8"]


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, planned):
        self.planned = planned
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(
            {
                "choices": [
                    {"message": {"content": json.dumps(self.planned)}}
                ]
            }
        )


def test_live_client_requests_strict_json_without_making_a_real_call():
    planned = ready_plan([requirement()])
    session = FakeSession(planned)
    client = OpenAIQueryPlanner(api_key="test-key", session=session)
    result = client.create_plan(
        "What was Costco revenue in 2024?",
        context=build_planner_context(TICKERS, ALIASES, RESOLVER),
    )
    assert result == planned
    _, call = session.calls[0]
    assert call["json"]["response_format"]["type"] == "json_schema"
    assert call["headers"]["Authorization"] == "Bearer test-key"
