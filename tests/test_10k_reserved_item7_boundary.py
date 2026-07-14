from importlib.util import (
    module_from_spec,
    spec_from_file_location,
)
from pathlib import Path

import pytest


SPLITTER_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "section_splitter_10k.py"
)

SPEC = spec_from_file_location(
    "section_splitter_10k_under_test",
    SPLITTER_PATH,
)

if SPEC is None or SPEC.loader is None:
    raise RuntimeError(
        f"Unable to load splitter from {SPLITTER_PATH}"
    )

SPLITTER = module_from_spec(SPEC)
SPEC.loader.exec_module(SPLITTER)

_collect_item_candidates = SPLITTER._collect_item_candidates
_select_ordered_candidates = SPLITTER._select_ordered_candidates
_split_at_selected_boundaries = (
    SPLITTER._split_at_selected_boundaries
)


@pytest.mark.parametrize(
    "text",
    [
        (
            "Item 6. [Reserved]\n\n"
            "Item 7. Management\n"
            "’\n"
            "s Discussion and Analysis of Financial Condition "
            "and Results of Operations.\n"
            + ("Opening MD&A discussion. " * 100)
            + "\n"
            "Item 7. Management’s Discussion and Analysis "
            "of Financial Condition and Results of Operations\n"
            + ("Later MD&A discussion. " * 100)
        ),
        (
            "Item\n"
            "6. [Reserved]\n\n"
            "Item\n"
            "7. Management\n"
            "’\n"
            "s Discussion and Analysis of Financial Condition "
            "and Results of Operations\n"
            + ("Safe Harbor Declaration. " * 100)
            + "\n"
            "Management’s Discussion and Analysis.\n"
            + ("Later accounting discussion. " * 100)
        ),
        (
            "Item\n"
            "6.\n"
            "[Reserved]\n\n"
            "Item\n"
            "7. Management\n"
            "’\n"
            "s Discussion and Analysis of Financial Condition "
            "and Results of Operations\n"
            + ("Results and liquidity discussion. " * 100)
            + "\n"
            "Management’s Discussion and Analysis.\n"
            + ("Later critical accounting discussion. " * 100)
        ),
    ],
)
def test_reserved_item_6_ends_before_true_item_7(text):
    candidates = _collect_item_candidates(text)
    selected = _select_ordered_candidates(candidates)
    sections = _split_at_selected_boundaries(
        text,
        selected,
    )

    item_6 = " ".join(sections["Item_6"].split())
    item_7 = " ".join(sections["Item_7"].split())

    assert "Reserved" in item_6
    assert "Management" not in item_6
    assert len(item_6) < 100

    assert item_7.startswith("Item")
    assert "Management" in item_7[:250]
    assert "Discussion and Analysis" in item_7[:250]


@pytest.mark.parametrize(
    "text",
    [
        (
            "Item 6. [Reserved]\n\n"
            "Item 7. Management\n"
            "’\n"
            "s Discussion and Analysis of Financial Condition "
            "and Results of Operations.\n"
            + ("Opening MD&A discussion. " * 100)
            + "\n"
            "Item 7. Management’s Discussion and Analysis "
            "of Financial Condition and Results of Operations\n"
            + ("Later MD&A discussion. " * 100)
        ),
        (
            "Item\n"
            "6. [Reserved]\n\n"
            "Item\n"
            "7. Management\n"
            "’\n"
            "s Discussion and Analysis of Financial Condition "
            "and Results of Operations\n"
            + ("Opening MD&A discussion. " * 100)
            + "\n"
            "Management’s Discussion and Analysis.\n"
            + ("Later MD&A discussion. " * 100)
        ),
    ],
)
def test_promoted_reserved_item7_candidate_is_selected(text):
    candidates = _collect_item_candidates(text)
    selected = _select_ordered_candidates(candidates)

    promoted = [
        candidate
        for candidate in candidates["Item_7"]
        if candidate.get("reserved_item_6_successor")
    ]

    selected_item_7 = next(
        candidate
        for candidate in selected
        if candidate["code"] == "Item_7"
    )

    assert len(promoted) == 1
    assert promoted[0]["score"] == 130
    assert selected_item_7["position"] == promoted[0]["position"]
    assert selected_item_7["reserved_item_6_successor"] is True


def test_page_numbered_reserved_item_6_uses_real_item_7_boundary():
    text = (
        "Item 6. [RESERVED]\n"
        "39\n"
        "Table of Contents\n"
        "Item 7.\n"
        "—\n"
        "MANAGEMENT’S DISCUSSION AND ANALYSIS OF "
        "FINANCIAL CONDITION AND RESULTS OF OPERATIONS\n"
        + ("Opening MD&A discussion. " * 100)
        + "\n"
        "Management’s Discussion and Analysis.\n"
        + ("Later MD&A subsection. " * 100)
    )

    candidates = _collect_item_candidates(text)
    baseline_item_6_candidates = candidates["Item_6"]

    assert any(
        candidate.get(
            "toc_page_number_after_heading"
        ) is True
        for candidate in baseline_item_6_candidates
    )

    selected = _select_ordered_candidates(candidates)

    selected_item_7 = next(
        candidate
        for candidate in selected
        if candidate["code"] == "Item_7"
    )

    assert (
        selected_item_7.get(
            "reserved_item_6_successor"
        )
        is True
    )
    assert selected_item_7["score"] == 130

    sections = _split_at_selected_boundaries(
        text,
        selected,
    )

    item_6 = " ".join(sections["Item_6"].split())
    item_7 = " ".join(sections["Item_7"].split())

    assert "RESERVED" in item_6
    assert "Item 7" not in item_6
    assert "MANAGEMENT" not in item_6
    assert len(item_6) < 100

    assert item_7.startswith("Item 7")
    assert "MANAGEMENT" in item_7[:250]
    assert "DISCUSSION AND ANALYSIS" in item_7[:250]
