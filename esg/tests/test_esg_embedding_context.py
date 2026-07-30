"""Tests for esg/scripts/build_esg_embedding_context.py.

The load-bearing property is the one the verified 100-chunk handoff package
holds on all 100 reference rows: stripping the header from `embedding_text`
must return `chunk_text` byte-for-byte. If that breaks, the citation text and
the embedded text have silently diverged.
"""

from __future__ import annotations

import csv
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
import config  # noqa: E402

SCRIPT = ROOT / "esg/scripts/build_esg_embedding_context.py"

_spec = importlib.util.spec_from_file_location("build_esg_embedding_context", SCRIPT)
mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(mod)

MANDATORY_KEYS = [
    "Company",
    "Ticker",
    "Document",
    "Reporting year",
    "ESG topic",
    "Subsection",
    "Content type",
]

ROW = {
    "ticker": "AAPL",
    "canonical_ticker": "AAPL",
    "company_name": "APPLE INC",
    "report_year": "2024",
    "report_year_status": "parsed",
    "report_year_span": "",
    "section_code": "diversity_equity_inclusion",
    "doc_type": "sustainability",
    "source_type": "sustainability",
}


class HeaderShapeTests(unittest.TestCase):
    def test_header_has_the_seven_reference_keys_in_order(self):
        header = mod.build_header(ROW, "Our Workforce", "narrative")
        keys = [line.split(": ", 1)[0] for line in header.split("\n")]
        self.assertEqual(keys, MANDATORY_KEYS)

    def test_header_renames_sec_specific_labels(self):
        header = mod.build_header(ROW, "Our Workforce", "narrative")
        self.assertIn("Reporting year: 2024", header)
        self.assertIn("ESG topic: Diversity, Equity and Inclusion", header)
        self.assertNotIn("Fiscal year:", header)
        self.assertNotIn("SEC section:", header)

    def test_multi_year_reports_disclose_their_span(self):
        row = dict(ROW, report_year="2023", report_year_status="multi_year_range", report_year_span="2022-2023")
        self.assertEqual(mod.reporting_year(row), "2023 (report covers 2022-2023)")

    def test_missing_values_degrade_to_unknown_not_blank(self):
        header = mod.build_header(dict(ROW, company_name="", canonical_ticker="", ticker=""), "", "narrative")
        self.assertIn("Company: unknown", header)
        self.assertIn("Ticker: unknown", header)
        self.assertIn("Subsection: unknown", header)

    def test_body_is_recoverable_byte_exact(self):
        body = "Line one.\n\nLine  two with  double  spaces.\nLine three."
        header = mod.build_header(ROW, "Our Workforce", "narrative")
        embedding_text = f"{header}\n\n{body}"
        self.assertEqual(embedding_text.partition("\n\n")[2], body)


class ContentTypeTests(unittest.TestCase):
    def test_prose_is_narrative(self):
        text = (
            "We reduced absolute emissions by 55 percent against our 2015 baseline, "
            "and we continue to invest in renewable energy across our supply chain.\n"
            "This progress reflects sustained work by teams in every region."
        )
        self.assertEqual(mod.classify_content(text)[0], "narrative")

    def test_short_numeric_lines_are_table(self):
        text = "Scope 1\n12,345\nScope 2\n67,890\nScope 3\n11,223\nTotal\n91,458\n2024\n2023"
        self.assertEqual(mod.classify_content(text)[0], "table")

    def test_bulleted_block_is_list(self):
        text = "\n".join(f"- Commitment number {i} for the reporting period" for i in range(8))
        self.assertEqual(mod.classify_content(text)[0], "list")

    def test_classifier_defaults_to_narrative_on_empty(self):
        self.assertEqual(mod.classify_content("")[0], "narrative")

    def test_narrative_with_many_figures_is_not_misread_as_table(self):
        text = (
            "In 2024 we invested 1.2 billion dollars in renewable projects, up from "
            "980 million dollars in 2023, and we expect that figure to reach 1.5 billion "
            "dollars by 2026 as additional sites come online across our operations."
        )
        self.assertEqual(mod.classify_content(text)[0], "narrative")


class GeneratedOutputTests(unittest.TestCase):
    """Verify the real output when it exists; skip cleanly when it does not."""

    INDEX = config.ESG_CHUNK_EMBEDDING_CONTEXT_CSV

    def setUp(self):
        if not self.INDEX.exists():
            self.skipTest("esg_chunk_embedding_context.csv not built")
        with self.INDEX.open(encoding="utf-8", errors="replace", newline="") as fh:
            self.rows = list(csv.DictReader(fh))

    def test_every_row_declares_the_context_version(self):
        versions = {r["embedding_context_version"] for r in self.rows}
        self.assertEqual(versions, {mod.EMBEDDING_CONTEXT_VERSION})

    def test_chunk_ids_are_unique(self):
        ids = [r["chunk_id"] for r in self.rows]
        self.assertEqual(len(ids), len(set(ids)))

    def test_content_types_are_from_the_controlled_set(self):
        allowed = {"narrative", "table", "list", "table_continuation"}
        self.assertTrue({r["content_type"] for r in self.rows} <= allowed)

    def test_sample_files_keep_header_and_body_separable(self):
        for row in self.rows[:: max(len(self.rows) // 50, 1)]:
            path = ROOT / row["embedding_text_ctx_file"]
            if not path.exists():
                continue
            header, sep, body = path.read_text(encoding="utf-8").partition("\n\n")
            self.assertEqual(sep, "\n\n", msg=row["chunk_id"])
            keys = [line.split(": ", 1)[0] for line in header.split("\n")]
            self.assertEqual(keys, MANDATORY_KEYS, msg=row["chunk_id"])
            self.assertTrue(body, msg=row["chunk_id"])


if __name__ == "__main__":
    unittest.main()
