from __future__ import annotations

import sys
import unittest
from pathlib import Path


import pdf_parser


def make_word(text: str, x0: float, top: float, x1: float | None = None, bottom: float | None = None) -> dict:
    return {
        "text": text,
        "x0": x0,
        "x1": x1 if x1 is not None else x0 + 10,
        "top": top,
        "bottom": bottom if bottom is not None else top + 10,
    }


class _FakeRow:
    def __init__(self, cells, bbox=None):
        self.cells = cells
        # Real Table.rows expose a bbox spanning the row's non-None cells; the
        # helper uses it (via table_bbox, or synthesized from all rows) to scope
        # the token budget, so fakes must supply one too.
        self.bbox = bbox if bbox is not None else _bbox_from_cells(cells)


def _bbox_from_cells(cells):
    boxes = [cell for cell in cells if cell is not None]
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


class BackfillEmptyTableCellsTests(unittest.TestCase):
    def test_empty_cell_gets_filled_from_word_center(self):
        table_rows = [_FakeRow([(0, 0, 50, 10), (50, 0, 100, 10)])]
        data = [["kept", None]]
        words = [make_word("word", 70, 0, x1=80, bottom=10)]

        result = pdf_parser._backfill_empty_table_cells(data, table_rows, words)

        self.assertEqual(result, [["kept", "word"]])

    def test_nonempty_cell_is_never_overwritten(self):
        table_rows = [_FakeRow([(0, 0, 50, 10), (50, 0, 100, 10)])]
        data = [["kept", "already here"]]
        words = [make_word("word", 70, 0, x1=80, bottom=10)]

        result = pdf_parser._backfill_empty_table_cells(data, table_rows, words)

        self.assertEqual(result, [["kept", "already here"]])

    def test_none_cell_bbox_is_skipped_for_merged_cells(self):
        table_rows = [_FakeRow([(0, 0, 50, 10), None])]
        data = [["kept", None]]
        words = [make_word("word", 70, 0, x1=80, bottom=10)]

        result = pdf_parser._backfill_empty_table_cells(data, table_rows, words)

        self.assertEqual(result, [["kept", None]])

    def test_multi_word_ordering_is_top_then_left(self):
        table_rows = [_FakeRow([(0, 0, 100, 40)])]
        data = [[None]]
        words = [
            make_word("second", 10, 20, x1=20, bottom=30),
            make_word("first", 10, 0, x1=20, bottom=10),
            make_word("also-first", 30, 0, x1=40, bottom=10),
        ]

        result = pdf_parser._backfill_empty_table_cells(data, table_rows, words)

        self.assertEqual(result, [["first also-first second"]])

    def test_row_count_mismatch_is_tolerated(self):
        table_rows = [
            _FakeRow([(0, 0, 50, 10)]),
            _FakeRow([(0, 10, 50, 20)]),
            _FakeRow([(0, 20, 50, 30)]),
        ]
        data = [["only row"]]
        words = [make_word("word", 10, 10, x1=20, bottom=20)]

        result = pdf_parser._backfill_empty_table_cells(data, table_rows, words)

        self.assertEqual(result, [["only row"]])


if __name__ == "__main__":
    unittest.main()
