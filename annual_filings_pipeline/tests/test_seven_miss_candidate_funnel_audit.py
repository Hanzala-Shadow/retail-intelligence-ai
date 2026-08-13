from __future__ import annotations

import unittest

from scripts import run_seven_miss_candidate_funnel_audit as audit


class CandidateFunnelAuditTests(unittest.TestCase):
    def test_retrieval_view_excludes_every_gold_field(self):
        row = {
            "question_id": "q",
            "question_group": "Item_7",
            "question": "question",
            "expected_tickers": "AAA",
            "expected_years": "2025",
            "required_doc_type": "10-K",
            "required_sections": "Item_7",
            "supporting_accession_numbers": "acc",
            "supporting_chunk_ids": "999",
            "supporting_chunk_indexes": "8",
            "supporting_passages": "gold",
            "expected_answer": "gold answer",
        }

        value = audit._retrieval_view(row)

        self.assertEqual(value["question_id"], "q")
        self.assertFalse(set(audit.GOLD_FIELDS) & set(value))
        self.assertNotIn("expected_answer", value)

    def test_rrf_is_deterministic_and_uses_both_rankings(self):
        semantic = [{"chunk_id": 1}, {"chunk_id": 2}, {"chunk_id": 3}]
        lexical = [{"chunk_id": 3}, {"chunk_id": 2}, {"chunk_id": 4}]

        first = audit._rrf_rows(semantic, lexical)
        second = audit._rrf_rows(semantic, lexical)

        self.assertEqual(first, second)
        self.assertEqual(first[0]["chunk_id"], 3)
        self.assertEqual({row["chunk_id"] for row in first}, {1, 2, 3, 4})
        self.assertEqual([row["rrf_rank"] for row in first], [1, 2, 3, 4])

    def test_candidate_generation_and_reranking_classifications(self):
        raw = {
            "question_id": "q",
            "supporting_chunk_ids": "101|202",
            "supporting_chunk_indexes": "5|6",
            "supporting_passages": "passage one|passage two",
        }
        source_a = {
            "ticker": "AAA",
            "filing_year": 2024,
            "doc_type": "10-K",
            "accession_number": "acc-a",
            "section_code": "Item_7",
        }
        source_b = {
            "ticker": "BBB",
            "filing_year": 2025,
            "doc_type": "10-K",
            "accession_number": "acc-b",
            "section_code": "Item_8",
        }
        routes = [
            {
                "route_index": 1,
                "source": source_a,
                "candidate_count": 30,
                "semantic": [
                    {"chunk_id": chunk_id, "semantic_rank": rank}
                    for rank, chunk_id in enumerate(range(80, 110), 1)
                ],
                "lexical": [{"chunk_id": 101, "lexical_rank": 1}],
                "reranked_top20": [],
                "hybrid": [{"chunk_id": 101, "rrf_rank": 2}],
            },
            {
                "route_index": 2,
                "source": source_b,
                "candidate_count": 20,
                "semantic": [{"chunk_id": 202, "semantic_rank": 4}],
                "lexical": [{"chunk_id": 202, "lexical_rank": 8}],
                "reranked_top20": [{"chunk_id": 202, "cross_encoder_rank": 7}],
                "hybrid": [{"chunk_id": 202, "rrf_rank": 3}],
            },
        ]

        class FakeAuditor:
            @staticmethod
            def fetch_neighbors(source, chunk_index):
                passage = "passage one" if source.ticker == "AAA" else "passage two"
                chunk_id = 101 if source.ticker == "AAA" else 202
                return [
                    {
                        "chunk_id": chunk_id,
                        "chunk_index": chunk_index,
                        "chunk_text": passage,
                        "token_count": 10,
                    }
                ]

        labels = audit._gold_labels(raw, routes, FakeAuditor())

        self.assertEqual(labels[0]["semantic_rank"], 22)
        self.assertEqual(
            labels[0]["primary_machine_classification"],
            "candidate_generation_failure",
        )
        self.assertEqual(
            labels[1]["primary_machine_classification"], "reranking_failure"
        )
        self.assertEqual(labels[1]["cross_encoder_rank_if_top20"], 7)

    def test_passage_mismatch_is_gold_contract_issue(self):
        raw = {
            "question_id": "q",
            "supporting_chunk_ids": "10",
            "supporting_chunk_indexes": "2",
            "supporting_passages": "missing exact passage",
        }
        source = {
            "ticker": "AAA",
            "filing_year": 2024,
            "doc_type": "10-K",
            "accession_number": "acc",
            "section_code": "Item_7",
        }
        route = {
            "route_index": 1,
            "source": source,
            "candidate_count": 20,
            "semantic": [{"chunk_id": 10, "semantic_rank": 1}],
            "lexical": [{"chunk_id": 10, "lexical_rank": 1}],
            "reranked_top20": [{"chunk_id": 10, "cross_encoder_rank": 1}],
            "hybrid": [{"chunk_id": 10, "rrf_rank": 1}],
        }

        class FakeAuditor:
            @staticmethod
            def fetch_neighbors(source, chunk_index):
                return [
                    {
                        "chunk_id": 10,
                        "chunk_index": 2,
                        "chunk_text": "different text",
                        "token_count": 2,
                    }
                ]

        label = audit._gold_labels(raw, [route], FakeAuditor())[0]
        self.assertFalse(label["supporting_passage_byte_exact_contained"])
        self.assertFalse(label["supporting_passage_normalized_contained"])
        self.assertEqual(label["primary_machine_classification"], "gold_contract_issue")

    def test_whitespace_only_passage_difference_is_not_contract_issue(self):
        raw = {
            "question_id": "q",
            "supporting_chunk_ids": "10",
            "supporting_chunk_indexes": "2",
            "supporting_passages": "answer text",
        }
        source = {
            "ticker": "AAA",
            "filing_year": 2024,
            "doc_type": "10-K",
            "accession_number": "acc",
            "section_code": "Item_7",
        }
        route = {
            "route_index": 1,
            "source": source,
            "candidate_count": 20,
            "semantic": [{"chunk_id": 10, "semantic_rank": 4}],
            "lexical": [],
            "reranked_top20": [{"chunk_id": 10, "cross_encoder_rank": 8}],
            "hybrid": [{"chunk_id": 10, "rrf_rank": 4}],
        }

        class FakeAuditor:
            @staticmethod
            def fetch_neighbors(source, chunk_index):
                return [
                    {
                        "chunk_id": 10,
                        "chunk_index": 2,
                        "chunk_text": "prefix answer\n\ttext suffix",
                        "token_count": 4,
                    }
                ]

        label = audit._gold_labels(raw, [route], FakeAuditor())[0]
        self.assertFalse(label["supporting_passage_byte_exact_contained"])
        self.assertTrue(label["supporting_passage_normalized_contained"])
        self.assertEqual(label["primary_machine_classification"], "reranking_failure")


if __name__ == "__main__":
    unittest.main()
