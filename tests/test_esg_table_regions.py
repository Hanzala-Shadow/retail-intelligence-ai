from __future__ import annotations

from esg_reading_regions import reconstruct_by_regions
from esg_table_regions import TableBox, detect_table_boxes, reconstruct_table_regions


def _word(text: str, x0: float, top: float, width: float | None = None) -> dict:
    width = width if width is not None else max(12.0, len(text) * 5.0)
    return {
        "text": text,
        "x0": x0,
        "x1": x0 + width,
        "top": top,
        "bottom": top + 9.0,
        "size": 9.0,
        "upright": True,
    }


def _small_table_in_left_column() -> tuple[list[dict], TableBox]:
    words = [
        _word("Left", 40, 45),
        _word("intro", 70, 45),
        _word("continues", 40, 62),
        _word("above", 90, 62),
        _word("Metric", 45, 105),
        _word("Value", 165, 105),
        _word("Water", 45, 123),
        _word("10", 165, 123),
        _word("Waste", 45, 141),
        _word("20", 165, 141),
        _word("Left", 40, 181),
        _word("continues", 70, 181),
        _word("below", 40, 198),
    ]
    for index, text in enumerate(
        ["Right one", "Right two", "Right three", "Right four", "Right five", "Right six", "Right seven", "Right eight", "Right nine", "Right ten"]
    ):
        words.append(_word(text, 340, 45 + index * 17, 75))
    markdown = "\n".join(
        [
            "| Metric | Value |",
            "| --- | --- |",
            "| Water | 10 |",
            "| Waste | 20 |",
        ]
    )
    return words, TableBox(35, 98, 235, 155, markdown)


def test_small_table_is_atomic_and_drives_left_column_order() -> None:
    words, table = _small_table_in_left_column()

    result = reconstruct_table_regions(
        words, 600.0, 300.0, [table], include_unruled=False
    )

    assert result.status == "candidate_ready"
    assert result.preservation_ratio == 1.0
    assert result.extra_token_ratio == 0.0
    assert [unit.kind for unit in result.units].count("table") == 1
    table_unit = next(unit for unit in result.units if unit.kind == "table")
    assert "| Water | 10 |" in table_unit.text
    assert result.text.index("Left intro") < result.text.index("Metric")
    assert result.text.index("Metric") < result.text.index("Left continues")
    assert result.text.index("Left continues") < result.text.index("Right one")


def test_short_table_does_not_need_stable_column_minimums() -> None:
    words, table = _small_table_in_left_column()

    result = reconstruct_table_regions(
        words, 600.0, 300.0, [table], include_unruled=False
    )

    table_unit = next(unit for unit in result.units if unit.kind == "table")
    assert table_unit.source_word_count == 6
    assert result.table_markdown_used == 1


def test_bad_table_serialisation_falls_back_without_losing_words() -> None:
    words, table = _small_table_in_left_column()
    damaged = TableBox(table.left, table.top, table.right, table.bottom, "| Metric | Value |")

    result = reconstruct_table_regions(
        words, 600.0, 300.0, [damaged], include_unruled=False
    )

    assert result.status == "candidate_ready"
    assert result.table_markdown_used == 0
    assert result.table_source_fallbacks == 1
    assert "Water 10" in next(unit.text for unit in result.units if unit.kind == "table")
    assert result.preservation_ratio == 1.0
    assert result.extra_token_ratio == 0.0


def test_no_table_box_is_exact_reader_b_fallback() -> None:
    words = [
        _word("A", 40, 50),
        _word("simple", 55, 50),
        _word("page", 40, 68),
        _word("continues", 70, 68),
    ]

    expected = reconstruct_by_regions(words, 600.0, 300.0)
    result = reconstruct_table_regions(
        words, 600.0, 300.0, [], include_unruled=False
    )

    assert result.reason == "no_table_box_fell_back_to_reader_b"
    assert result.text == expected.text
    assert result.preservation_ratio == 1.0


def test_caller_can_supply_production_fallback_text() -> None:
    words = [_word("Production", 40, 50), _word("fallback", 100, 50)]

    result = reconstruct_table_regions(
        words,
        600.0,
        300.0,
        [],
        include_unruled=False,
        fallback_text="Production fallback",
    )

    assert result.text == "Production fallback"
    assert result.preservation_ratio == 1.0


def test_near_duplicate_table_boxes_are_deduplicated() -> None:
    words, table = _small_table_in_left_column()
    duplicate = TableBox(36, 99, 234, 154, table.text, "unruled_anchor")

    boxes = detect_table_boxes(
        words, 600.0, [table, duplicate], include_unruled=False
    )

    assert len(boxes) == 1
    assert boxes[0].source == "ruled"
