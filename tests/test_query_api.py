import unittest
from unittest.mock import patch

import numpy as np

from src import query_api


class FakeColumn:
    def __init__(self, name):
        self.name = name


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.description = [
            FakeColumn(name)
            for name in (
                "chunk_id", "ticker", "filing_year", "accession_number",
                "doc_type", "section_code", "chunk_index", "chunk_text",
                "embedding_text", "token_count", "semantic_score",
            )
        ]
        self.sql = None
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def execute(self, sql, params):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, rows):
        self.fake_cursor = FakeCursor(rows)

    def cursor(self):
        return self.fake_cursor


class FakeBiEncoder:
    def encode(self, texts, **kwargs):
        return np.asarray([[1.0] + [0.0] * 767], dtype=np.float32)


class FakeCrossEncoder:
    def predict(self, pairs, **kwargs):
        return np.asarray([float(text.rsplit(" ", 1)[-1]) for _, text in pairs])


def database_rows(start, count):
    return [
        (
            start + index, "TEST", 2025, "acc", "10-K", "Item_7", index,
            f"chunk text {index}", f"embedding {float(index)}", 100, 1.0-index/100,
        )
        for index in range(count)
    ]


class QueryApiTests(unittest.TestCase):
    def test_policy_is_locked(self):
        self.assertEqual(query_api.CANDIDATES_PER_SOURCE, 20)
        self.assertEqual(query_api.FINAL_EVIDENCE_COUNT, 5)
        self.assertEqual(query_api.BI_ENCODER_DIMENSION, 768)

    def test_source_requires_route_fields_and_10k(self):
        with self.assertRaises(ValueError):
            query_api.SourceSpec.from_mapping({"ticker": "A"})
        with self.assertRaises(ValueError):
            query_api.SourceSpec.from_mapping({
                "ticker": "A", "filing_year": 2025,
                "accession_number": "x", "section_code": "Item_7",
                "doc_type": "ESG",
            })

    def test_round_robin_preserves_sources_and_deduplicates(self):
        first = [{"chunk_id": 1}, {"chunk_id": 2}, {"chunk_id": 3}]
        second = [{"chunk_id": 1}, {"chunk_id": 4}, {"chunk_id": 5}]
        result = query_api._round_robin([first, second], 5)
        self.assertEqual([row["chunk_id"] for row in result], [1, 2, 4, 3, 5])

    def test_fetch_uses_every_required_filter_and_limit(self):
        conn = FakeConnection(database_rows(100, 2))
        retriever = query_api.ProductionRetriever(
            conn=conn, bi_encoder=FakeBiEncoder(), cross_encoder=FakeCrossEncoder()
        )
        source = query_api.SourceSpec("TEST", 2025, "acc", "Item_7")
        rows = retriever._fetch_candidates("question", source, "[1,0]")
        self.assertEqual(len(rows), 2)
        sql = conn.fake_cursor.sql
        for field in ("ticker", "filing_year", "doc_type", "accession_number", "section_code"):
            self.assertIn(field, sql)
        self.assertEqual(conn.fake_cursor.params[-1], 20)

    def test_retrieve_reranks_and_returns_exactly_five(self):
        conn = FakeConnection(database_rows(100, 8))
        retriever = query_api.ProductionRetriever(
            conn=conn, bi_encoder=FakeBiEncoder(), cross_encoder=FakeCrossEncoder()
        )
        source = query_api.SourceSpec("TEST", 2025, "acc", "Item_7")
        with patch.object(query_api, "_vector_literal", return_value="[1,0]"):
            result = retriever.retrieve("test question", [source])
        self.assertEqual(len(result["evidence"]), 5)
        self.assertEqual(
            [row["chunk_id"] for row in result["evidence"]],
            [107, 106, 105, 104, 103],
        )
        self.assertEqual(result["candidate_counts_by_source"], [8])

    def test_empty_question_and_duplicate_sources_fail_closed(self):
        retriever = query_api.ProductionRetriever(
            conn=FakeConnection([]),
            bi_encoder=FakeBiEncoder(),
            cross_encoder=FakeCrossEncoder(),
        )
        source = query_api.SourceSpec("TEST", 2025, "acc", "Item_7")
        with self.assertRaises(ValueError):
            retriever.retrieve("", [source])
        with self.assertRaises(ValueError):
            retriever.retrieve("question", [source, source])


if __name__ == "__main__":
    unittest.main()
