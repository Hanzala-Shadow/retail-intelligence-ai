from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import validate_esg_provenance as provenance


def test_scope_matches_parse_and_index_rows() -> None:
    assert provenance.row_matches_scope(
        {"ticker": "AAP", "source_pdf": "data/AAP/report-2024.pdf"},
        "aap",
        "report-2024.pdf",
    )
    assert provenance.row_matches_scope(
        {"ticker": "AAP", "pdf_stem": "report-2024"},
        "AAP",
        "report-2024",
    )
    assert not provenance.row_matches_scope(
        {"ticker": "AMZN", "pdf_stem": "report-2024"},
        "AAP",
        "report-2024",
    )
