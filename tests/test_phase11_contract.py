import unittest
from unittest.mock import MagicMock, patch

from app.services.citation_validator import validate_answer
from app.services.semantic_guard import guarded_prompt


class Phase11ContractTests(unittest.TestCase):
    def test_unsupported_sidedness_is_rejected(self):
        evidence = [{"label": "C1", "text": "The platform serves four participant groups."}]
        result = validate_answer("It is a two-sided platform. [C1]", evidence, 50, "end_turn")
        self.assertIn("UNSUPPORTED_PLATFORM_SIDEDNESS", result["failures"])

    def test_exact_evidenced_sidedness_is_allowed(self):
        evidence = [{"label": "C1", "text": "We operate a two-sided platform."}]
        result = validate_answer("It is a two-sided platform. [C1]", evidence, 50, "end_turn")
        self.assertTrue(result["valid"])

    def test_prompt_guard_preserves_open_question(self):
        prompt = "QUESTION\nCompare A and B.\n- Return only the final answer with canonical citations."
        guarded = guarded_prompt(prompt)
        self.assertIn("Compare A and B", guarded)
        self.assertIn("exact characterization appears verbatim", guarded)

    @patch("app.services.retrieval_service.os.getenv")
    def test_missing_database_dsn_fails_closed(self, getenv):
        from app.services.retrieval_service import _dsn
        getenv.return_value = None
        with self.assertRaises(RuntimeError):
            _dsn()

    def test_routing_stats_are_safe_before_preload(self):
        import app.services.retrieval_service as service
        prior = service._ROUTING_METADATA
        try:
            service._ROUTING_METADATA = None
            self.assertEqual(service.routing_catalog_stats()["ready"], False)
        finally:
            service._ROUTING_METADATA = prior


if __name__ == "__main__":
    unittest.main()
