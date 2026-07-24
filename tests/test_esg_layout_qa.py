from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import esg_layout_qa
import esg_pipeline_qa
from esg_reading_order import ReadingOrderResult


def word(text: str, x0: float, top: float) -> dict:
    return {"text": text, "x0": x0, "x1": x0 + 20, "top": top, "bottom": top + 8}


class ESGLayoutQATests(unittest.TestCase):
    def test_verified_markdown_table_ignores_rotated_decoration(self) -> None:
        current = (
            "| GRI indicator | Description | Reported |\n"
            "| --- | --- | --- |\n"
            "| G4-28 | Reporting period | FY2015 |\n"
            "| G4-29 | Reporting cycle | Annually |"
        )
        source_tokens = (
            "GRI indicator Description Reported G4-28 Reporting period FY2015 "
            "G4-29 Reporting cycle Annually"
        ).split()
        words = [word(token, 40 + index * 5, 80) for index, token in enumerate(source_tokens)]
        rotated = word("tropeR", 760, 200)
        rotated["upright"] = False
        words.append(rotated)

        decision, reason, metrics = esg_layout_qa.table_extraction_decision(
            {
                "repair_method": "pymupdf_table_aware_xy_cut_order",
                "table_candidate_count": "1",
            },
            current,
            words,
        )

        self.assertEqual(decision, esg_layout_qa.AUTO_PASS_VERIFIED_TABLE)
        self.assertIn("rows=2", reason)
        self.assertEqual(metrics["table_token_recall"], "1.000000")

    def test_malformed_parser_table_fails_closed(self) -> None:
        decision, reason, _ = esg_layout_qa.table_extraction_decision(
            {
                "repair_method": "pymupdf_table_aware_xy_cut_order",
                "table_candidate_count": "1",
            },
            "GRI indicator Description Reported without markdown rows",
            [word("GRI", 40, 80), word("indicator", 80, 80)],
        )

        self.assertEqual(decision, esg_layout_qa.AUTO_HOLD)
        self.assertEqual(reason, "auto_hold_table_extraction_invalid_markdown_shape")

    def test_table_method_without_candidate_fails_closed(self) -> None:
        decision, reason, _ = esg_layout_qa.table_extraction_decision(
            {
                "repair_method": "pymupdf_table_aware_xy_cut_order",
                "table_candidate_count": "0",
            },
            "| A | B |\n| --- | --- |\n| one | two |\n| three | four |",
            [word("A", 40, 80), word("B", 80, 80)],
        )

        self.assertEqual(decision, esg_layout_qa.AUTO_HOLD)
        self.assertEqual(reason, "auto_hold_table_extraction_missing_candidate")

    def test_unextracted_table_candidate_fails_closed(self) -> None:
        decision, reason, _ = esg_layout_qa.table_extraction_decision(
            {"repair_method": "none", "table_candidate_count": "1"},
            "G4-28 Reporting period FY2015 G4-29 Reporting cycle Annually",
            [word("G4-28", 40, 80), word("Reporting", 80, 80)],
        )

        self.assertEqual(decision, esg_layout_qa.AUTO_HOLD)
        self.assertEqual(reason, "auto_hold_table_candidate_not_extracted")

    def test_two_column_metrics_detect_structural_candidate(self) -> None:
        words = []
        for index in range(35):
            words.append(word(f"left{index}", 60, 20 + index * 10))
            words.append(word(f"right{index}", 620, 20 + index * 10))

        metrics = esg_layout_qa._column_metrics(words, page_width=800, visual_object_count=0)

        self.assertTrue(metrics["two_column_candidate"])
        self.assertGreaterEqual(metrics["mixed_column_lines"], 2)

    def test_single_column_page_auto_passes(self) -> None:
        words = [word(f"body{index}", 60, 20 + index * 10) for index in range(80)]
        metrics = esg_layout_qa._column_metrics(words, page_width=800, visual_object_count=0)

        decision, _, _, _ = esg_layout_qa.automatic_decision(
            metrics,
            "Readable climate strategy evidence " * 20,
            "Readable climate strategy evidence " * 20,
            "",
        )

        self.assertEqual(decision, esg_layout_qa.AUTO_PASS)

    def test_full_width_prose_is_not_mistaken_for_columns(self) -> None:
        words = []
        for index in range(20):
            words.append(word(f"left{index}", 60, 20 + index * 10))
            words.append(word(f"right{index}", 620, 20 + index * 10))
        metrics = esg_layout_qa._column_metrics(words, page_width=800, visual_object_count=0)

        decision, reason, _, _ = esg_layout_qa.automatic_decision(
            metrics,
            "Readable climate strategy evidence " * 20,
            "Readable climate strategy evidence " * 20,
            "",
        )

        self.assertEqual(decision, esg_layout_qa.AUTO_PASS)
        self.assertEqual(reason, "auto_pass_no_unresolved_layout_signal")

    def test_ambiguous_multi_column_page_is_automatically_held(self) -> None:
        words = []
        for index in range(35):
            words.append(word(f"left{index}", 60, 20 + index * 10))
            words.append(word(f"right{index}", 620, 20 + index * 10))
        metrics = esg_layout_qa._column_metrics(words, page_width=800, visual_object_count=0)

        decision, reason, _, _ = esg_layout_qa.automatic_decision(
            metrics,
            "PDFium recovered detailed emissions disclosure " * 30,
            "Sparse native text",
            "PDFium recovered detailed emissions disclosure " * 30,
        )

        self.assertEqual(decision, esg_layout_qa.AUTO_HOLD)
        self.assertIn("structural_multi_column", reason)

    def test_low_native_text_can_pass_only_with_material_pdfium_recovery(self) -> None:
        metrics = {
            "native_word_count": 100,
            "two_column_candidate": False,
            "mixed_column_lines": 0,
        }
        decision, reason, preference, _ = esg_layout_qa.automatic_decision(
            metrics,
            "",
            "Sparse native text",
            "Recovered ESG evidence about energy emissions and workforce safety. " * 20,
        )

        self.assertEqual(decision, esg_layout_qa.AUTO_PASS_PDFIUM_COVERAGE)
        self.assertEqual(preference, "pdfium")
        self.assertIn("pdfium_coverage", reason)

    def test_coordinate_reconstruction_passes_only_when_live_order_matches(self) -> None:
        result = ReadingOrderResult(
            status="reconstructed",
            text="Left column evidence. Right column evidence.",
            reason="stable_columns_left_to_right",
            column_count=2,
            source_word_count=6,
            reconstructed_word_count=6,
            preservation_ratio=1.0,
        )

        decision, _, matches = esg_layout_qa.reading_order_decision(
            result,
            "Left column evidence. Right column evidence.",
        )
        self.assertEqual(decision, esg_layout_qa.AUTO_PASS_COLUMN_ORDER)
        self.assertTrue(matches)

        decision, reason, matches = esg_layout_qa.reading_order_decision(
            result,
            "Right column evidence. Left column evidence.",
        )
        self.assertEqual(decision, esg_layout_qa.AUTO_HOLD)
        self.assertFalse(matches)
        self.assertIn("not_applied", reason)

    def test_qa_summary_marks_missing_or_stale_audit(self) -> None:
        summary = esg_pipeline_qa.layout_summary_for_doc(
            [],
            [
                {
                    "page_count": "2",
                    "source_sha256": "source",
                    "content_hash": "parsed",
                }
            ],
            [],
        )

        self.assertEqual(summary["status"], "missing_or_stale")

    def test_qa_summary_counts_auto_held_chunk_ranges(self) -> None:
        layout_rows = [
            {
                "page": "1",
                "source_sha256": "source",
                "parsed_text_sha256": "parsed",
                "audit_version": esg_pipeline_qa.LAYOUT_AUDIT_VERSION,
                "decision": "auto_pass",
            },
            {
                "page": "2",
                "source_sha256": "source",
                "parsed_text_sha256": "parsed",
                "audit_version": esg_pipeline_qa.LAYOUT_AUDIT_VERSION,
                "decision": "auto_hold",
            },
        ]
        summary = esg_pipeline_qa.layout_summary_for_doc(
            layout_rows,
            [
                {
                    "page_count": "2",
                    "source_sha256": "source",
                    "content_hash": "parsed",
                }
            ],
            [
                {"page_start": "1", "page_end": "1"},
                {"page_start": "1", "page_end": "2"},
            ],
        )

        self.assertEqual(summary["status"], "auto_hold")
        self.assertEqual(summary["auto_hold_page_count"], 1)
        self.assertEqual(summary["held_chunk_count"], 1)


if __name__ == "__main__":
    unittest.main()
