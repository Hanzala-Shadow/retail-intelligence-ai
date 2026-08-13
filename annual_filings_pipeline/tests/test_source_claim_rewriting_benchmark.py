from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import run_source_claim_rewriting_benchmark as runner
from src.query_api import SourceSpec


def candidate(chunk_id: int, requirement_id: str, score: float) -> dict:
    return {"chunk_id": chunk_id, "requirement_id": requirement_id, "cross_encoder_score": score}


class SourceClaimRewritingTests(unittest.TestCase):
    def test_registered_config_loads_all_methods(self):
        config = runner.load_config(Path("config/retrieval_rewrite_requirements_v1.json"))
        self.assertTrue(config.in_sample)
        self.assertEqual(config.semantic_depth, 20)
        self.assertEqual(config.final_evidence_count, 5)
        self.assertEqual(config.methods, runner.ALLOWED_METHODS)
        self.assertEqual(len(config.questions), 9)

    def test_config_rejects_gold_like_claim_text(self):
        raw = json.loads(Path("config/retrieval_rewrite_requirements_v1.json").read_text())
        raw["questions"]["10K-V2-I7-002"]["routes"][0][0][1] = "use supporting passage"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text(json.dumps(raw))
            with self.assertRaisesRegex(ValueError, "prohibited"):
                runner.load_config(path)

    def test_unregistered_question_gets_deterministic_route_requirements(self):
        config = runner.load_config(Path("config/retrieval_rewrite_requirements_v1.json"))
        first = runner.requirements_for(config, "Q", 2)
        second = runner.requirements_for(config, "Q", 2)
        self.assertEqual(first, second)
        self.assertEqual([route[0].requirement_id for route in first], ["Q-route-01", "Q-route-02"])

    def test_source_claim_query_retains_authorized_source(self):
        source = SourceSpec("TGT", 2024, "0000027419-24-000032", "Item_1A", "10-K")
        requirement = runner.Requirement("target", "vendors and common carriers")
        query = runner.build_query("source_claim_specific_rewrite", "original", source, requirement)
        self.assertIn("TGT", query)
        self.assertIn("2024", query)
        self.assertIn("Item_1A", query)
        self.assertIn(requirement.claim, query)
        self.assertNotIn("original", query)

    def test_claim_requirement_query_excludes_source_prompt_text(self):
        source = SourceSpec("TGT", 2024, "0000027419-24-000032", "Item_1A")
        requirement = runner.Requirement("target", "vendors and common carriers")
        query = runner.build_query(
            "claim_specific_requirement_aware", "original", source, requirement
        )
        self.assertEqual(query, requirement.claim)
        self.assertNotIn("TGT", query)
        self.assertNotIn("2024", query)

    def test_source_only_control_keeps_original_question_unchanged(self):
        source = SourceSpec("TGT", 2024, "0000027419-24-000032", "Item_1A")
        requirement = runner.Requirement("target", "vendors and common carriers")
        original = "Compare Lands' End and Target."
        self.assertEqual(
            runner.build_query("source_only_original_control", original, source, requirement),
            original,
        )

    def test_unregistered_question_never_rewrites(self):
        source = SourceSpec("ASO", 2024, "accession", "Item_1")
        requirement = runner.Requirement("fallback", "the question's requested disclosure")
        original = "How does the distribution network support fulfillment?"
        for method in runner.ALLOWED_METHODS:
            self.assertEqual(
                runner.build_query(
                    method,
                    original,
                    source,
                    requirement,
                    rewrite_enabled=False,
                ),
                original,
            )

    def test_git_head_uses_per_command_safe_directory(self):
        with patch.object(subprocess, "check_output", return_value="abc123\n") as mocked:
            self.assertEqual(runner._git_head(), "abc123")
        command = mocked.call_args.args[0]
        self.assertEqual(command[:3], ["git", "-c", f"safe.directory={runner.REPO_ROOT}"])
        self.assertEqual(command[-2:], ["rev-parse", "HEAD"])

    def test_requirement_aware_guarantees_one_unique_chunk_per_requirement(self):
        groups = [
            [candidate(1, "a", 0.9), candidate(2, "a", 0.8)],
            [candidate(1, "b", 0.95), candidate(3, "b", 0.7)],
            [candidate(4, "c", 0.6), candidate(5, "c", 0.5)],
        ]
        selected, uncovered = runner.aggregate_requirement_aware(groups, 5)
        self.assertEqual(uncovered, [])
        self.assertEqual([row["chunk_id"] for row in selected[:3]], [1, 3, 4])
        self.assertEqual(len({row["chunk_id"] for row in selected}), 5)

    def test_requirement_aware_reports_uncovered_when_limit_is_too_small(self):
        groups = [[candidate(1, "a", 1.0)], [candidate(2, "b", 0.9)]]
        selected, uncovered = runner.aggregate_requirement_aware(groups, 1)
        self.assertEqual([row["chunk_id"] for row in selected], [1])
        self.assertEqual(uncovered, ["b"])


if __name__ == "__main__":
    unittest.main()
