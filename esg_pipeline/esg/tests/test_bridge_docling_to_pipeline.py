from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from esg.scripts.bridge_docling_to_pipeline import (
    build_document,
    repeated_unplaced_keys,
)


class RepeatedUnplacedTests(unittest.TestCase):
    def test_counts_distinct_pages_and_normalises_digits(self):
        with tempfile.TemporaryDirectory() as tmp:
            fused_dir = Path(tmp)
            stem = "TEST-2024"
            pages = {
                1: "Ribbon 10\nRibbon 10\nUnique detail",
                2: "Ribbon 11\nOther detail",
                3: "Ribbon 12\nFinal detail",
            }
            for page_no, unplaced in pages.items():
                (fused_dir / f"{stem}_p{page_no}.txt").write_text(
                    f"[1:text]\nBody {page_no}\n\n[unplaced words]\n{unplaced}",
                    encoding="utf-8",
                )

            self.assertEqual(
                repeated_unplaced_keys([1, 2, 3], stem, fused_dir, 3),
                {"ribbon"},
            )

    def test_drops_only_repeated_unplaced_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            fused_dir = Path(tmp)
            stem = "TEST-2024"
            ribbon = "THE BIG PICTURE ENVIRONMENT SOCIAL GOVERNANCE APPENDIX"
            details = {1: "Alpha detail", 2: "Beta detail", 3: "Gamma detail"}
            for page_no in (1, 2, 3):
                body = ribbon if page_no == 1 else f"Body {page_no}"
                (fused_dir / f"{stem}_p{page_no}.txt").write_text(
                    f"[1:text]\n{body}\n\n[unplaced words]\n"
                    f"{ribbon}\n{details[page_no]}",
                    encoding="utf-8",
                )

            cached = {
                "pdf_stem": stem,
                "pages": {str(page_no): [] for page_no in (1, 2, 3)},
            }
            text, rows, missing, _ = build_document(
                cached,
                fused_dir,
                drop_repeated_unplaced=3,
            )

            self.assertEqual(text.count(ribbon), 1)
            self.assertTrue(all(detail in text for detail in details.values()))
            self.assertEqual(len(rows), 3)
            self.assertEqual(missing, 0)


if __name__ == "__main__":
    unittest.main()
