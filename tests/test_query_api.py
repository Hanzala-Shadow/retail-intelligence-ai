import unittest
from types import SimpleNamespace
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


class FakeRemoteReranker:
    def __init__(self):
        self.calls = []

    def score(self, **kwargs):
        self.calls.append(kwargs)
        return [float(text.rsplit(" ", 1)[-1]) for _, text in kwargs["pairs"]]


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

    def test_soft_section_fetch_keeps_source_hard_and_sections_broad(self):
        conn = FakeConnection(database_rows(100, 2))
        retriever = query_api.ProductionRetriever(
            conn=conn, bi_encoder=FakeBiEncoder(), cross_encoder=FakeCrossEncoder()
        )
        source = query_api.SourceSpec("TEST", 2025, "acc", "Item_8")
        rows = retriever._fetch_soft_section_candidates(
            "question", source, "[1,0]"
        )
        self.assertEqual(len(rows), 2)
        self.assertIn("section_code = ANY", conn.fake_cursor.sql)
        self.assertEqual(
            tuple(conn.fake_cursor.params[5]),
            query_api.SUPPORTED_RETRIEVAL_SECTIONS,
        )
        self.assertTrue(all(row["preferred_section_code"] == "Item_8" for row in rows))

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

    def test_adaptive_narrative_requirement_uses_multiview_rrf_order(self):
        conn = FakeConnection(database_rows(100, 8))
        retriever = query_api.ProductionRetriever(
            conn=conn, bi_encoder=FakeBiEncoder(), cross_encoder=FakeCrossEncoder()
        )
        subquery = SimpleNamespace(
            question="focused inventory performance",
            claim_key="inventory performance",
            ticker="TEST", filing_year=2025, accession_number="acc",
            section_code="Item_7", doc_type="10-K",
        )
        with patch.object(query_api, "_vector_literal", return_value="[1,0]"):
            result = retriever.retrieve_requirement(
                subquery, original_question="original inventory question"
            )
        self.assertEqual(
            [row["chunk_id"] for row in result["evidence"]],
            [100, 101, 102, 103, 104],
        )
        self.assertEqual(
            result["policy"]["section_selection"],
            "multiview_rrf_narrative",
        )

    def test_adaptive_item8_requirement_preserves_cross_encoder_order(self):
        conn = FakeConnection(database_rows(100, 8))
        retriever = query_api.ProductionRetriever(
            conn=conn, bi_encoder=FakeBiEncoder(), cross_encoder=FakeCrossEncoder()
        )
        subquery = SimpleNamespace(
            question="focused revenue recognition",
            claim_key="revenue recognition policy",
            ticker="TEST", filing_year=2025, accession_number="acc",
            section_code="Item_8", doc_type="10-K",
        )
        with patch.object(query_api, "_vector_literal", return_value="[1,0]"):
            result = retriever.retrieve_requirement(
                subquery, original_question="original accounting question"
            )
        self.assertEqual(
            [row["chunk_id"] for row in result["evidence"]],
            [107, 106, 105, 104, 103],
        )
        self.assertEqual(
            result["policy"]["section_selection"],
            "cross_encoder_financial_notes",
        )

    def test_anchored_policy_returns_six_anchors_and_k16(self):
        conn = FakeConnection(database_rows(100, 30))
        retriever = query_api.ProductionRetriever(
            conn=conn,
            bi_encoder=FakeBiEncoder(),
            cross_encoder=FakeCrossEncoder(),
            anchor_cross_encoder=FakeCrossEncoder(),
            expansion_cross_encoder=FakeCrossEncoder(),
        )
        subquery = SimpleNamespace(
            subquery_id="sq-test",
            question="focused revenue performance",
            claim_key="revenue performance",
            comparison_side_id="TEST-2025-Item_7-revenue",
            ticker="TEST",
            filing_year=2025,
            accession_number="acc",
            section_code="Item_7",
            doc_type="10-K",
        )
        with patch.object(query_api, "_vector_literal", return_value="[1,0]"):
            result = retriever.retrieve_anchored(
                "original revenue question",
                [subquery],
            )
        self.assertEqual(len(result["evidence"]), 16)
        self.assertEqual(
            [item["selection_reason"] for item in result["evidence"][:6]],
            ["l12_anchor"] * 6,
        )
        self.assertEqual(
            result["policy"]["policy_id"],
            "balanced_anchored_round_robin_k16",
        )
        runtime = result["runtime_profile"]
        self.assertEqual(runtime["device"], "cpu")
        self.assertEqual(runtime["requirement_count"], 1)
        self.assertEqual(runtime["scored_pairs"]["anchor"], 30)
        self.assertEqual(runtime["scored_pairs"]["expansion"], 30)
        self.assertEqual(
            runtime["candidate_counts"]["unique_requirement_chunk_pairs"],
            30,
        )
        self.assertEqual(len(runtime["evidence_identity_sha256"]), 64)
        self.assertGreaterEqual(runtime["timings_ms"]["total"], 0)

    def test_runtime_device_rejects_unapproved_value(self):
        with patch.dict(
            "os.environ",
            {"RAG_MODEL_DEVICE": "automatic"},
        ):
            with self.assertRaisesRegex(ValueError, "RAG_MODEL_DEVICE"):
                query_api._runtime_model_device()

    def test_remote_backend_delegates_only_anchored_pair_scoring(self):
        remote = FakeRemoteReranker()
        retriever = query_api.ProductionRetriever(
            conn=FakeConnection([]),
            bi_encoder=FakeBiEncoder(),
            remote_reranker_client=remote,
            anchored_config=query_api.ProductionRetriever(
                conn=FakeConnection([])
            )._anchored_policy(),
        )
        pairs = [("question", "embedding 1.5"), ("question", "embedding 2.5")]
        with patch.dict(
            "os.environ",
            {"RAG_RERANKER_BACKEND": "remote", "RAG_MODEL_DEVICE": "cpu"},
        ):
            scores = retriever._score_anchored_pairs(pairs, role="anchor")
            self.assertEqual(query_api._runtime_profile_device(), "remote_cuda")
        self.assertEqual(scores, [1.5, 2.5])
        self.assertEqual(len(remote.calls), 1)
        self.assertEqual(remote.calls[0]["role"], "anchor")
        self.assertEqual(remote.calls[0]["max_length"], 512)

    def test_remote_and_local_scoring_preserve_exact_k16_identity(self):
        subquery = SimpleNamespace(
            subquery_id="sq-test",
            question="focused revenue performance",
            claim_key="revenue performance",
            comparison_side_id="TEST-2025-Item_7-revenue",
            ticker="TEST",
            filing_year=2025,
            accession_number="acc",
            section_code="Item_7",
            doc_type="10-K",
        )
        local = query_api.ProductionRetriever(
            conn=FakeConnection(database_rows(100, 30)),
            bi_encoder=FakeBiEncoder(),
            anchor_cross_encoder=FakeCrossEncoder(),
            expansion_cross_encoder=FakeCrossEncoder(),
        )
        remote = query_api.ProductionRetriever(
            conn=FakeConnection(database_rows(100, 30)),
            bi_encoder=FakeBiEncoder(),
            remote_reranker_client=FakeRemoteReranker(),
        )
        with patch.object(query_api, "_vector_literal", return_value="[1,0]"):
            local_result = local.retrieve_anchored("original question", [subquery])
        with patch.object(query_api, "_vector_literal", return_value="[1,0]"), patch.dict(
            "os.environ",
            {"RAG_RERANKER_BACKEND": "remote", "RAG_MODEL_DEVICE": "cpu"},
        ):
            remote_result = remote.retrieve_anchored("original question", [subquery])
        self.assertEqual(
            local_result["runtime_profile"]["evidence_identity_sha256"],
            remote_result["runtime_profile"]["evidence_identity_sha256"],
        )
        self.assertEqual(
            [row["chunk_id"] for row in local_result["evidence"]],
            [row["chunk_id"] for row in remote_result["evidence"]],
        )


if __name__ == "__main__":
    unittest.main()
