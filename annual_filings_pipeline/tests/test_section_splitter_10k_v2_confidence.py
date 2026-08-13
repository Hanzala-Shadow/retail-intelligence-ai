from src.section_splitter_10k_v2 import (
    boundary_confidence,
    selected_boundaries,
)


def test_exact_canonical_heading_overrides_false_toc_cluster():
    candidate = {
        "code": "Item_7",
        "score": 50,
        "line": (
            "Item 7. Management’s Discussion and Analysis "
            "of Financial Condition and Results of Operations"
        ),
        "toc_cluster": True,
        "toc_page_number_after_heading": False,
    }

    assert boundary_confidence(candidate) == "high"


def test_singular_item_8_heading_is_selected():
    substantive = "Substantive financial discussion. " * 80
    text = (
        f"ITEM 7. Management’s Discussion and Analysis of "
        f"Financial Condition and Results of Operations\n"
        f"{substantive}\n"
        "ITEM 7A. Quantitative and Qualitative Disclosures "
        "about Market Risk\n"
        f"{substantive}\n"
        "ITEM 8. Financial Statement and Supplementary Data\n"
        f"{substantive}\n"
        "ITEM 9. Changes in and Disagreements with Accountants\n"
        "None.\n"
        "SIGNATURES\nAuthorized signature."
    )

    selected, _ = selected_boundaries(text)
    item_8 = next(
        candidate
        for candidate in selected
        if candidate["code"] == "Item_8"
    )

    assert item_8["line"] == (
        "ITEM 8. Financial Statement and Supplementary Data"
    )
    assert item_8["method"] == "canonical_singular_item_8_alias"
    assert boundary_confidence(item_8) == "high"


def test_multiline_item_8_heading_is_promoted_to_high_confidence():
    substantive = "Substantive financial discussion. " * 80
    text = (
        f"ITEM 1. BUSINESS\n{substantive}\n"
        f"ITEM 1A. RISK FACTORS\n{substantive}\n"
        f"ITEM 7. MANAGEMENT’S DISCUSSION AND ANALYSIS OF "
        f"FINANCIAL CONDITION AND RESULTS OF OPERATIONS\n"
        f"{substantive}\n"
        "ITEM 8. FINANCIAL\n"
        "STATEMENTS AND SUPPLEMENTARY FINANCIAL DATA\n"
        f"{substantive}\n"
        "SIGNATURES\nAuthorized signature."
    )

    selected, _ = selected_boundaries(text)
    item_8 = next(
        candidate
        for candidate in selected
        if candidate["code"] == "Item_8"
    )

    assert item_8["method"] == "multiline_canonical_item_heading"
    assert boundary_confidence(item_8) == "high"


def test_canonical_body_heading_survives_nearby_toc_phrase():
    substantive = "Substantive financial disclosure. " * 80
    text = (
        "Table of Contents\n"
        "Item 8. Financial Statements and Supplementary Data 45\n\n"
        f"ITEM 7. MANAGEMENT’S DISCUSSION AND ANALYSIS OF "
        f"FINANCIAL CONDITION AND RESULTS OF OPERATIONS\n"
        f"{substantive}\n"
        "Table of Contents to Financial Statements\n"
        "ITEM 8. FINANCIAL STATEMENTS AND SUPPLEMENTARY DATA\n"
        "ITEM 9. CHANGES IN AND DISAGREEMENTS WITH ACCOUNTANTS\n"
        "ITEM 9A. CONTROLS AND PROCEDURES\n"
        "ITEM 9B. OTHER INFORMATION\n"
        f"{substantive}\n"
        "SIGNATURES\nAuthorized signature."
    )

    selected, _ = selected_boundaries(text)
    item_8 = next(
        candidate
        for candidate in selected
        if candidate["code"] == "Item_8"
    )

    assert item_8["position"] == text.index(
        "ITEM 8. FINANCIAL STATEMENTS"
    )
    assert boundary_confidence(item_8) == "high"
