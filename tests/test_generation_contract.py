import hashlib
import unittest

from app.services.citation_validator import validate_answer
from app.services.generation_contract import SYSTEM, SYSTEM_SHA256, complexity, request_prompt
from app.services.generation_service import GenerationService


def row():
    return {
        "question": "Compare Example Corp in 2024 and 2025.",
        "query_type": "temporal_comparison",
        "requirements": [
            {"claim_key": "operations", "ticker": "EX", "filing_year": 2024,
             "required_section_code": "Item_1"},
            {"claim_key": "operations", "ticker": "EX", "filing_year": 2025,
             "required_section_code": "Item_1"},
        ],
        "evidence": [
            {"label": "C1", "ticker": "EX", "filing_year": 2024,
             "section_code": "Item_1", "source_chunk_id": "EX-24-1",
             "text": "The company operated 10 stores."},
            {"label": "C2", "ticker": "EX", "filing_year": 2025,
             "section_code": "Item_1", "source_chunk_id": "EX-25-1",
             "text": "The company operated 12 stores."},
        ],
    }


class FakeBedrock:
    def converse(self, **kwargs):
        self.kwargs = kwargs
        return {
            "output": {"message": {"content": [{"text": "Stores rose from 10 to 12. [C1][C2]"}]}},
            "usage": {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120},
            "stopReason": "end_turn",
        }


class GenerationContractTests(unittest.TestCase):
    def test_exact_recovered_system_identity(self):
        self.assertEqual(len(SYSTEM), 4101)
        self.assertEqual(
            SYSTEM_SHA256,
            "2c2e85546f8d1c66330f393c691abf50dfa233cfcd5546e520c2d66e8b78f050",
        )
        self.assertEqual(SYSTEM_SHA256, hashlib.sha256(SYSTEM.encode()).hexdigest())

    def test_prompt_and_budget_contract(self):
        prompt = request_prompt(row())
        self.assertIn("scope-matching evidence labels=['C1']", prompt)
        self.assertIn("Maximum target length: 320 words", prompt)
        self.assertEqual(complexity(row()), (850, 320))

    def test_sentinel_is_status_not_malformed_citation(self):
        result = validate_answer(
            "The policy cannot be established. [INSUFFICIENT_EVIDENCE]",
            row()["evidence"], 320, "end_turn",
        )
        self.assertTrue(result["valid"])
        self.assertTrue(result["insufficient_evidence"])
        self.assertEqual(result["failures"], [])

    def test_rejects_unavailable_and_malformed_citations(self):
        unavailable = validate_answer("Claim. [C3]", row()["evidence"], 320, "end_turn")
        malformed = validate_answer("Claim. [C1, C2]", row()["evidence"], 320, "end_turn")
        self.assertIn("UNAVAILABLE_CITATION", unavailable["failures"])
        self.assertIn("MALFORMED_CITATION", malformed["failures"])

    def test_bedrock_request_is_pinned(self):
        client = FakeBedrock()
        result = GenerationService(client=client).generate(row())
        self.assertTrue(result["validation"]["valid"])
        self.assertEqual(client.kwargs["modelId"], "deepseek.v3.2")
        self.assertEqual(client.kwargs["inferenceConfig"], {"maxTokens": 850, "temperature": 0})


if __name__ == "__main__":
    unittest.main()
