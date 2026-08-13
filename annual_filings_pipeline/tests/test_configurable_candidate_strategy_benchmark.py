from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import run_configurable_candidate_strategy_benchmark as runner


def row(chunk_id, semantic_rank=None, lexical_rank=None):
    value = {"chunk_id": chunk_id, "embedding_text": f"chunk {chunk_id}"}
    if semantic_rank is not None:
        value["semantic_rank"] = semantic_rank
    if lexical_rank is not None:
        value["lexical_rank"] = lexical_rank
    return value


class ConfigurableCandidateStrategyTests(unittest.TestCase):
    def test_registered_config_loads_five_strategies(self):
        config = runner.load_config(Path("config/retrieval_candidate_strategies_v1.json"))
        self.assertEqual(config.schema_version, 1)
        self.assertTrue(config.in_sample)
        self.assertEqual(len(config.strategies), 5)
        self.assertEqual(config.final_evidence_count, 5)

    def test_invalid_or_duplicate_strategy_fails_closed(self):
        value = {
            "schema_version": 1,
            "experiment_id": "x",
            "in_sample": True,
            "final_evidence_count": 5,
            "rrf_k": 60,
            "strategies": [
                {
                    "method_id": "same",
                    "candidate_policy": "semantic",
                    "semantic_depth": 20,
                    "lexical_depth": 0,
                    "pool_limit": 20,
                },
                {
                    "method_id": "same",
                    "candidate_policy": "semantic",
                    "semantic_depth": 40,
                    "lexical_depth": 0,
                    "pool_limit": 40,
                },
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unique"):
                runner.load_config(path)

    def test_semantic_policy_uses_declared_depth(self):
        strategy = runner.Strategy("semantic", "semantic", 3, 0, 3)
        semantic = [row(index, semantic_rank=index) for index in range(1, 7)]
        selected = runner.select_pool(strategy, semantic, [], 60)
        self.assertEqual([item["chunk_id"] for item in selected], [1, 2, 3])

    def test_union_deduplicates_and_retains_lexical_candidates(self):
        strategy = runner.Strategy("union", "union", 3, 3, 6)
        semantic = [row(1, 1), row(2, 2), row(3, 3)]
        lexical = [row(3, lexical_rank=1), row(4, lexical_rank=2)]
        selected = runner.select_pool(strategy, semantic, lexical, 60)
        self.assertEqual([item["chunk_id"] for item in selected], [1, 2, 3, 4])
        self.assertEqual(selected[2]["semantic_rank"], 3)
        self.assertEqual(selected[2]["lexical_rank"], 1)

    def test_rrf_pool_is_deterministic_and_promotes_shared_candidate(self):
        strategy = runner.Strategy("rrf", "rrf_pool", 3, 3, 3)
        semantic = [row(1, 1), row(2, 2), row(3, 3)]
        lexical = [row(3, lexical_rank=1), row(4, lexical_rank=2), row(2, lexical_rank=3)]
        first = runner.select_pool(strategy, semantic, lexical, 60)
        second = runner.select_pool(strategy, semantic, lexical, 60)
        self.assertEqual(first, second)
        self.assertEqual(first[0]["chunk_id"], 3)
        self.assertEqual(len(first), 3)

    def test_approved_view_excludes_gold_fields(self):
        raw = {field: field for field in runner.ROUTING_FIELDS}
        for field in runner.PROHIBITED_FIELDS:
            raw[field] = "gold"
        approved = runner._approved_view(raw)
        self.assertFalse(set(runner.PROHIBITED_FIELDS) & set(approved))


if __name__ == "__main__":
    unittest.main()
