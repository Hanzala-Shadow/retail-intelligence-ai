from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]


class Phase7ContractTests(unittest.TestCase):
    def test_retriever_is_resident_and_offline(self):
        text = (ROOT / "app/services/retrieval_service.py").read_text()
        self.assertIn("_BI_ENCODER", text)
        self.assertIn("RemoteEmbedder", text)
        self.assertIn("RemoteEmbedder.from_env()", text)
        self.assertNotIn("SentenceTransformer", text)
        self.assertIn("bi_encoder=resident_bi_encoder()", text)

    def test_interface_remains_open_for_comparisons(self):
        text = (ROOT / "app/ui/pages/1_Annual_Filings_Chat.py").read_text()
        self.assertNotIn('text_input("Company ticker"', text)
        self.assertNotIn('number_input("Filing year"', text)
        self.assertIn("api_client().chat(\n                    pending_question,", text)
        self.assertIn('"Technical panel"', text)
        self.assertNotIn("Professor", text)

    def test_encoder_preloads_before_requests(self):
        text = (ROOT / "app/api/main.py").read_text()
        self.assertIn("resident_bi_encoder()", text)
        self.assertIn("lifespan=lifespan", text)

    def test_client_timeout_covers_bounded_analysis(self):
        text = (ROOT / "app/ui/api_client.py").read_text()
        self.assertIn('CHATBOT_UI_TIMEOUT_SECONDS", "600"', text)


if __name__ == "__main__":
    unittest.main()
