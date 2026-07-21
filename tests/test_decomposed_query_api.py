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


def test_aliases_retain_safe_full_names_and_drop_generic_first_words():
    tickers, aliases = _aliases_from_connection(AliasConnection())
    assert tickers == {"DBGI", "DECK", "GPI", "LULU", "ORLY"}
    assert aliases["o reilly automotive"] == "ORLY"
    assert aliases["deckers"] == "DECK"
    assert aliases["lululemon"] == "LULU"
    assert "digital" not in aliases
    assert "group" not in aliases


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
