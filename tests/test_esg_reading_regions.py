from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import esg_reading_regions as regions_mod


def make_word(text: str, x0: float, top: float, *, width: float = 14.0, upright: bool = True, bottom: float | None = None) -> dict:
    return {
        "text": text,
        "x0": x0,
        "x1": x0 + width,
        "top": top,
        "bottom": top + 8 if bottom is None else bottom,
        "upright": upright,
    }


def add_line(words: list[dict], prefix: str, x0: float, top: float, count: int = 6, spacing: float = 18.0) -> None:
    for index in range(count):
        words.append(make_word(f"{prefix}_{index}", x0 + index * spacing, top))


def add_pair(words: list[dict], prefix: str, x0: float, top: float, spacing: float) -> None:
    words.append(make_word(f"{prefix}A", x0, top))
    words.append(make_word(f"{prefix}B", x0 + spacing, top))


class TwoColumnProseTests(unittest.TestCase):
    def test_two_column_prose_reconstructs_left_then_right(self) -> None:
        words: list[dict] = []
        for line in range(12):
            add_line(words, f"LEFT{line}", 50, 80 + line * 24)
            add_line(words, f"RIGHT{line}", 470, 80 + line * 24)

        result = regions_mod.reconstruct_by_regions(words, 800, 500)

        self.assertEqual(result.status, "candidate_ready")
        self.assertEqual(len(result.regions), 1)
        self.assertEqual(result.regions[0].region_type, "multi_column_prose")
        self.assertEqual(result.regions[0].column_count, 2)
        self.assertLess(result.text.index("LEFT0_0"), result.text.index("RIGHT0_0"))
        self.assertLess(result.text.index("LEFT11_0"), result.text.index("RIGHT0_0"))
        self.assertEqual(result.preservation_ratio, 1.0)


class RecursiveColumnTests(unittest.TestCase):
    def test_four_columns_detected_inside_two_broad_columns(self) -> None:
        """Two broad columns whose inner gap (10pt) is below the page-level
        gutter floor (16pt) merge into one segment per line at the top level
        -- reproducing the measured BBWI-2023 p22 bug ("three columns are
        read like one"). The recursive inner pass (its own, smaller and
        explicitly-unvalidated floor) must recover all four.
        """
        words: list[dict] = []
        page_width = 900
        page_height = 550
        for line in range(20):
            top = 60 + line * 24
            add_line(words, f"A1L{line}", 50, top)
            add_line(words, f"A2L{line}", 164, top)
            add_line(words, f"B1L{line}", 400, top)
            add_line(words, f"B2L{line}", 514, top)

        result = regions_mod.reconstruct_by_regions(words, page_width, page_height)

        self.assertEqual(result.status, "candidate_ready")
        self.assertEqual(len(result.regions), 1)
        region = result.regions[0]
        self.assertEqual(region.region_type, "multi_column_prose")
        self.assertEqual(region.column_count, 4, "recursive refinement must recover all 4 sub-columns")
        text = result.text
        self.assertLess(text.index("A1L0_0"), text.index("A2L0_0"))
        self.assertLess(text.index("A2L0_0"), text.index("B1L0_0"))
        self.assertLess(text.index("B1L0_0"), text.index("B2L0_0"))
        self.assertEqual(result.preservation_ratio, 1.0)


class HeadingBoundaryTests(unittest.TestCase):
    def test_full_width_heading_then_columns_is_its_own_region(self) -> None:
        words: list[dict] = []
        add_line(words, "HEADING", 50, 60, count=30, spacing=18)  # spans ~ 50 to 590+14: full width
        for line in range(12):
            add_line(words, f"LEFT{line}", 50, 140 + line * 24)
            add_line(words, f"RIGHT{line}", 470, 140 + line * 24)

        result = regions_mod.reconstruct_by_regions(words, 800, 600)

        self.assertEqual(result.status, "candidate_ready")
        self.assertEqual(len(result.regions), 2)
        self.assertEqual(result.regions[0].region_type, "heading")
        self.assertEqual(result.regions[1].region_type, "multi_column_prose")
        text = result.text
        heading_pos = text.index("HEADING_0")
        left_pos = text.index("LEFT0_0")
        right_pos = text.index("RIGHT0_0")
        self.assertLess(heading_pos, left_pos)
        self.assertLess(heading_pos, right_pos)
        # The heading must not be split across the two columns.
        self.assertLess(text.index("HEADING_29"), left_pos)
        self.assertEqual(result.preservation_ratio, 1.0)


def _build_row_structured_block(top_start: float, edges: tuple[float, ...] = (50, 300, 550, 800), rows: int = 29) -> list[dict]:
    words: list[dict] = []
    # Header-ish wide row establishes each column's observed content width.
    for edge in edges:
        add_pair(words, f"HDR{edge}", edge, top_start, spacing=200)
    # Narrow label/value rows: short relative to the header's width.
    for row in range(rows):
        top = top_start + 15 * (row + 1)
        for edge in edges:
            add_pair(words, f"R{row}C{edge}", edge, top, spacing=16)
    return words


class RowStructureTests(unittest.TestCase):
    def test_row_label_value_grid_is_read_across_rows(self) -> None:
        words = _build_row_structured_block(top_start=50)

        result = regions_mod.reconstruct_by_regions(words, 1000, 600)

        self.assertEqual(result.status, "candidate_ready")
        self.assertEqual(len(result.regions), 1)
        region = result.regions[0]
        self.assertEqual(region.region_type, "row_structured")
        self.assertEqual(region.column_count, 4)
        # Row-major: the first row's four cells must all precede the second row's cells.
        text = result.text
        self.assertLess(text.index("HDR50A"), text.index("HDR300A"))
        self.assertLess(text.index("HDR300A"), text.index("HDR550A"))
        self.assertLess(text.index("HDR550A"), text.index("HDR800A"))
        self.assertLess(text.index("HDR800A"), text.index("R0C50A"))
        self.assertEqual(result.preservation_ratio, 1.0)


class MixedLayoutTests(unittest.TestCase):
    def test_two_different_layouts_on_one_page_become_two_regions(self) -> None:
        # page_height=1300 keeps every line clear of the 6% header/footer
        # exclusion bands (header_limit=78, footer_limit=1222) so the region
        # split reflects the two layouts, not a band-clipped row count.
        words: list[dict] = []
        for line in range(12):
            add_line(words, f"LEFT{line}", 50, 100 + line * 24)
            add_line(words, f"RIGHT{line}", 470, 100 + line * 24)
        prose_bottom = 100 + 11 * 24 + 8

        grid_words = _build_row_structured_block(top_start=prose_bottom + 150)
        words.extend(grid_words)

        result = regions_mod.reconstruct_by_regions(words, 1000, 1300)

        self.assertEqual(result.status, "candidate_ready")
        self.assertEqual(len(result.regions), 2)
        self.assertEqual(result.regions[0].region_type, "multi_column_prose")
        self.assertEqual(result.regions[1].region_type, "row_structured")
        text = result.text
        self.assertLess(text.index("RIGHT11_0"), text.index("HDR50A"))
        self.assertEqual(result.preservation_ratio, 1.0)


class SpanningPanelTests(unittest.TestCase):
    """A panel running alongside content that has its own row breaks.

    The panel bridges the gap between the two stacked blocks beside it, so no
    run boundary can fall between them while both are read together. Splitting
    into rows first therefore puts one slice of the panel into each row and
    emits the two alternately; the panel has to be peeled off first.
    """

    @staticmethod
    def _build() -> list[dict]:
        # 8 words per line spans 140pt, clearing PANEL_MIN_WIDTH_SHARE (12% of
        # the 1000pt page) on both sides of the gutter.
        words: list[dict] = []
        for line in range(9):
            add_line(words, f"UPPER{line}", 50, 100 + line * 25, count=8)
            add_line(words, f"LOWER{line}", 50, 500 + line * 25, count=8)
        # The sidebar's own lines are deliberately off the stacked blocks'
        # baselines, so this is a genuine independent panel rather than the
        # second column of one flowing block.
        for line in range(20):
            add_line(words, f"SIDE{line}", 600, 162 + line * 25, count=8)
        return words

    def test_spanning_panel_is_read_after_the_blocks_it_runs_beside(self) -> None:
        result = regions_mod.reconstruct_by_regions(self._build(), 1000, 1300)

        self.assertEqual(result.status, "candidate_ready")
        self.assertEqual(len(result.regions), 3)
        text = result.text
        self.assertLess(text.index("UPPER8_0"), text.index("LOWER0_0"))
        self.assertLess(text.index("LOWER8_0"), text.index("SIDE0_0"))
        self.assertEqual(result.preservation_ratio, 1.0)

    def test_the_panel_never_interrupts_the_blocks(self) -> None:
        result = regions_mod.reconstruct_by_regions(self._build(), 1000, 1300)

        text = result.text
        first_sidebar_word = min(text.index(f"SIDE{line}_0") for line in range(20))
        for line in range(9):
            self.assertLess(text.index(f"UPPER{line}_0"), first_sidebar_word)
            self.assertLess(text.index(f"LOWER{line}_0"), first_sidebar_word)


class UncertainRegionTests(unittest.TestCase):
    def test_too_many_stable_columns_is_held_not_guessed(self) -> None:
        """Five well-separated, vertically-overlapping, dense column clusters
        all pass the individual stability checks, but esg_reading_order caps
        trustworthy reconstruction at 4 columns (``MAX_COLUMN_COUNT``) -- past
        that point geometry alone is not considered strong enough evidence.
        The region must be held, not guessed at as either 5 real columns or
        collapsed back down to single-column prose.
        """
        words: list[dict] = []
        edges = [50, 330, 610, 890, 1170]
        for line in range(12):
            top = 60 + line * 24
            for edge in edges:
                add_line(words, f"COL{edge}L{line}", edge, top)

        result = regions_mod.reconstruct_by_regions(words, 1600, 500)

        self.assertEqual(result.status, "needs_review")
        self.assertTrue(any(region.region_type == "uncertain" for region in result.regions))
        # Held content must still be present verbatim for a reviewer to read,
        # just not silently reordered with confidence it does not have.
        self.assertIn("COL1170L0_0", result.text)
        self.assertEqual(result.preservation_ratio, 1.0)


class SafetyInvariantTests(unittest.TestCase):
    def test_no_word_is_lost_or_duplicated_across_mixed_regions(self) -> None:
        # page_height=1300: same header/footer clearance reasoning as the
        # mixed-layout test above.
        words: list[dict] = []
        add_line(words, "HEADING", 50, 30, count=30, spacing=18)
        for line in range(12):
            add_line(words, f"LEFT{line}", 50, 110 + line * 24)
            add_line(words, f"RIGHT{line}", 470, 110 + line * 24)
        grid_words = _build_row_structured_block(top_start=520)
        words.extend(grid_words)
        # A genuinely rotated decorative word (tall bbox, non-upright).
        words.append(make_word("SIDEBARTITLE", 20, 200, upright=False, bottom=480))

        result = regions_mod.reconstruct_by_regions(words, 1000, 1300)

        source_tokens = sorted(str(w["text"]).casefold() for w in words if str(w["text"]).strip())
        annotation_prefix = "[excluded"
        body_text = "\n".join(
            line for line in result.text.split("\n") if not line.strip().startswith(annotation_prefix)
        )
        output_tokens = sorted(part.casefold() for part in body_text.split() if part)
        self.assertEqual(source_tokens, output_tokens)
        self.assertEqual(result.source_word_count, len(words))
        self.assertEqual(result.preservation_ratio, 1.0)
        self.assertIn("SIDEBARTITLE", result.text)

    def test_empty_page_is_candidate_ready_with_empty_text(self) -> None:
        result = regions_mod.reconstruct_by_regions([], 800, 500)
        self.assertEqual(result.status, "candidate_ready")
        self.assertEqual(result.text, "")
        self.assertEqual(result.source_word_count, 0)

    def test_verified_table_hint_is_reused_verbatim(self) -> None:
        words = [make_word("Revenue", 50, 60), make_word("123", 100, 60)]
        result = regions_mod.reconstruct_by_regions(words, 800, 500, verified_table_text="Revenue 123")
        self.assertEqual(result.status, "candidate_ready")
        self.assertEqual(result.regions[0].region_type, "table_verified")
        self.assertEqual(result.text, "Revenue 123")
        self.assertEqual(result.preservation_ratio, 1.0)

    def test_verified_region_table_replaces_only_its_matched_region(self) -> None:
        words: list[dict] = [
            make_word("HEADER", 50, 20),
            make_word("FOOTER", 50, 1260),
        ]
        for line in range(12):
            add_line(words, f"LEFT{line}", 50, 100 + line * 24)
            add_line(words, f"RIGHT{line}", 470, 100 + line * 24)
        words.extend(_build_row_structured_block(top_start=600))

        edges = (50, 300, 550, 800)
        rows = [
            [f"HDR{edge}A HDR{edge}B" for edge in edges],
            *[
                [f"R{row}C{edge}A R{row}C{edge}B" for edge in edges]
                for row in range(29)
            ],
        ]
        markdown_lines = [
            "| " + " | ".join(rows[0]) + " |",
            "| " + " | ".join(["---"] * len(edges)) + " |",
            *["| " + " | ".join(row) + " |" for row in rows[1:]],
        ]
        markdown = "\n".join(markdown_lines)

        result = regions_mod.reconstruct_by_regions(
            words,
            1000,
            1300,
            verified_region_tables=[((40, 590, 1020, 1050), markdown)],
        )

        self.assertEqual(result.status, "candidate_ready")
        self.assertEqual([region.region_type for region in result.regions], [
            "multi_column_prose",
            "table_verified",
        ])
        self.assertEqual(len(result.region_texts), len(result.regions))
        self.assertIn("| HDR50A HDR50B |", result.region_texts[1])
        self.assertLess(result.text.index("HEADER"), result.text.index("LEFT0_0"))
        self.assertLess(result.text.index("RIGHT11_0"), result.text.index("| HDR50A"))
        self.assertLess(result.text.index("| R28C50A"), result.text.index("FOOTER"))
        self.assertEqual(result.preservation_ratio, 1.0)


if __name__ == "__main__":
    unittest.main()
