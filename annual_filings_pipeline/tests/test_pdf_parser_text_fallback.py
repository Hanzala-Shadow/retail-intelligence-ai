from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import pdf_parser


class PDFParserTextFallbackTests(unittest.TestCase):
    def test_adequate_simple_extraction_does_not_try_fallback(self) -> None:
        pages = [(page, "Readable ESG report text. " * 20) for page in range(1, 6)]

        self.assertFalse(pdf_parser.should_try_text_layer_fallback(pages, page_count=5))

    def test_low_character_density_tries_fallback(self) -> None:
        pages = [(page, "Sparse text") for page in range(1, 6)]

        self.assertTrue(pdf_parser.should_try_text_layer_fallback(pages, page_count=10))

    def test_low_page_coverage_tries_fallback(self) -> None:
        pages = [(1, "Readable ESG report text. " * 100)]

        self.assertTrue(pdf_parser.should_try_text_layer_fallback(pages, page_count=10))

    def test_cid_artifacts_try_fallback(self) -> None:
        pages = [(page, "Metric value (cid:20)(cid:23)7 text. " * 20) for page in range(1, 6)]

        self.assertTrue(pdf_parser.should_try_text_layer_fallback(pages, page_count=5))

    def test_materially_better_fallback_is_used(self) -> None:
        simple_pages = [(1, "Sparse text")]
        fallback_pages = [
            (page, "Detailed sustainability report text. " * 100)
            for page in range(1, 11)
        ]

        self.assertTrue(
            pdf_parser.should_use_text_layer_fallback(
                simple_pages,
                fallback_pages,
                page_count=10,
            )
        )

    def test_cid_cleanup_fallback_is_used(self) -> None:
        simple_pages = [(1, "FY21 value (cid:20)(cid:23)7 and readable context.")]
        fallback_pages = [(1, "FY21 value 147 and readable context.")]

        self.assertTrue(
            pdf_parser.should_use_text_layer_fallback(
                simple_pages,
                fallback_pages,
                page_count=1,
            )
        )

    def test_smaller_fallback_is_not_used(self) -> None:
        simple_pages = [(page, "Readable ESG report text. " * 100) for page in range(1, 6)]
        fallback_pages = [(1, "Sparse text")]

        self.assertFalse(
            pdf_parser.should_use_text_layer_fallback(
                simple_pages,
                fallback_pages,
                page_count=5,
            )
        )

    def test_visual_metric_grid_triggers_layout_fallback(self) -> None:
        metrics = {
            "word_count": 190,
            "short_lines": 35,
            "metric_lines": 3,
            "huge_gap_lines": 9,
            "common_start_count": 4,
            "visual_objects": 25,
        }

        self.assertTrue(pdf_parser.layout_grid_risk_from_metrics(metrics))

    def test_plain_two_column_prose_does_not_trigger_layout_fallback(self) -> None:
        metrics = {
            "word_count": 650,
            "short_lines": 7,
            "metric_lines": 0,
            "huge_gap_lines": 5,
            "common_start_count": 2,
            "visual_objects": 5,
        }

        self.assertFalse(pdf_parser.layout_grid_risk_from_metrics(metrics))

    def test_metric_text_without_visual_structure_is_not_a_layout_risk(self) -> None:
        metrics = {
            "word_count": 190,
            "short_lines": 35,
            "metric_lines": 3,
            "huge_gap_lines": 9,
            "common_start_count": 4,
            "visual_objects": 0,
        }

        self.assertFalse(pdf_parser.layout_grid_risk_from_metrics(metrics))

    def test_report_only_layout_mode_reprocesses_legacy_layout_rows(self) -> None:
        row = {"parser_policy": "auto_layout_grid_fallback"}

        self.assertFalse(
            pdf_parser._parser_policy_matches_request(
                row,
                expected_parser_policy="",
                auto_layout_pdfium=False,
            )
        )
        self.assertTrue(
            pdf_parser._parser_policy_matches_request(
                row,
                expected_parser_policy="",
                auto_layout_pdfium=True,
            )
        )

    def test_report_only_layout_mode_keeps_text_quality_fallback_rows(self) -> None:
        row = {"parser_policy": "auto_text_layer_fallback"}

        self.assertTrue(
            pdf_parser._parser_policy_matches_request(
                row,
                expected_parser_policy="",
                auto_layout_pdfium=False,
            )
        )

    def test_resume_reprocesses_rows_when_an_explicit_override_is_removed(self) -> None:
        row = {"parser_policy": "override_pdfium"}

        self.assertFalse(
            pdf_parser._parser_policy_matches_request(
                row,
                expected_parser_policy="",
                auto_layout_pdfium=False,
            )
        )

    def test_parser_override_forces_pdfium(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            overrides = Path(tmp_dir) / "overrides.csv"
            overrides.write_text(
                "ticker,pdf_file,parser_mode,reason,active\n"
                "AMZN,AMZN-Amazon-2021.pdf,pypdfium,known_grid_issue,true\n",
                encoding="utf-8",
            )

            loaded = pdf_parser.load_parser_overrides(overrides)
            request = pdf_parser.parser_request_for_pdf(
                ticker="AMZN",
                pdf=Path("AMZN-Amazon-2021.pdf"),
                overrides=loaded,
                prefer_pdfium=False,
            )

        self.assertTrue(request["prefer_pdfium"])
        self.assertEqual(request["parser_policy"], "override_pdfium")
        self.assertEqual(request["parser_reason"], "known_grid_issue")

    def test_default_request_uses_coordinate_reading_order_policy(self) -> None:
        request = pdf_parser.parser_request_for_pdf(
            ticker="TEST",
            pdf=Path("TEST-Report-2024.pdf"),
            overrides={},
            prefer_pdfium=False,
        )

        self.assertFalse(request["prefer_pdfium"])
        self.assertEqual(
            request["parser_policy"],
            pdf_parser.AUTO_PDFPLUMBER_COLUMN_POLICY,
        )

    def test_new_coordinate_policy_reprocesses_old_default_row(self) -> None:
        self.assertFalse(
            pdf_parser._parser_policy_matches_request(
                {"parser_policy": "auto_pdfplumber"},
                expected_parser_policy=pdf_parser.AUTO_PDFPLUMBER_COLUMN_POLICY,
                auto_layout_pdfium=False,
            )
        )
        self.assertTrue(
            pdf_parser._parser_policy_matches_request(
                {"parser_policy": pdf_parser.AUTO_PDFPLUMBER_COLUMN_POLICY},
                expected_parser_policy=pdf_parser.AUTO_PDFPLUMBER_COLUMN_POLICY,
                auto_layout_pdfium=False,
            )
        )


if __name__ == "__main__":
    unittest.main()
