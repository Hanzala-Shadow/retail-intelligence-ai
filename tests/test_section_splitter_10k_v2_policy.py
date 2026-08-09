from src.section_splitter_10k_v2 import (
    section_quality,
    selected_boundaries,
)


def test_split_item_no_heading_beats_later_title_only_candidate():
    substantive = "Initial business overview narrative. " * 100
    later = "Later business strategy narrative. " * 100

    text = (
        "TABLE OF CONTENTS\n"
        "Item No. 1 | Description of Business | 1\n\n"
        "Item\n"
        "No. 1 –Business\n"
        "Business Overview\n"
        f"{substantive}\n"
        "Business\n"
        "Strategies and Developments\n"
        f"{later}\n"
        "Item No. 1A – Risk Factors\n"
        f"{substantive}\n"
        "Item No. 7 – Management’s Discussion and Analysis "
        "of Financial Condition and Results of Operations\n"
        f"{substantive}\n"
        "Item No. 8 – Financial Statements and Supplementary Data\n"
        f"{substantive}\n"
        "SIGNATURES\nAuthorized signature."
    )

    selected, _ = selected_boundaries(text)
    item_1 = next(
        candidate
        for candidate in selected
        if candidate["code"] == "Item_1"
    )

    assert item_1["position"] == text.index("Item\nNo. 1")
    assert not item_1.get("title_only", False)


def test_reversed_item_7a_title_is_selected():
    substantive = "Management discussion narrative. " * 100

    text = (
        "Item No. 7 – Management’s Discussion and Analysis "
        "of Financial Condition and Results of Operations\n"
        f"{substantive}\n"
        "Item\n"
        "No. 7A – Qualitative and Quantitative Disclosures "
        "Regarding Market Risk\n"
        "Not applicable.\n"
        "Item No. 8 – Financial Statements and Supplementary Data\n"
        f"{substantive}\n"
        "Item No. 9 – Changes in and Disagreements with Accountants\n"
        "None."
    )

    selected, _ = selected_boundaries(text)
    item_7a = next(
        candidate
        for candidate in selected
        if candidate["code"] == "Item_7A"
    )

    assert item_7a["method"] == "reversed_item_7a_title_alias"


def test_item_6_is_always_non_rag_for_fy2325():
    status, flags, action = section_quality(
        "Item_6",
        "Item 6. Selected Financial Data. Not required.",
    )

    assert status == "passed"
    assert "item_6_non_rag" in flags
    assert action == "exclude"


def test_financial_statement_index_is_not_running_toc():
    status, flags, action = section_quality(
        "Item_8",
        "TABLE OF CONTENTS TO FINANCIAL STATEMENTS\nBalance Sheets",
    )

    assert status == "passed"
    assert "contains_toc_running_header" not in flags
    assert action == "include"
