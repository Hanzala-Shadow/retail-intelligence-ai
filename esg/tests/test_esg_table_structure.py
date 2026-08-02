from __future__ import annotations

import pytest

from esg_table_structure import (
    CellBox,
    TableNotEmbeddingEligible,
    validate_table,
)


def word(idx, text, x0, x1, top, bottom):
    return {
        "idx": idx,
        "text": text,
        "x0": x0,
        "x1": x1,
        "top": top,
        "bottom": bottom,
        "upright": True,
    }


def simple_table_words():
    return [
        word(1, "Metric", 5, 35, 4, 12),
        word(2, "FY23", 55, 80, 4, 12),
        word(3, "FY24", 105, 130, 4, 12),
        word(4, "Energy", 5, 35, 24, 32),
        word(5, "10", 60, 75, 24, 32),
        word(6, "12", 110, 125, 24, 32),
    ]


def simple_table_cells():
    return [
        [
            CellBox(0, 0, (0, 0, 50, 20)),
            CellBox(0, 1, (50, 0, 100, 20)),
            CellBox(0, 2, (100, 0, 150, 20)),
        ],
        [
            CellBox(1, 0, (0, 20, 50, 40)),
            CellBox(1, 1, (50, 20, 100, 40)),
            CellBox(1, 2, (100, 20, 150, 40)),
        ],
    ]


def test_verified_table_proves_each_word_owner_and_column_order():
    result = validate_table(
        words=simple_table_words(),
        cells=simple_table_cells(),
        extracted_rows=[
            ["Metric", "FY23", "FY24"],
            ["Energy", "10", "12"],
        ],
        table_bbox=(0, 0, 150, 40),
        header_rows=[0],
    )

    assert result.status == "verified"
    assert result.embedding_eligible
    assert result.unassigned_word_ids == ()
    assert result.ambiguous_word_ids == ()
    assert result.word_recall == 1.0
    assert [cell.text for cell in result.cells] == ["Metric", "FY23", "FY24", "Energy", "10", "12"]
    markdown = result.to_markdown()
    assert "| Metric | FY23 | FY24 |" in markdown
    assert "| Energy | 10 | 12 |" in markdown
    assert markdown.count("Metric") == 1
    records = result.to_embedding_records()
    assert len(records) == 1
    assert records[0]["row_index"] == 1


def test_swapped_extractor_cells_are_rebuilt_from_geometry():
    result = validate_table(
        words=simple_table_words(),
        cells=simple_table_cells(),
        extracted_rows=[
            ["Metric", "FY24", "FY23"],
            ["Energy", "12", "10"],
        ],
        table_bbox=(0, 0, 150, 40),
        header_rows=[0],
    )

    assert result.status == "reconstructable"
    assert result.embedding_eligible
    assert not result.extracted_matches
    assert "extractor_cell_text_mismatch" in result.reason_codes
    markdown = result.to_markdown()
    assert "| Metric | FY23 | FY24 |" in markdown
    assert "| Energy | 10 | 12 |" in markdown


def test_word_crossing_a_column_boundary_is_not_guessed():
    result = validate_table(
        words=[word(1, "10", 45, 55, 24, 32)],
        cells=[[CellBox(0, 0, (0, 0, 50, 40)), CellBox(0, 1, (50, 0, 100, 40))]],
        extracted_rows=[["10", ""]],
        table_bbox=(0, 0, 100, 40),
    )

    assert not result.embedding_eligible
    assert result.status == "review"
    assert result.ambiguous_word_ids == (1,)
    assert "word_has_multiple_possible_cell_owners" in result.reason_codes


def test_readable_word_in_a_gap_is_not_silently_dropped():
    result = validate_table(
        words=[
            word(1, "Energy", 5, 35, 4, 12),
            word(2, "512", 70, 85, 4, 12),
        ],
        cells=[[CellBox(0, 0, (0, 0, 50, 20)), CellBox(0, 1, (90, 0, 100, 20))]],
        extracted_rows=[["Energy", "512"]],
        table_bbox=(0, 0, 100, 20),
    )

    assert not result.embedding_eligible
    assert result.unassigned_word_ids == (2,)
    assert "readable_words_without_cell_owner" in result.reason_codes
    with pytest.raises(TableNotEmbeddingEligible):
        result.to_embedding_records()


def test_multiline_cell_keeps_all_words_in_one_source_backed_cell():
    result = validate_table(
        words=[
            word(1, "Supplier", 5, 40, 4, 12),
            word(2, "North", 5, 25, 24, 32),
            word(3, "America", 27, 55, 24, 32),
            word(4, "90%", 65, 85, 24, 32),
        ],
        cells=[
            [CellBox(0, 0, (0, 0, 100, 20))],
            [CellBox(1, 0, (0, 20, 60, 40)), CellBox(1, 1, (60, 20, 100, 40))],
        ],
        extracted_rows=[["Supplier"], ["North\nAmerica", "90%"]],
        table_bbox=(0, 0, 100, 40),
        header_rows=[0],
    )

    assert result.embedding_eligible
    assert result.cells[1].text == "North America"
    records = result.to_embedding_records(document_id="DOC", page_number=3, table_id="T1")
    assert len(records) == 1
    assert records[0]["source_word_ids"] == [2, 3, 4]
    assert "90%" in records[0]["text"]


def test_no_declared_header_does_not_promote_first_row_to_header():
    result = validate_table(
        words=simple_table_words(),
        cells=simple_table_cells(),
        extracted_rows=[
            ["Metric", "FY23", "FY24"],
            ["Energy", "10", "12"],
        ],
        table_bbox=(0, 0, 150, 40),
    )

    assert result.header_status == "not_declared"
    assert not result.embedding_eligible
    assert "header_rows_required_for_embedding" in result.reason_codes
    with pytest.raises(TableNotEmbeddingEligible):
        result.to_markdown()


def test_overlapping_cells_without_a_declared_span_are_held():
    result = validate_table(
        words=[word(1, "Energy", 5, 35, 4, 12)],
        cells=[[CellBox(0, 0, (0, 0, 70, 20)), CellBox(0, 1, (50, 0, 100, 20))]],
        extracted_rows=[["Energy", ""]],
        table_bbox=(0, 0, 100, 20),
    )

    assert not result.embedding_eligible
    assert "overlapping_cells_without_span" in result.reason_codes


def test_one_row_header_fragment_is_not_a_table():
    result = validate_table(
        words=[
            word(1, "FY23", 5, 25, 4, 12),
            word(2, "FY24", 55, 75, 4, 12),
        ],
        cells=[[CellBox(0, 0, (0, 0, 50, 20)), CellBox(0, 1, (50, 0, 100, 20))]],
        extracted_rows=[["FY23", "FY24"]],
        table_bbox=(0, 0, 100, 20),
        header_rows=[0],
    )

    assert not result.embedding_eligible
    assert result.status == "review"
    assert "insufficient_table_shape" in result.reason_codes


def test_one_column_page_furniture_is_not_a_table():
    result = validate_table(
        words=[
            word(1, "Notes", 5, 30, 4, 12),
            word(2, "Page", 5, 25, 24, 32),
            word(3, "1", 5, 10, 44, 52),
        ],
        cells=[
            [CellBox(0, 0, (0, 0, 100, 20))],
            [CellBox(1, 0, (0, 20, 100, 40))],
            [CellBox(2, 0, (0, 40, 100, 60))],
        ],
        extracted_rows=[["Notes"], ["Page"], ["1"]],
        table_bbox=(0, 0, 100, 60),
        header_rows=[0],
    )

    assert not result.embedding_eligible
    assert "insufficient_table_shape" in result.reason_codes


def test_near_full_page_candidate_is_not_a_table():
    result = validate_table(
        words=[
            word(1, "Metric", 20, 50, 20, 30),
            word(2, "Value", 60, 90, 20, 30),
            word(3, "Energy", 20, 50, 50, 60),
            word(4, "10", 60, 75, 50, 60),
        ],
        cells=[
            [CellBox(0, 0, (0, 0, 90, 45)), CellBox(0, 1, (90, 0, 180, 45))],
            [CellBox(1, 0, (0, 45, 90, 90)), CellBox(1, 1, (90, 45, 180, 90))],
        ],
        extracted_rows=[["Metric", "Value"], ["Energy", "10"]],
        table_bbox=(0, 0, 180, 90),
        page_bbox=(0, 0, 200, 100),
        header_rows=[0],
    )

    assert not result.embedding_eligible
    assert "table_too_large" in result.reason_codes
