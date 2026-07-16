from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import esg_reading_order


def make_word(text: str, x0: float, top: float) -> dict:
    return {
        "text": text,
        "x0": x0,
        "x1": x0 + 14,
        "top": top,
        "bottom": top + 8,
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

    def test_canonical_order_text_ignores_whitespace_only(self) -> None:
        self.assertEqual(
            esg_reading_order.canonical_order_text("Climate\n  strategy\tand  targets"),
            esg_reading_order.canonical_order_text("climate strategy and targets"),
        )


if __name__ == "__main__":
    unittest.main()
