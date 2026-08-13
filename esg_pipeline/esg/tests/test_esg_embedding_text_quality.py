from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "esg" / "src"))

import esg_chunker  # noqa: E402


class WordTokenizer:
    @staticmethod
    def encode(text: str, **_: object) -> list[int]:
        return list(range(len(re.findall(r"\w+|[^\w\s]", text))))


class ESGEmbeddingTextQualityTests(unittest.TestCase):
    def test_markdown_table_and_continuation_content_types(self):
        self.assertEqual(
            esg_chunker.classify_embedding_content("| Metric | 2023 | 2024 |"),
            "table",
        )
        self.assertEqual(
            esg_chunker.classify_embedding_content(
                "A later row with no strong numeric pattern", "| Metric | 2024 |"
            ),
            "table_continuation",
        )

    def test_model_subsection_is_clean_bounded_and_keeps_body_exact(self):
        raw = (
            "This sentence is extracted as a heading but it is really body prose."
            " → Climate Strategy → Scope 1 Emissions → Energy Use"
            " → Water Stewardship"
        )
        metadata = {
            "company_name": "TEST CO",
            "ticker": "TEST",
            "doc_type": "sustainability",
            "report_year": "2024",
            "section_code": "climate",
            "section_title_original": raw,
            "physical_section_title": "Climate",
        }
        body = "The company reduced energy use during the reporting year."
        embedded = esg_chunker.final_embedding_text(metadata, body)
        subsection = next(
            line.removeprefix("Subsection: ")
            for line in embedded.splitlines()
            if line.startswith("Subsection: ")
        )
        self.assertLessEqual(len(subsection), esg_chunker.MAX_MODEL_SUBSECTION_CHARS)
        self.assertEqual(
            subsection, "Climate Strategy → Scope 1 Emissions → Energy Use"
        )
        self.assertEqual(metadata["section_title_original"], raw)
        self.assertTrue(embedded.endswith(body))

    def test_unknown_subsection_falls_back_to_physical_title_then_topic(self):
        self.assertEqual(
            esg_chunker.model_subsection_text(
                {
                    "section_title_original": "unknown",
                    "physical_section_title": "Water Management",
                },
                "Water",
            ),
            "Water Management",
        )
        self.assertEqual(
            esg_chunker.model_subsection_text(
                {"section_title_original": "unknown"}, "Water"
            ),
            "Water",
        )

    def test_weak_table_context_is_rejected_but_real_short_labels_remain(self):
        tokenizer = WordTokenizer()
        for value in ("W 2", "10x", "C0NTENT"):
            with self.subTest(value=value):
                self.assertEqual(
                    esg_chunker._credible_table_context(value, tokenizer), ""
                )
        for value in ("Scope 1", "Water3"):
            with self.subTest(value=value):
                self.assertEqual(
                    esg_chunker._credible_table_context(value, tokenizer), value
                )

    def test_explicit_toc_and_replacement_character_are_held(self):
        toc = """TABLE OF CONTENTS
Climate Strategy ........ 4
Energy .................. 8
Water ................... 12
People .................. 18
Governance .............. 24
"""
        self.assertTrue(esg_chunker.is_explicit_toc_dominant(toc))
        self.assertEqual(
            esg_chunker.retrieval_chunk_exclusion_reason(toc, 200),
            "table_of_contents_or_navigation",
        )
        self.assertEqual(
            esg_chunker.retrieval_chunk_exclusion_reason(
                "A broken extraction contains � text.", 20
            ),
            "unicode_replacement_character",
        )
        self.assertFalse(
            esg_chunker.is_explicit_toc_dominant(
                "Contents of this report are discussed in the following narrative."
            )
        )

    def test_dot_leader_percentage_table_is_not_held_as_navigation(self):
        metrics = """Metric Performance
Percentage of suppliers covered by the chemical assessment........100%
Percentage of suppliers that attended virtual training..............81%
Percentage of suppliers that received training materials...........100%
Percentage of workers who received chemicals training...............98%
"""
        self.assertTrue(esg_chunker.is_dot_leader_metric_table(metrics))
        self.assertEqual(
            esg_chunker.retrieval_chunk_exclusion_reason(metrics, 500),
            "",
        )


if __name__ == "__main__":
    unittest.main()
