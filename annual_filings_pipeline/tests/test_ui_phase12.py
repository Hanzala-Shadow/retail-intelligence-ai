from pathlib import Path
import unittest

from app.ui.components import citation_rows, coverage_rows, technical_metrics


ROOT = Path(__file__).parents[1]


class UiPhase12Tests(unittest.TestCase):
    def test_citation_contract_is_visible(self):
        rows = citation_rows({"citations": [{
            "label": "C8", "ticker": "CART", "filing_year": 2025,
            "section_code": "Item_1", "accession_number": "accession",
            "source_chunk_id": "chunk-1", "excerpt": "Filing evidence",
        }]})
        self.assertEqual(rows[0]["source"], "CART · 2025 · Item_1")
        self.assertEqual(rows[0]["excerpt"], "Filing evidence")

    def test_requirement_card_contract(self):
        rows = coverage_rows({"requirements": [{
            "claim_key": "business model", "ticker": "CART",
            "filing_year": 2025, "required_section_code": "Item_1",
            "status": "supported",
        }]})
        self.assertTrue(rows[0]["supported"])
        self.assertEqual(rows[0]["scope"], "CART · 2025 · Item_1")

    def test_complete_technical_timing_contract(self):
        metrics = technical_metrics({"telemetry": {
            "database_connect_ms": 15.2, "routing_orchestration_ms": 43.9,
            "retrieval_core_ms": 14498.5, "retrieval_ms": 14558.1,
            "generation_ms": 4358.9, "total_ms": 18919.2,
            "routing_catalog_load_ms": 41880.3,
        }})
        self.assertEqual(metrics["Routing orchestration"], "0.04 s")
        self.assertEqual(metrics["Retrieval core"], "14.50 s")
        self.assertEqual(metrics["End-to-end total"], "18.92 s")
        self.assertEqual(metrics["Routing catalog preload (startup only)"], "41.88 s")

    def test_interface_remains_open_and_professional(self):
        text = (ROOT / "app/ui/pages/1_Annual_Filings_Chat.py").read_text()
        self.assertNotIn('text_input("Company ticker"', text)
        self.assertNotIn('number_input("Filing year"', text)
        self.assertNotIn("Professor", text)
        self.assertIn("one or more companies, filing years, or topics", text)
        self.assertIn('st.container(border=True)', text)


if __name__ == "__main__":
    unittest.main()
