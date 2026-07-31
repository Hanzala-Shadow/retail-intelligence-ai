from __future__ import annotations

import unittest

import evaluate_esg_parser_ai_gold as evaluator


class ParserGoldMetricTests(unittest.TestCase):
    def test_number_tokens_keep_currency_sign_and_percent(self):
        text = "$1,200 €3 £4 −5 –6 —7 8%"

        self.assertEqual(
            evaluator.number_tokens(text),
            ["$1200", "€3", "£4", "-5", "-6", "-7", "8%"],
        )

    def test_sequence_metric_rejects_scrambled_blocks(self):
        reference = evaluator.tokens("alpha one beta two gamma three delta four")
        candidate = evaluator.tokens("gamma three delta four alpha one beta two")

        recall, _precision, _f1 = evaluator.sequence_metrics(reference, candidate)

        self.assertLess(recall, 0.85)

    def test_table_row_order_handles_repeated_labels(self):
        reference = """| Metric | Value |
| --- | --- |
| Total | 10 |
| Total | 20 |
| Other | 30 |"""
        candidate = evaluator.tokens("Metric Value Total 10 Total 20 Other 30")

        row_count, score = evaluator.table_row_order(reference, candidate)

        self.assertEqual(row_count, 4)
        self.assertEqual(score, 1.0)

    def test_table_row_order_detects_reordered_distinct_rows(self):
        reference = """| Metric | Value |
| --- | --- |
| Alpha | 10 |
| Beta | 20 |
| Gamma | 30 |"""
        candidate = evaluator.tokens("Metric Value Gamma 30 Alpha 10 Beta 20")

        _row_count, score = evaluator.table_row_order(reference, candidate)

        self.assertLess(score, 1.0)


class ParserGoldDecisionTests(unittest.TestCase):
    @staticmethod
    def _gold(reference_use: str = "content") -> dict:
        return {
            "item_id": "gold_test",
            "ticker": "TEST",
            "pdf_file": "TEST-2024.pdf",
            "page": 1,
            "split": "development",
            "sample_category": "clean_control",
            "page_type": "mixed",
            "canonical_order": "top_to_bottom",
            "reference_use": reference_use,
            "review_status": "accepted",
            "confidence": "high",
            "reference_markdown": "Alpha beta gamma 10%",
            "source_sha256": "source",
            "image_sha256": "image",
        }

    @staticmethod
    def _parser(text: str = "Alpha beta gamma 10%") -> dict:
        return {
            "parser_text": text,
            "parser_used": "pdfplumber",
            "parser_policy": "test",
            "parser_page_text_sha256": "parser",
        }

    def test_navigation_page_fails_when_pipeline_passes_it(self):
        row = evaluator.score_page(
            self._gold("exclude_navigation"),
            self._parser(),
            {"current_layout_decision": "auto_pass_navigation_contents"},
        )

        self.assertEqual(row["outcome"], "fail_navigation_not_excluded")
        self.assertFalse(row["embedding_safe"])

    def test_navigation_page_is_safe_when_pipeline_holds_it(self):
        row = evaluator.score_page(
            self._gold("exclude_navigation"),
            self._parser(),
            {"current_layout_decision": "auto_hold"},
        )

        self.assertEqual(row["outcome"], "excluded_as_expected")
        self.assertTrue(row["embedding_safe"])


if __name__ == "__main__":
    unittest.main()
