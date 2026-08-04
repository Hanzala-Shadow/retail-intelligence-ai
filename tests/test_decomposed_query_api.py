from __future__ import annotations

import sys
import types
from dataclasses import dataclass


@dataclass(frozen=True)
class StubSourceSpec:
    ticker: str
    filing_year: int
    accession_number: str
    section_code: str
    doc_type: str = "10-K"

    @classmethod
    def from_mapping(cls, value):
        return cls(value["ticker"], int(value["filing_year"]), value["accession_number"], value["section_code"], value.get("doc_type", "10-K"))


stub = types.ModuleType("src.query_api")
stub.SourceSpec = StubSourceSpec
stub.ProductionRetriever = object
sys.modules.setdefault("src.query_api", stub)

from src.decomposed_query_api import _aliases_from_connection, run_query


class AliasCursor:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql):
        self.sql = sql

    def fetchall(self):
        return [
            ("DBGI", "DIGITAL BRANDS GROUP INC"),
            ("DECK", "DECKERS OUTDOOR CORP"),
            ("GPI", "GROUP 1 AUTOMOTIVE INC"),
            ("LULU", "LULULEMON ATHLETICA INC"),
            ("ORLY", "O'REILLY AUTOMOTIVE INC"),
        ]


class AliasConnection:
    def cursor(self):
        return AliasCursor()


class PossessiveAliasCursor(AliasCursor):
    def fetchall(self):
        return [("RTL", "RETAILER'S GROUP INC")]


class PossessiveAliasConnection:
    def cursor(self):
        return PossessiveAliasCursor()


def test_aliases_retain_safe_full_names_and_drop_generic_first_words():
    tickers, aliases = _aliases_from_connection(AliasConnection())
    assert tickers == {"DBGI", "DECK", "GPI", "LULU", "ORLY"}
    assert aliases["o reilly automotive"] == "ORLY"
    assert aliases["deckers"] == "DECK"
    assert aliases["lululemon"] == "LULU"
    assert "digital" not in aliases
    assert "group" not in aliases


def test_aliases_add_only_unique_corpus_derived_leading_names():
    tickers, aliases = _aliases_from_connection(AliasConnection())
    assert aliases["group 1"] == "GPI"
    assert aliases["o reilly"] == "ORLY"
    assert aliases["deckers"] == "DECK"
    assert "group" not in aliases


def test_aliases_generate_bidirectional_possessive_forms():
    tickers, aliases = _aliases_from_connection(PossessiveAliasConnection())
    assert tickers == {"RTL"}
    assert aliases["retailer s group"] == "RTL"
    assert aliases["retailers group"] == "RTL"
    assert aliases["retailers"] == "RTL"


class SimpleRawRetriever:
    def __init__(self):
        self.conn = None
        self.closed = False

    def retrieve(self, question, sources):
        source = sources[0]
        evidence = []
        for rank in range(1, 6):
            evidence.append({
                "chunk_id": rank, "ticker": source.ticker,
                "filing_year": source.filing_year,
                "accession_number": source.accession_number,
                "doc_type": source.doc_type, "section_code": source.section_code,
                "chunk_index": rank - 1, "chunk_text": f"text {rank}",
                "embedding_text": f"text {rank}", "token_count": 100,
                "semantic_score": 0.9, "semantic_rank": rank,
                "source": source.__dict__, "cross_encoder_score": 0.8,
                "cross_encoder_rank_within_source": rank, "final_rank": rank,
            })
        return {"question": question, "policy": {}, "sources": [source.__dict__], "candidate_counts_by_source": [20], "evidence": evidence}

    def close(self):
        self.closed = True

    def retrieve_anchored(self, question, requirements):
        requirement = list(requirements)[0]
        return {
            "status": "success",
            "question": question,
            "policy": {"policy_id": "balanced_anchored_round_robin_k16"},
            "requirement_coverage": [{
                "subquery_id": requirement.subquery_id,
                "retrieval_status": "represented",
            }],
            "evidence": [
                {
                    "chunk_id": rank,
                    "final_rank": rank,
                    "aggregated_rank": rank,
                    "selected_for_subquery_id": requirement.subquery_id,
                }
                for rank in range(1, 17)
            ],
        }


def test_explicit_simple_query_preserves_locked_path():
    retriever = SimpleRawRetriever()
    response = run_query(
        "What were the revenue drivers?",
        {"ticker": "COST", "filing_year": 2024, "doc_type": "10-K", "accession_number": "acc", "section_code": "Item_7"},
        retriever=retriever,
    )
    assert response["status"] == "success"
    assert response["is_decomposed"] is False
    assert len(response["evidence"]) == 5
    assert [x["aggregated_rank"] for x in response["evidence"]] == [1, 2, 3, 4, 5]
    assert retriever.closed is False


def test_explicit_query_uses_anchored_path_only_when_feature_flagged(monkeypatch):
    monkeypatch.setenv(
        "RAG_RETRIEVAL_POLICY",
        "balanced_anchored_round_robin_k16",
    )
    retriever = SimpleRawRetriever()
    response = run_query(
        "What were the revenue drivers?",
        {
            "ticker": "COST",
            "filing_year": 2024,
            "doc_type": "10-K",
            "accession_number": "acc",
            "section_code": "Item_7",
        },
        retriever=retriever,
    )
    assert response["status"] == "success"
    assert response["is_decomposed"] is False
    assert response["policy"]["policy_id"] == "balanced_anchored_round_robin_k16"
    assert len(response["evidence"]) == 16
