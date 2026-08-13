from src.section_splitter_10k_v2 import selected_boundaries


def test_same_line_item_no_nomenclature_and_toc_rejection():
    substantive = "Company-specific substantive discussion. " * 90

    text = (
        "TABLE OF CONTENTS\n"
        "Item No. 1 | Description of Business | 1\n"
        "Item No. 1A | Risk Factors | 10\n"
        "Item No. 7 | Management Discussion and Analysis | 30\n"
        "Item No. 8 | Financial Statements and Supplementary Data | 50\n\n"
        f"Item No. 1\nDescription of Business\n{substantive}\n"
        f"Item No. 1A\nRisk Factors\n{substantive}\n"
        f"Item No. 7\nManagement Discussion and Analysis\n{substantive}\n"
        f"Item No. 8\nFinancial Statements and Supplementary Data\n"
        f"{substantive}\n"
        "SIGNATURES\nAuthorized signature."
    )

    selected, _ = selected_boundaries(text)
    by_code = {candidate["code"]: candidate for candidate in selected}

    assert {"Item_1", "Item_1A", "Item_7", "Item_8"}.issubset(by_code)
    assert by_code["Item_1"]["position"] > text.index(
        "Item No. 8 | Financial Statements"
    )
    assert by_code["Item_1"]["line"].startswith("Item No. 1")

    positions = [candidate["position"] for candidate in selected]
    assert positions == sorted(positions)


def test_renderer_variants_are_recognized_as_mandatory_boundaries():
    substantive = "Company-specific substantive discussion. " * 90
    text = (
        f"ITEM 1BUSINESS\n{substantive}\n"
        f"ITEM 1ARISK FACTORS\n{substantive}\n"
        f"ITEM 7MANAGEMENT’S DISCUSSION AND ANALYSIS OF "
        f"FINANCIAL CONDITION AND RESULTS OF OPERATIONS\n"
        f"{substantive}\n"
        f"ITEM 8FINANCIAL STATEMENTS AND SUPPLEMENTARY DATA\n"
        f"{substantive}\n"
        "SIGNATURES\nAuthorized signature."
    )

    selected, _ = selected_boundaries(text)
    by_code = {candidate["code"]: candidate for candidate in selected}

    assert {"Item_1", "Item_1A", "Item_7", "Item_8"}.issubset(by_code)
    assert by_code["Item_1"]["method"] == "observed_item_heading_variant"
    assert by_code["Item_1A"]["method"] == "observed_item_heading_variant"

    text = text.replace("ITEM 1BUSINESS", "ITEM I. BUSINESS")
    selected, _ = selected_boundaries(text)
    by_code = {candidate["code"]: candidate for candidate in selected}
    assert "Item_1" in by_code


def test_split_and_ocr_item_prefixes_are_recognized():
    substantive = "Company-specific substantive discussion. " * 90
    text = (
        f"ITEM 1. BUSINESS\n{substantive}\n"
        f"ITE M 1A. | RISK FACTORS\n{substantive}\n"
        f"ITEM 7. | MANAGEMENT ’ S DISCUSSION AND ANALYSIS OF "
        f"FINANCIAL CONDITION AND RESULTS OF OPERATIONS\n"
        f"{substantive}\n"
        f"ITEM 8. FINANCIAL STATEMENTS AND SUPPLEMENTARY DATA\n"
        f"{substantive}\n"
        "SIGNATURES\nAuthorized signature."
    )

    selected, _ = selected_boundaries(text)
    by_code = {candidate["code"]: candidate for candidate in selected}
    assert {"Item_1", "Item_1A", "Item_7", "Item_8"}.issubset(by_code)

    text = text.replace("ITE M 1A.", "ltem 1A.")
    selected, _ = selected_boundaries(text)
    by_code = {candidate["code"]: candidate for candidate in selected}
    assert "Item_1A" in by_code
