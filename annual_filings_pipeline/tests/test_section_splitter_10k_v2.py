from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from section_splitter_10k_v2 import selected_boundaries, trimmed_span


def test_major_boundaries_are_monotonic_and_reconstruct_exact_text():
    substantive = "Substantive company-specific discussion. " * 80
    text = (
        "Table of Contents\n"
        "Item 1. Business 3\n"
        "Item 1A. Risk Factors 8\n"
        "Item 7. Management's Discussion and Analysis 30\n"
        "Item 8. Financial Statements 55\n\n"
        f"Item 1. Business\n{substantive}\n"
        f"Item 1A. Risk Factors\n{substantive}\n"
        f"Item 7. Management's Discussion and Analysis of Financial Condition and Results of Operations\n{substantive}\n"
        f"Item 8. Financial Statements and Supplementary Data\n{substantive}\n"
        "SIGNATURES\nAuthorized signature."
    )
    selected, _ = selected_boundaries(text)
    codes = [candidate["code"] for candidate in selected]
    assert {"Item_1", "Item_1A", "Item_7", "Item_8"}.issubset(codes)
    positions = [candidate["position"] for candidate in selected]
    assert positions == sorted(positions)
    assert positions[0] > text.index("Item 8. Financial Statements 55")
    for index, candidate in enumerate(selected):
        start = candidate["position"]
        end = selected[index + 1]["position"] if index + 1 < len(selected) else len(text)
        start, end, value = trimmed_span(text, start, end)
        assert value == text[start:end]
