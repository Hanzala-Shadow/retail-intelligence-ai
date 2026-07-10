from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import esg_chunker
import esg_pipeline_qa


class ESGRAGQualityTests(unittest.TestCase):
    def test_report_year_filter_uses_matching_pdf_only(self) -> None:
        tracker = {"ticker": "ETSY", "report_year": "2024"}
        parse_rows = [
            {"ticker": "ETSY", "pdf_file": "ETSY-Etsy-2021.pdf", "source_pdf": "data/ETSY-Etsy-2021.pdf"},
            {"ticker": "ETSY", "pdf_file": "ETSY-Etsy-2024.pdf", "source_pdf": "data/ETSY-Etsy-2024.pdf"},
        ]

        matched = esg_pipeline_qa.filter_parse_rows_for_tracker(tracker, parse_rows)

        self.assertEqual([row["pdf_file"] for row in matched], ["ETSY-Etsy-2024.pdf"])

    def test_wrong_doc_type_excludes_from_esg_rag(self) -> None:
        parse_row = {
            "status": "parsed",
            "quality_flags": "possible_10k",
            "possible_wrong_doc_type": "true",
        }

        self.assertEqual(
            esg_chunker.doc_quality_status(parse_row),
            "exclude_from_esg_rag",
        )
        self.assertEqual(
            esg_chunker.rag_action_for_status("exclude_from_esg_rag"),
            "exclude_from_esg_index",
        )
        self.assertEqual(
            esg_chunker.doc_type_for_parse_row(parse_row),
            "annual_report_with_esg",
        )

    def test_normalized_chunk_span_lookup_tolerates_whitespace(self) -> None:
        source = "Climate goals include Scope 1,\nScope 2, and Scope 3 emissions."
        needle = "Scope 1, Scope 2, and Scope 3"

        start, end = esg_chunker.locate_text_span(source, needle)

        self.assertIsNotNone(start)
        self.assertIsNotNone(end)
        self.assertIn("Scope 1", source[start:end])
        self.assertIn("Scope 3", source[start:end])


if __name__ == "__main__":
    unittest.main()
