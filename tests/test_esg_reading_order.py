from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import esg_reading_order


def make_word(text: str, x0: float, top: float, *, upright: bool = True, bottom: float | None = None) -> dict:
    return {
        "text": text,
        "x0": x0,
        "x1": x0 + 14,
        "top": top,
        "bottom": top + 8 if bottom is None else bottom,
        "upright": upright,
    }


def add_line(words: list[dict], prefix: str, x0: float, top: float) -> None:
    for index in range(6):
        words.append(make_word(f"{prefix}_{index}", x0 + index * 18, top))


class ESGReadingOrderTests(unittest.TestCase):
    def test_reconstructs_stable_two_column_page_left_to_right(self) -> None:
        words: list[dict] = []
        for line in range(12):
            add_line(words, f"LEFT{line}", 50, 80 + line * 24)
            add_line(words, f"RIGHT{line}", 470, 80 + line * 24)

        result = esg_reading_order.reconstruct_column_order(words, 800, 500)

        self.assertEqual(result.status, "reconstructed")
        self.assertEqual(result.column_count, 2)
        self.assertEqual(result.preservation_ratio, 1.0)
        self.assertLess(result.text.index("LEFT0_0"), result.text.index("RIGHT0_0"))
        self.assertLess(result.text.index("LEFT11_0"), result.text.index("RIGHT0_0"))

    def test_single_column_page_is_not_reordered(self) -> None:
        words: list[dict] = []
        for line in range(12):
            add_line(words, f"BODY{line}", 50, 80 + line * 24)

        result = esg_reading_order.reconstruct_column_order(words, 800, 500)

        self.assertEqual(result.status, "not_applicable")
        self.assertEqual(result.text, "")

    def test_nonoverlapping_blocks_are_ambiguous_not_reconstructed(self) -> None:
        words: list[dict] = []
        for line in range(10):
            add_line(words, f"LEFT{line}", 50, 60 + line * 16)
            add_line(words, f"RIGHT{line}", 470, 300 + line * 16)

        result = esg_reading_order.reconstruct_column_order(words, 800, 500)

        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(result.text, "")

    def test_structural_grid_page_is_held_even_with_repeating_columns(self) -> None:
        words: list[dict] = []
        for line in range(12):
            add_line(words, f"LEFT{line}", 50, 80 + line * 24)
            add_line(words, f"RIGHT{line}", 470, 80 + line * 24)

        result = esg_reading_order.reconstruct_column_order(
            words,
            800,
            500,
            structural_grid_risk=True,
        )

        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(result.reason, "structural_grid_or_table_layout")

    def test_sparse_structural_grid_page_is_also_held(self) -> None:
        result = esg_reading_order.reconstruct_column_order(
            [make_word("Cover", 50, 50)],
            800,
            500,
            structural_grid_risk=True,
        )

        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(result.reason, "structural_grid_or_table_layout")

    def test_contents_page_is_not_treated_as_prose_columns(self) -> None:
        words: list[dict] = []
        for line in range(12):
            add_line(words, f"LEFT{line}", 50, 80 + line * 24)
            add_line(words, f"RIGHT{line}", 470, 80 + line * 24)
        words.append(make_word("Contents", 50, 40))
        for index in range(8):
            words.append(make_word(str(index + 1), 60, 55 + index * 10))

        result = esg_reading_order.reconstruct_column_order(words, 800, 500)

        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(result.reason, "navigation_contents_layout")

    def test_ragged_left_edge_words_stay_in_their_own_column(self) -> None:
        """A column boundary must be its left edge, not the median of its starts.

        Bullet glyphs and indented runs make a column's left edge ragged. The
        median then sits inside the column, so its own leftmost words sort below
        it and were handed to the previous column -- while preservation_ratio
        stayed 1.0000, because no token was lost, only misplaced.
        """
        words: list[dict] = []
        for line in range(12):
            add_line(words, f"LEFT{line}", 50, 80 + line * 24)
        # Right column: six lines flush at 470, six indented to 490. The median
        # start is 480, so every word at 470 used to fall into the left column.
        for line in range(6):
            add_line(words, f"FLUSH{line}", 470, 80 + line * 24)
        for line in range(6):
            add_line(words, f"INDENT{line}", 490, 224 + line * 24)

        result = esg_reading_order.reconstruct_column_order(words, 800, 500)

        self.assertEqual(result.status, "reconstructed")
        self.assertEqual(result.column_count, 2)
        self.assertEqual(result.preservation_ratio, 1.0)
        # Every left-column word precedes every right-column word, including the
        # flush-left ones that anchor the right column's ragged edge.
        self.assertLess(result.text.index("LEFT11_0"), result.text.index("FLUSH0_0"))
        self.assertLess(result.text.index("LEFT11_0"), result.text.index("INDENT0_0"))

    def test_rotated_word_in_header_band_is_excluded(self) -> None:
        """A non-upright word whose whole bbox sits inside the header band
        must not surface in the reconstructed header text. Uses 20 lines per
        column (240 words) rather than the usual 12; the ratio's interaction
        with dropped words is exercised separately in
        test_rotated_words_do_not_count_against_preservation_budget.
        """
        words: list[dict] = []
        for line in range(20):
            add_line(words, f"LEFT{line}", 50, 80 + line * 24)
            add_line(words, f"RIGHT{line}", 470, 80 + line * 24)
        words.append(make_word("SIDEWAYS", 50, 5, upright=False, bottom=25))

        result = esg_reading_order.reconstruct_column_order(words, 800, 800)

        self.assertEqual(result.status, "reconstructed")
        self.assertNotIn("SIDEWAYS", result.text)

    def test_rotated_word_spanning_into_body_is_excluded_from_column(self) -> None:
        """A rotated sidebar word's bbox is tall (it spans the vertical run of
        the glyphs, not one line's height), so its ``bottom`` usually reaches
        past the header band and it used to fall through to column
        assignment, interleaving into real body text at whatever line the
        rotated word's ``top`` happened to coincide with. It must be dropped
        instead, matching the reconstructed-page-vs-authored-order finding on
        LULU-2021 p.29 in reports/esg_tagged_pdf_ground_truth_2026-07-17/.
        """
        words: list[dict] = []
        for line in range(20):
            add_line(words, f"LEFT{line}", 50, 80 + line * 24)
            add_line(words, f"RIGHT{line}", 470, 80 + line * 24)
        # Starts inside the header band (top=10) but its bbox runs deep into
        # the body (bottom=300), the same shape a real rotated sidebar title
        # has -- it must not appear in headers OR be picked up by a column.
        words.append(make_word("SIDEBARTITLE", 50, 10, upright=False, bottom=300))

        result = esg_reading_order.reconstruct_column_order(words, 800, 800)

        self.assertEqual(result.status, "reconstructed")
        self.assertEqual(result.column_count, 2)
        self.assertNotIn("SIDEBARTITLE", result.text)

    def test_rotated_words_do_not_count_against_preservation_budget(self) -> None:
        """Deliberately dropped rotated words are excluded from the
        preservation-ratio denominator. Under the old policy a page like
        LULU-2021 p.29 (16 of 668 words rotated decoration, ratio 0.976) was
        held as ``ambiguous`` even though every upright body word survived;
        now the ratio measures only words the reconstructor intended to keep,
        so the same page reconstructs with a perfect ratio.
        """
        words: list[dict] = []
        for line in range(12):
            add_line(words, f"LEFT{line}", 50, 80 + line * 24)
            add_line(words, f"RIGHT{line}", 470, 80 + line * 24)
        words.append(make_word("SIDEBARTITLE", 50, 10, upright=False, bottom=300))

        result = esg_reading_order.reconstruct_column_order(words, 800, 500)

        self.assertEqual(result.status, "reconstructed")
        self.assertNotIn("SIDEBARTITLE", result.text)
        self.assertGreaterEqual(result.preservation_ratio, 0.995)

    def test_short_nonupright_word_is_kept_not_dropped(self) -> None:
        """Not every ``upright: False`` word is real rotation: GES-GUESS-2024
        p.9 has 907 words pdfminer flags non-upright (an embedded
        Arial-ItalicMT run reads as a negative text-scaling matrix) that are
        ordinary ~7pt single-line body text -- visually confirmed by
        rendering the page, no rotation at all. A genuinely rotated word's
        bbox height grows with its character count; this one stays within a
        single line's height, so it must flow through as normal content.
        """
        words: list[dict] = []
        for line in range(12):
            add_line(words, f"LEFT{line}", 50, 80 + line * 24)
            add_line(words, f"RIGHT{line}", 470, 80 + line * 24)
        words.append(make_word("numbers", 200, 90, upright=False, bottom=97))

        result = esg_reading_order.reconstruct_column_order(words, 800, 500)

        self.assertEqual(result.status, "reconstructed")
        self.assertIn("numbers", result.text)
        self.assertEqual(result.preservation_ratio, 1.0)

    def test_canonical_order_text_ignores_whitespace_only(self) -> None:
        self.assertEqual(
            esg_reading_order.canonical_order_text("Climate\n  strategy\tand  targets"),
            esg_reading_order.canonical_order_text("climate strategy and targets"),
        )


if __name__ == "__main__":
    unittest.main()
