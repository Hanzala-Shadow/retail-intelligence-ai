import csv
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
ESG_SRC = REPO_ROOT / "esg" / "src"
if str(ESG_SRC) not in sys.path:
    sys.path.insert(0, str(ESG_SRC))

from esg_compact_toc import detect_compact_toc_entries, has_compact_toc_cluster
import section_splitter_esg


BBWI_STEM = "BBWI-BATH & BODY WORKS INC-2023"
BBWI_TEXT = REPO_ROOT / "data" / "02_interim" / "esg_text" / "BBWI" / f"{BBWI_STEM}.txt"


class CompactTocUnitTests(unittest.TestCase):
    def test_requires_cluster_and_contents_context(self) -> None:
        self.assertEqual(detect_compact_toc_entries(["Scope 3"]), ())
        self.assertEqual(
            detect_compact_toc_entries(
                ["Scope 3", "Climate 8", "People 12", "Appendix 59"]
            ),
            (),
        )

    def test_detects_short_ordered_titles_with_page_numbers(self) -> None:
        lines = [
            "Table of Contents",
            "CEO Message 3",
            "Welcome 4",
            "Engaged People 14",
            "Governance 54",
            "Appendix 59",
        ]
        entries = detect_compact_toc_entries(lines, max_page_number=80)
        self.assertEqual([entry.page_number for entry in entries], [3, 4, 14, 54, 59])
        self.assertTrue(has_compact_toc_cluster("\n".join(lines), max_page_number=80))

    def test_isolated_scope_3_remains_a_heading(self) -> None:
        text = "Scope 3\nWe report value-chain emissions and reduction work in this section."
        titles = [
            candidate.title
            for candidate in section_splitter_esg.collect_heading_candidates(text)
        ]
        self.assertIn("Scope 3", titles)


@unittest.skipUnless(BBWI_TEXT.exists(), "BBWI parsed-text fixture is unavailable")
class BbwiCompactTocRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = BBWI_TEXT.read_text(encoding="utf-8")
        with BBWI_TEXT.with_suffix(".pages.csv").open(newline="", encoding="utf-8") as handle:
            cls.pages = list(csv.DictReader(handle))
        cls.candidates = section_splitter_esg.collect_heading_candidates(cls.text, cls.pages)
        cls.sections = section_splitter_esg.split_esg_sections(cls.text, cls.pages)

    @classmethod
    def page_for_offset(cls, offset: int) -> int:
        return next(
            int(row["page"])
            for row in cls.pages
            if int(row["char_start"]) <= offset < int(row["char_end"])
        )

    def test_page_2_toc_entries_are_not_headings(self) -> None:
        page_2_titles = {
            candidate.title
            for candidate in self.candidates
            if self.page_for_offset(candidate.char_offset) == 2
        }
        self.assertEqual(page_2_titles, {"Table of Contents"})
        for title in (
            "A Message From Our Chief Executive",
            "Engaged People 14",
            "Thoughtful Products 40 Board (SASB) Standards and the Task",
            "Governance 54",
            "Appendix 59",
        ):
            self.assertNotIn(title, page_2_titles)

    def test_real_appendix_and_front_matter_sections_are_preserved(self) -> None:
        real_appendix = [
            candidate
            for candidate in self.candidates
            if candidate.title == "Appendix"
            and self.page_for_offset(candidate.char_offset) == 59
        ]
        self.assertEqual(len(real_appendix), 1)

        page_3 = [
            section
            for section in self.sections
            if section.source_start_char is not None
            and self.page_for_offset(section.source_start_char) == 3
        ]
        self.assertTrue(any(section.section_code == "ceo_letter" for section in page_3))
        page_4 = [
            section
            for section in self.sections
            if section.source_start_char is not None
            and self.page_for_offset(section.source_start_char) == 4
        ]
        self.assertTrue(
            any(
                section.section_code == "about_this_report"
                and section.title == "Welcome To Bath & Body Works"
                for section in page_4
            )
        )
        self.assertFalse(
            any(
                section.section_code == "appendix"
                and section.source_start_char is not None
                and self.page_for_offset(section.source_start_char) in {2, 3, 4, 5}
                for section in self.sections
            )
        )

    def test_source_spans_tile_without_content_gaps(self) -> None:
        cursor = 0
        for section in self.sections:
            self.assertIsNotNone(section.source_start_char)
            self.assertIsNotNone(section.source_end_char)
            start = int(section.source_start_char)
            end = int(section.source_end_char)
            self.assertFalse(self.text[cursor:start].strip())
            self.assertEqual(self.text[start:end], section.text)
            cursor = end
        self.assertFalse(self.text[cursor:].strip())


if __name__ == "__main__":
    unittest.main()
