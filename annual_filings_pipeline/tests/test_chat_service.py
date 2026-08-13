import unittest

from app.services.chat_service import ChatService


def retrieval(_question, _request_id):
    subqueries = [
        {"subquery_id": "sq1", "claim_key": "operations", "ticker": "EX",
         "filing_year": 2025, "section_code": "Item_1"}
    ]
    evidence = [
        {"ticker": "EX", "filing_year": 2025, "section_code": "Item_1",
         "accession_number": "0001", "source_chunk_id": f"EX-{i}",
         "chunk_text": f"Evidence text {i}", "final_rank": i}
        for i in range(1, 17)
    ]
    return {
        "status": "success", "query_type": "single_source",
        "subqueries": subqueries, "evidence": evidence,
        "requirement_coverage": [{"subquery_id": "sq1", "retrieval_status": "represented"}],
        "runtime_profile": {"timings_ms": {"total": 13000}},
    }


class Generator:
    def __init__(self, answer="Supported claim. [C1]"):
        self.answer_text = answer

    def generate(self, _row):
        insufficient = "[INSUFFICIENT_EVIDENCE]" in self.answer_text
        return {
            "answer": self.answer_text, "model_id": "deepseek.v3.2",
            "generation_ms": 1000, "input_tokens": 100, "output_tokens": 20,
            "estimated_cost_usd": 0.000118,
            "validation": {"valid": True, "failures": [], "citations": ["[C1]"] if not insufficient else [],
                           "insufficient_evidence": insufficient, "word_count": 3, "word_budget": 220},
        }


class ChatServiceTests(unittest.TestCase):
    def test_success_response_and_citation_metadata(self):
        result = ChatService(retrieval, Generator()).answer("Question?", "req-1")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["telemetry"]["evidence_count"], 16)
        self.assertEqual(result["citations"][0]["label"], "C1")
        self.assertNotIn("chunk_text", result["citations"][0])

    def test_insufficiency_becomes_partial_answer(self):
        result = ChatService(
            retrieval, Generator("Cannot establish it. [INSUFFICIENT_EVIDENCE]")
        ).answer("Question?", "req-2")
        self.assertEqual(result["status"], "partial_answer")
        self.assertTrue(result["limitations"])


if __name__ == "__main__":
    unittest.main()
