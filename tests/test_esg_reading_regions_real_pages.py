"""Real-page regression tests for the default-off region reading-order candidate.

These assert the **order of the reconstructed text**, not region counts. A
region count says nothing about whether a page reads correctly: BBWI-2023 p22
scored the "right" five regions while emitting the infographic, the awards panel
and the pull-quote a slice at a time, mid-sentence. Every check here is phrased
as landmarks that must appear in a given order, or as a block of landmarks that
no other block may appear inside -- which is what "this page reads correctly"
actually means, and what the earlier bbox/count assertions could not see.

Landmarks are chosen to be ASCII-safe: the extracted text carries replacement
characters where the source PDFs use typographic apostrophes and dashes, so no
landmark spans one.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import esg_reading_regions as regions_mod

BBY_2024 = "BBY-BEST BUY CO INC-2024.pdf"
BBWI_2023 = "BBWI-BATH & BODY WORKS INC-2023.pdf"
BBW_2023 = "BBW-BUILD-A-BEAR WORKSHOP INC-2023.pdf"
AMZN_2023 = "AMZN-AMAZON.COM INC-2023.pdf"
AEO_2023 = "AEO-AMERICAN EAGLE OUTFITTERS INC-2023.pdf"
AEO_2024 = "AEO-AMERICAN EAGLE OUTFITTERS INC-2024.pdf"
AAPL_2024 = "AAPL-APPLE INC-2024.pdf"


def make_word(text: str, x0: float, top: float) -> dict:
    return {"text": text, "x0": x0, "x1": x0 + 14, "top": top, "bottom": top + 8, "upright": True}


_BUNDLE: dict[tuple[str, int, str], dict] | None = None


def bundle_rows() -> dict[tuple[str, int, str], dict]:
    """Run the pilot once for the whole module.

    Every test class below reads the same 12 pages. Building the navigation
    profile means walking each source report end to end, so running the bundle
    per class turns a seconds-long suite into a minutes-long one.
    """

    global _BUNDLE
    if _BUNDLE is None:
        script = REPO_ROOT / "scripts" / "run_reading_order_pilot.py"
        spec = importlib.util.spec_from_file_location("bundle2_runner", script)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _BUNDLE = {(row["ticker"], row["page"], row["pdf_file"]): row for row in module.run_bundle()}
    return _BUNDLE


class RealPageBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = bundle_rows()

    def row(self, ticker: str, page: int, filename: str) -> dict:
        return self.rows[(ticker, page, filename)]

    def flat(self, row: dict) -> str:
        """The candidate text as one whitespace-normalised, casefolded line.

        Region joins collapse to single spaces, so a landmark that ends one
        region and one that starts the next are directly comparable by position.
        """

        return " ".join(row["candidate_text"].split()).casefold()

    def positions(self, row: dict, landmarks: tuple[str, ...]) -> list[int]:
        text = self.flat(row)
        found = []
        for landmark in landmarks:
            position = text.find(landmark.casefold())
            self.assertGreaterEqual(
                position, 0, f"{row['ticker']} p{row['page']}: landmark not in output: {landmark!r}"
            )
            found.append(position)
        return found

    def assert_reads_in_order(self, row: dict, *landmarks: str) -> None:
        found = self.positions(row, landmarks)
        for (earlier, first), (later, second) in zip(zip(landmarks, found), zip(landmarks[1:], found[1:])):
            self.assertLess(
                first,
                second,
                f"{row['ticker']} p{row['page']}: {earlier!r} must be read before {later!r}",
            )

    def assert_block_uninterrupted(self, row: dict, block: tuple[str, ...], foreign: tuple[str, ...]) -> None:
        """No `foreign` landmark may fall inside the span `block` occupies.

        This is the interleaving check. Ordering alone cannot catch a panel that
        is emitted in two pieces with another panel's text wedged between them,
        because each piece can still be in the right relative order.
        """

        block_positions = self.positions(row, block)
        start, end = min(block_positions), max(block_positions)
        for landmark, position in zip(foreign, self.positions(row, foreign)):
            self.assertFalse(
                start < position < end,
                f"{row['ticker']} p{row['page']}: {landmark!r} was emitted inside the block "
                f"spanning {block[0]!r}..{block[-1]!r}",
            )


class WordPreservationTests(RealPageBase):
    def test_every_page_preserves_each_cleaned_body_word_exactly_once(self) -> None:
        self.assertEqual(len(self.rows), 12)
        for row in self.rows.values():
            self.assertTrue(row["word_preservation_passed"], f"{row['ticker']} p{row['page']}")
            self.assertEqual(row["preservation_ratio"], 1.0, f"{row['ticker']} p{row['page']}")


class FourColumnBodyTests(RealPageBase):
    """BBY-2024 p46: a full-width heading and introduction over a 4-column body."""

    COLUMN_ONE_END = "protect our organization from attempted"
    COLUMN_TWO_START = "attacks. our cybersecurity operations"

    def test_each_of_the_four_columns_is_read_whole_before_the_next(self) -> None:
        row = self.row("BBY", 46, BBY_2024)
        self.assert_reads_in_order(
            row,
            "Cybersecurity and privacy",
            "Securing customer information",
            "that trust through our cybersecurity and privacy practices.",
            # column 1, first line to last
            "We recognize the importance of ensuring",
            self.COLUMN_ONE_END,
            # column 2
            self.COLUMN_TWO_START,
            "highlighting emerging threats.",
            # column 3
            "All employees participate in Best Buy",
            "bad actors. To help keep our customers and",
            # column 4
            "infrastructure safe, we are proud to",
            "electronics purchases.",
        )

    def test_the_sentence_broken_across_the_first_column_break_is_rejoined(self) -> None:
        """Column 1 ends mid-sentence ("...from attempted") and column 2 opens
        with the rest of it ("attacks. ..."). Reading one column at a time makes
        those two adjacent in the output; the bug this page was reported for --
        a single line built from all four columns at once -- cannot satisfy this.
        """

        row = self.row("BBY", 46, BBY_2024)
        self.assertIn(f"{self.COLUMN_ONE_END} {self.COLUMN_TWO_START}".casefold(), self.flat(row))

    def test_no_output_line_mixes_all_four_columns(self) -> None:
        row = self.row("BBY", 46, BBY_2024)
        for line in row["candidate_text"].splitlines():
            folded = line.casefold()
            mixed = sum(
                marker in folded
                for marker in ("we recognize the importance", "attacks. our cybersecurity", "all employees participate")
            )
            self.assertLessEqual(mixed, 1, f"line mixes separate columns: {line!r}")


class SideBySidePanelTests(RealPageBase):
    """AEO p8, both years: a page-spanning heading over a progress card (left)
    and a bar chart (right). The card must be complete before the chart starts."""

    def test_progress_card_is_complete_before_the_chart_begins(self) -> None:
        for year, filename in ((2023, AEO_2023), (2024, AEO_2024)):
            with self.subTest(year=year):
                row = self.row("AEO", 8, filename)
                self.assert_reads_in_order(
                    row,
                    "CONTINUED INCREASE IN USE OF SUSTAINABLE RAW MATERIALS",
                    f"{year} PROGRESS",
                    f"{year} ACHIEVED:",
                    "sustainable sources",
                    "2028 GOAL:",
                    "Use sustainable sources for",
                    "75% of all fibers by 2028",
                    "TOTAL PREFERRED FIBERS",
                )

    def test_chart_never_interrupts_the_card(self) -> None:
        """The reported bug alternated between the two panels. On the 2024 page
        it survived a first fix in a new shape -- the card was emitted in two
        pieces with the entire chart between them -- which ordering assertions
        alone still passed, so the card is checked for interruption directly.
        """

        for year, filename in ((2023, AEO_2023), (2024, AEO_2024)):
            with self.subTest(year=year):
                row = self.row("AEO", 8, filename)
                self.assert_block_uninterrupted(
                    row,
                    (f"{year} PROGRESS", f"{year} ACHIEVED:", "2028 GOAL:", "75% of all fibers by 2028"),
                    ("TOTAL PREFERRED FIBERS",),
                )


class MultiPanelPageTests(RealPageBase):
    """BBWI-2023 p22: a heading and intro, a diversity infographic, lower-left
    DEI prose, a centre awards panel, and a right pull-quote that runs alongside
    all of them."""

    HEADING_INTRO = ("Diversity, Equity and Inclusion (DEI)", "WE EMBRACE DIVERSITY", "across our business")
    INFOGRAPHIC = (
        "FAMILY STATUS",
        "SEXUAL ORIENTATION",
        "MILITARY SERVICE",
        "EDUCATION",
        "ETHNICITY",
        "Culture or country of origin",
    )
    DEI_PROSE = (
        "We intentionally weave inclusion and belonging",
        "cultural foundation and supports our corporate",
        "where we live and work.",
    )
    AWARDS = (
        "A Diversity First Top 50 Company",
        "by the Diversity Research Institute",
        "Most Trustworthy Companies in",
        "Index America by Newsweek",
    )
    QUOTE = (
        "We want associates to feel part of something",
        "name tag pins and our Inclusion Resource",
        "Kelie Charles",
        "Chief Diversity Officer",
    )

    def test_the_five_blocks_are_read_in_visual_order(self) -> None:
        row = self.row("BBWI", 22, BBWI_2023)
        self.assert_reads_in_order(
            row,
            self.HEADING_INTRO[1],
            self.HEADING_INTRO[2],
            self.INFOGRAPHIC[0],
            self.INFOGRAPHIC[-1],
            self.DEI_PROSE[0],
            self.DEI_PROSE[-1],
            self.AWARDS[0],
            self.AWARDS[-1],
            self.QUOTE[0],
            self.QUOTE[-1],
        )

    def test_each_block_is_continuous(self) -> None:
        """The pull-quote runs the height of the page alongside content that has
        its own row breaks. Splitting the page into rows first therefore put a
        slice of every panel into each row; each block is checked here against
        every other block's landmarks so that failure mode cannot return.
        """

        row = self.row("BBWI", 22, BBWI_2023)
        blocks = {
            "infographic": self.INFOGRAPHIC,
            "dei_prose": self.DEI_PROSE,
            "awards_panel": self.AWARDS,
            "pull_quote": self.QUOTE,
        }
        for name, block in blocks.items():
            foreign = tuple(
                landmark for other, group in blocks.items() if other != name for landmark in group
            )
            with self.subTest(block=name):
                self.assert_block_uninterrupted(row, block, foreign)

    def test_each_sentence_of_the_prose_block_stays_with_the_next(self) -> None:
        row = self.row("BBWI", 22, BBWI_2023)
        self.assertIn(
            "we intentionally weave inclusion and belonging into our business.", self.flat(row)
        )


class RowStructuredTableTests(RealPageBase):
    """AEO-2023 p3 (control): a five-column goals table. Each goal has to keep
    its own established year, status and progress text -- reading the table down
    its columns instead of across its rows detaches every status from its goal."""

    def test_each_goal_keeps_its_own_status_and_progress(self) -> None:
        row = self.row("AEO", 3, AEO_2023)
        self.assert_reads_in_order(
            row,
            "FOCUS AREA",
            "Reduce water use per jean by 30% by 2023",
            "EXCEEDED",
            "meeting goal two years early",
            "Reduce water use per jean by 50% by 2025",
            "ON TRACK",
            "increased, after meeting our initial goal",
        )

    def test_the_table_is_one_row_structured_region(self) -> None:
        row = self.row("AEO", 3, AEO_2023)
        self.assertEqual(len(row["regions"]), 1)
        self.assertEqual(row["regions"][0]["region_type"], "row_structured")

    def test_a_later_row_is_not_detached_from_its_status(self) -> None:
        """The regression this page guards against tore the STATUS/PROGRESS text
        of the later rows away from their GOAL text and emitted it at the end, so
        a row from further down the table is checked, not only the first.

        Asserted as one contiguous phrase rather than an ordering: "NEW" and
        "Initial work underway" appear against several goals, so a position
        comparison would compare against whichever row happens to come first.
        """

        row = self.row("AEO", 3, AEO_2023)
        self.assertIn(
            "committed to net-zero emissions by 2050 2022 new initial work underway "
            "phase out coal-fired boilers in our supply chain by 2030",
            self.flat(row),
        )


class ControlPageTests(RealPageBase):
    """Pages whose reading order was already correct and must not move."""

    def test_controls_stay_usable(self) -> None:
        controls = [row for row in self.rows.values() if row["role"] == "control"]
        self.assertEqual(len(controls), 5)
        for row in controls:
            self.assertEqual(row["candidate_status"], "candidate_ready", f"{row['ticker']} p{row['page']}")
            self.assertTrue(row["word_preservation_passed"], f"{row['ticker']} p{row['page']}")
            self.assertEqual(row["visual_verdict"], "unchanged", f"{row['ticker']} p{row['page']}")

    def test_bbw_contents_reads_down_each_column_then_the_report_note(self) -> None:
        row = self.row("BBW", 2, BBW_2023)
        self.assert_reads_in_order(
            row,
            "Message from Our CEO",
            "Conscientious Conduct",  # end of the left contents column
            "Caring for People",  # start of the right contents column
            "SASB Index",
            "About This Report",
            "unless otherwise noted.",
        )

    def test_aeo_polyester_page_reads_card_then_chart_then_table(self) -> None:
        row = self.row("AEO", 10, AEO_2023)
        self.assert_reads_in_order(
            row,
            "2023 SUSTAINABLE POLYESTER BREAKDOWN",
            "2023 PROGRESS",
            "100% sustainable polyester",
            "recycled polyester",
            "RECYCLED POYLESTER BY BRAND",
            "Total Recycled (kg)",
            "Total AEO",
        )

    def test_aeo_introduction_page_reads_top_to_bottom(self) -> None:
        row = self.row("AEO", 2, AEO_2023)
        self.assert_reads_in_order(
            row,
            "INTRODUCTION",
            "goals and the progress made through our Planet initiatives.",
            "Additional information can be found",
        )

    def test_aeo_numbers_page_keeps_chart_above_its_table(self) -> None:
        row = self.row("AEO", 6, AEO_2023)
        # "Total AEO" is also the last label on the chart's category axis, so the
        # table's own final row is identified by the values that follow it.
        self.assert_reads_in_order(
            row,
            "2023 REAL GOOD BY THE NUMBERS",
            "BRAND / CATEGORY",
            "Total Intimates",
            "Total AEO 19% 47% 71% 70%",
        )

    def test_aapl_section_index_order(self) -> None:
        row = self.row("AAPL", 97, AAPL_2024)
        self.assert_reads_in_order(
            row,
            "In this section",
            "A: Corporate facilities energy supplement",
            "F: ISO 14001 certification",
            "Report notes",
            "End notes",
        )


class HeldPageTests(RealPageBase):
    """Pages the visual audit still calls wrong. They are reported as needing
    review rather than quietly presented as fixed, and nothing here tries to
    reconstruct them."""

    def test_pages_the_audit_rejects_stay_flagged(self) -> None:
        for ticker, page, filename in (("BBWI", 36, BBWI_2023), ("AMZN", 63, AMZN_2023)):
            with self.subTest(ticker=ticker, page=page):
                row = self.row(ticker, page, filename)
                self.assertEqual(row["visual_verdict"], "needs review")
                self.assertTrue(row["word_preservation_passed"])

    def test_amzn_right_hand_panel_is_no_longer_merged_into_the_top_region(self) -> None:
        """This page stays held for its two prose columns, but the panel peel
        did separate the right-hand feature panel from the columns beside it.
        Its heading used to be pulled into the first region, hundreds of points
        away from the rest of its own panel."""

        row = self.row("AMZN", 63, AMZN_2023)
        self.assert_reads_in_order(
            row,
            "Improving Customer Discovery",
            "Looking Forward",
            "Discover Product Sustainability Features",
            "Badging in Search",
        )


class UnclearSyntheticLayoutTests(unittest.TestCase):
    def test_unclear_five_column_layout_needs_review(self) -> None:
        words = []
        for line in range(12):
            for edge in (50, 330, 610, 890, 1170):
                for part in range(6):
                    words.append(make_word(f"C{edge}_{line}_{part}", edge + part * 18, 60 + line * 24))
        result = regions_mod.reconstruct_by_regions(words, 1600, 500)
        self.assertEqual(result.status, "needs_review")


if __name__ == "__main__":
    unittest.main()
