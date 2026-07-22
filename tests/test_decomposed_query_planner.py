from __future__ import annotations

from src.decomposed_query_api import run_query


class CatalogCursor:
    def __init__(self):
        self.sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.sql = sql

    def fetchall(self):
        if "JOIN public.rag_eligible_10k_chunks" in self.sql:
            return [("COST", "COSTCO WHOLESALE CORP"), ("WMT", "WALMART INC")]
        return [
            ("COST", 2024, "10-K", "cost-24", "Item_7"),
            ("COST", 2024, "10-K", "cost-24", "Item_8"),
            ("WMT", 2024, "10-K", "wmt-24", "Item_7"),
            ("WMT", 2024, "10-K", "wmt-24", "Item_8"),
        ]


class CatalogConnection:
    def cursor(self):
        return CatalogCursor()


class FakePlanner:
    def __init__(self, plan):
        self.plan = plan
        self.calls = []

    def create_plan(self, question, *, context):
        self.calls.append((question, context))
        return self.plan


class PlannedRetriever:
    def __init__(self):
        self.conn = CatalogConnection()
        self.subqueries = []

    def retrieve_requirement(self, subquery, *, original_question):
        self.subqueries.append(subquery)
        evidence = []
        base = 100 if subquery.ticker == "COST" else 200
        for rank in range(1, 6):
            evidence.append(
                {
                    "chunk_id": base + rank,
                    "semantic_rank": rank,
                    "cross_encoder_rank_within_source": rank,
                    "cross_encoder_score": 1.0 - rank / 10,
                    "ticker": subquery.ticker,
                    "filing_year": subquery.filing_year,
                    "doc_type": subquery.doc_type,
                    "accession_number": subquery.accession_number,
                    "section_code": subquery.section_code,
                    "chunk_text": f"{subquery.ticker} evidence {rank}",
                }
            )
        return {"evidence": evidence}

    def close(self):
        return None


def ready_comparison_plan():
    return {
        "status": "ready",
        "query_type": "cross_company_comparison",
        "normalized_intent": "Compare Costco and Walmart revenue in 2024.",
        "requirements": [
            {
                "company_mention": "Coscto",
                "ticker": "COST",
                "year_mention": "2024",
                "filing_year": 2024,
                "document_type": "10-K",
                "claim_mention": "revenue",
                "claim_key": "revenue",
                "section_code": "Item_8",
                "search_queries": ["Costco reported revenue", "sales performance"],
            },
            {
                "company_mention": "Walmart",
                "ticker": "WMT",
                "year_mention": "2024",
                "filing_year": 2024,
                "document_type": "10-K",
                "claim_mention": "revenue",
                "claim_key": "revenue",
                "section_code": "Item_8",
                "search_queries": ["Walmart reported revenue", "sales performance"],
            },
        ],
        "clarification": None,
    }


def test_ai_plan_is_locked_then_sent_through_existing_evidence_gate():
    planner = FakePlanner(ready_comparison_plan())
    retriever = PlannedRetriever()
    result = run_query(
        "Compare Coscto and Walmart revenue in 2024",
        retriever=retriever,
        planner=planner,
    )
    assert result["status"] == "success"
    assert result["planner_mode"] == "ai"
    assert result["query_type"] == "cross_company_comparison"
    assert {item.accession_number for item in retriever.subqueries} == {
        "cost-24",
        "wmt-24",
    }
    assert retriever.subqueries[0].search_queries
    assert {item["comparison_side_id"] for item in result["evidence"]} == set(
        result["required_sides"]
    )


def test_clarification_returns_before_retrieval():
    planner = FakePlanner(
        {
            "status": "clarification_required",
            "query_type": "unsupported_or_ambiguous",
            "normalized_intent": "A company year is missing.",
            "requirements": [],
            "clarification": {
                "field": "filing_year",
                "question": "Which filing year should I use?",
                "reason": "The question did not state a filing year.",
                "options": ["2024"],
            },
        }
    )
    retriever = PlannedRetriever()
    result = run_query(
        "How did Costco revenue change?",
        retriever=retriever,
        planner=planner,
    )
    assert result["status"] == "clarification_required"
    assert result["clarification"]["options"] == ("2024",)
    assert result["evidence"] == []
    assert retriever.subqueries == []


def test_planner_cannot_route_to_a_source_outside_the_catalog():
    plan = ready_comparison_plan()
    plan["query_type"] = "single_source"
    plan["requirements"] = [
        {
            **plan["requirements"][0],
            "company_mention": "Tesla",
            "ticker": "TSLA",
            "search_queries": ["Tesla revenue"],
        }
    ]
    result = run_query(
        "What was Tesla revenue in 2024?",
        retriever=PlannedRetriever(),
        planner=FakePlanner(plan),
    )
    assert result["status"] == "retrieval_failed"
    assert result["error_code"] == "PLANNER_SOURCE_INVALID"
    assert result["evidence"] == []
