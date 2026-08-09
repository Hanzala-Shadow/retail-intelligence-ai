from pathlib import Path
import sys

import tiktoken

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from chunker_v2 import embedding_text, group_units, semantic_units, token_count


def test_semantic_chunks_preserve_source_substrings_and_metadata():
    text = (
        "Revenue Trends\n\n"
        + "Net sales increased because comparable-store demand improved. " * 20
        + "\n\nLiquidity\n\n"
        + "Cash generated from operations funded inventory and capital expenditures. " * 20
    )
    encoder = tiktoken.get_encoding("cl100k_base")
    units = semantic_units(text)
    groups = group_units(
        text,
        units,
        encoder,
        target=120,
        hard_max=180,
        overlap=25,
    )
    assert len(groups) >= 2
    for group in groups:
        value = text[group[0].start : group[-1].end]
        assert value in text
        assert token_count(encoder, value) <= 180
    embedded = embedding_text(
        "Example Retailer",
        "EXM",
        2025,
        "Item_7",
        "Revenue Trends",
        "narrative",
        "Net sales increased.",
    )
    assert "Fiscal year: FY2025" in embedded
    assert "SEC section: Item 7" in embedded
    assert embedded.endswith("Net sales increased.")
