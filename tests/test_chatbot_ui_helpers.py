import unittest

from app.ui.api_client import ChatbotApiClient, ChatbotApiError
from app.ui.components import coverage_rows, status_message, technical_metrics


class Response:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self.data = data or {}

    def json(self):
        return self.data


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.response

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.response


class UiHelperTests(unittest.TestCase):
    def test_client_posts_bounded_contract(self):
        session = Session(Response(data={"status": "success"}))
        client = ChatbotApiClient("http://localhost:8000/", 10, session)
        result = client.chat("Question", "conversation")
        self.assertEqual(result["status"], "success")
        method, url, kwargs = session.calls[0]
        self.assertEqual((method, url), ("POST", "http://localhost:8000/api/chat"))
        self.assertFalse(kwargs["json"]["show_debug"])
        self.assertEqual(kwargs["timeout"], 10)

    def test_client_hides_server_details_on_error(self):
        session = Session(Response(500, {"message": "Request failed", "request_id": "req-1"}))
        with self.assertRaisesRegex(ChatbotApiError, "req-1"):
            ChatbotApiClient(session=session).health()

    def test_status_coverage_and_metrics(self):
        result = {
            "status": "partial_answer", "request_id": "req-2",
            "requirements": [{"claim_key": "risk", "ticker": "EX", "filing_year": 2025,
                              "required_section_code": "Item_1A", "status": "insufficient_evidence"}],
            "telemetry": {"total_ms": 2000, "retrieval_ms": 1000, "generation_ms": 500},
        }
        self.assertEqual(status_message(result)[0], "warning")
        self.assertEqual(coverage_rows(result)[0]["Coverage"], "Insufficient Evidence")
        self.assertEqual(technical_metrics(result)["Total seconds"], 2.0)


if __name__ == "__main__":
    unittest.main()
