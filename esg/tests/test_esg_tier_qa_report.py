from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "esg" / "scripts"))

import build_esg_tier_qa_report as report_builder  # noqa: E402
import build_esg_manual_section_review as manual_review_builder  # noqa: E402


class TierQAReportTests(unittest.TestCase):
    def test_stale_amzn_review_does_not_attach_after_section_renumbering(self):
        key = ("AMZN", "AMZN-AMAZON.COM INC-2023", "energy__0004")
        self.assertTrue(
            manual_review_builder.review_matches_current_section(
                key,
                {
                    "section_title": (
                        "| Fuel- and Energy-Related Activities | | 4.76 | 4.97 |"
                    )
                },
            )
        )
        self.assertFalse(
            manual_review_builder.review_matches_current_section(
                key,
                {
                    "section_title": (
                        "Carbon Carbon-Free Energy Packaging Waste and Circularity Water"
                    )
                },
            )
        )

    def test_stale_bbwi_review_does_not_attach_to_real_appendix(self):
        key = ("BBWI", "BBWI-BATH & BODY WORKS INC-2023", "appendix__0001")
        self.assertTrue(
            manual_review_builder.review_matches_current_section(
                key, {"section_title": "Appendix 59"}
            )
        )
        self.assertFalse(
            manual_review_builder.review_matches_current_section(
                key, {"section_title": "Appendix"}
            )
        )

    def test_raw_json_cannot_be_named_html(self):
        with self.assertRaisesRegex(ValueError, "must end in .json"):
            report_builder.validate_report_output_paths(
                Path("report.html"), Path("report.md")
            )

    def test_json_and_markdown_outputs_validate(self):
        report = {
            "generated_at": "2026-08-01T00:00:00Z",
            "candidate": {
                "documents": 1,
                "sections": 1,
                "chunks": 1,
                "eligible_chunks": 0,
                "held_sections": 1,
                "held_chunks": 1,
                "held_chunks_eligible": 0,
            },
            "status_totals": {"PASS": 1, "WARN": 0, "FAIL": 0, "SKIP": 0},
            "tiers": [
                {
                    "tier": 1,
                    "name": "Structural invariants",
                    "pass": 1,
                    "warn": 0,
                    "fail": 0,
                    "skip": 0,
                }
            ],
            "hard_failures": [],
            "manual_review": {"rows": 0, "included_failure_chunks": 0},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            json_path = root / "report.json"
            markdown_path = root / "report.md"
            report_builder.write_reports(report, json_path, markdown_path)
            self.assertIsInstance(json.loads(json_path.read_text(encoding="utf-8")), dict)
            self.assertTrue(markdown_path.read_text(encoding="utf-8").startswith("# "))


if __name__ == "__main__":
    unittest.main()
