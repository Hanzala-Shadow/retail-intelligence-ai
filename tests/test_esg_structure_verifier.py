"""Unit tests for the deterministic structure-extraction verifier (v2).

All pages are synthetic word lists; no PDFs, no network. Each gate gets a
passing and a failing case, and the v2 amendments (header plausibility,
qualifier provenance, self-containment grade) carry named regression tests
for the failure class that motivated them: a record that passes anchoring,
numeric exactness, and row-band coherence while citing an adjacent data
value as its qualifier.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import esg_structure_verifier as verifier  # noqa: E402


def word(idx, text, x0, x1, top, bottom):
    return {"idx": idx, "text": text, "x0": x0, "x1": x1, "top": top, "bottom": bottom}


def page(page_id, words):
    return {"page_id": page_id, "words": words}


def card_record(**overrides):
    """A clean stat-card record: label above value inside one card bbox."""
    record = {
        "page_id": "P1",
        "record_id": "r1",
        "record_type": "stat_card",
        "metric_label_idx": [1],
        "value_idx": [2],
        "unit_idx": [],
        "qualifier_idx": [],
        "coherence_mode": "card_bbox",
        "card_bbox": [5.0, 5.0, 80.0, 45.0],
    }
    record.update(overrides)
    return record


@pytest.fixture
def card_page():
    return page(
        "P1",
        [
            word(1, "Water", 10, 40, 10, 20),
            word(2, "32%", 10, 30, 25, 35),
            word(3, "reduction", 32, 70, 25, 35),
            word(4, "elsewhere", 300, 340, 500, 510),
        ],
    )


# --- token anchoring ---------------------------------------------------------


def test_anchoring_passes_on_valid_indices(card_page):
    widx = verifier.words_by_idx(card_page)
    reasons, _ = verifier.check_token_anchoring(card_record(), widx)
    assert reasons == []


def test_anchoring_fails_on_unknown_index(card_page):
    widx = verifier.words_by_idx(card_page)
    reasons, _ = verifier.check_token_anchoring(card_record(value_idx=[99]), widx)
    assert any("not on this page's word list" in r for r in reasons)


def test_anchoring_fails_on_role_collision(card_page):
    widx = verifier.words_by_idx(card_page)
    reasons, _ = verifier.check_token_anchoring(
        card_record(metric_label_idx=[1], qualifier_idx=[1]), widx
    )
    assert any("used in both" in r for r in reasons)


def test_anchoring_fails_without_value(card_page):
    widx = verifier.words_by_idx(card_page)
    reasons, _ = verifier.check_token_anchoring(card_record(value_idx=[]), widx)
    assert any("no value_idx" in r for r in reasons)


# --- literal numeric grammar -------------------------------------------------


def test_numeric_exactness_accepts_literal_value(card_page):
    widx = verifier.words_by_idx(card_page)
    assert verifier.check_numeric_exactness(card_record(), widx) == []


def test_numeric_exactness_accepts_split_token_value():
    # pdfplumber can split one printed number across adjacent word tokens.
    data = page("P1", [word(1, "3", 10, 15, 25, 35), word(2, "2%", 16, 30, 25, 35)])
    widx = verifier.words_by_idx(data)
    record = card_record(metric_label_idx=[], value_idx=[1, 2], card_bbox=[5, 5, 80, 45])
    assert verifier.check_numeric_exactness(record, widx) == []


def test_numeric_exactness_rejects_prose(card_page):
    widx = verifier.words_by_idx(card_page)
    reasons = verifier.check_numeric_exactness(card_record(value_idx=[3]), widx)
    assert any("non-numeric" in r for r in reasons)


# --- spatial coherence -------------------------------------------------------


def test_card_bbox_coherence_passes_inside(card_page):
    widx = verifier.words_by_idx(card_page)
    assert verifier.check_spatial_coherence(card_record(), widx) == []


def test_card_bbox_coherence_fails_outside(card_page):
    widx = verifier.words_by_idx(card_page)
    reasons = verifier.check_spatial_coherence(card_record(metric_label_idx=[4]), widx)
    assert any("outside declared" in r for r in reasons)


def test_row_band_requires_mutual_y_overlap():
    data = page(
        "P1",
        [word(1, "Scope", 10, 40, 10, 20), word(2, "1,204", 200, 230, 10, 20),
         word(3, "misplaced", 10, 60, 60, 70)],
    )
    widx = verifier.words_by_idx(data)
    ok = card_record(coherence_mode="row_band", card_bbox=None, metric_label_idx=[1], value_idx=[2])
    assert verifier.check_spatial_coherence(ok, widx) == []
    bad = card_record(coherence_mode="row_band", card_bbox=None, metric_label_idx=[3], value_idx=[2])
    assert verifier.check_spatial_coherence(bad, widx) != []


# --- v2: header plausibility and qualifier provenance ------------------------


def table_page():
    """A two-column table: temporal headers, one label column, one data row.

    Word 20/21 are the FY22/FY23 headers; 30 is the row label; 31/32 the row
    values. Word 40 is a parenthesized accounting figure sitting in the value
    y-band — the wrong-qualifier bait.
    """
    return page(
        "T",
        [
            word(20, "FY22", 100, 130, 10, 20),
            word(21, "FY23", 200, 230, 10, 20),
            word(30, "Energy", 10, 60, 30, 40),
            word(31, "512", 100, 125, 30, 40),
            word(32, "498", 200, 225, 30, 40),
            word(40, "(0.84)", 300, 330, 30, 40),
        ],
    )


def table_records(qualifier_a=(20,), qualifier_b=(21,)):
    base = dict(
        page_id="T", record_type="table_cell", coherence_mode="row_band",
        table_id="tbl1", metric_label_idx=[30], unit_idx=[],
    )
    rec_a = dict(base, record_id="a", value_idx=[31], col_idx=0, qualifier_idx=list(qualifier_a))
    rec_b = dict(base, record_id="b", value_idx=[32], col_idx=1, qualifier_idx=list(qualifier_b))
    return [rec_a, rec_b]


def test_temporal_headers_are_label_like():
    assert verifier.is_label_like("FY23")
    assert verifier.is_data_like("(0.84)")
    assert not verifier.is_data_like("FY23")


def test_header_plausibility_accepts_temporal_headers():
    records = table_records()
    context = verifier.build_table_context(records, {"T": table_page()})
    assert context[("T", "tbl1")]["plausible"]
    assert verifier.check_header_plausibility(records[0], context) == []


def test_header_plausibility_rejects_data_like_header():
    # The motivating failure class: an accounting figure proposed as a header.
    records = table_records(qualifier_a=(40,), qualifier_b=(40,))
    context = verifier.build_table_context(records, {"T": table_page()})
    assert not context[("T", "tbl1")]["plausible"]
    assert verifier.check_header_plausibility(records[0], context) != []


def test_qualifier_in_value_band_is_rejected():
    # Record a's qualifier is word 40, which y-overlaps record b's value band.
    records = table_records(qualifier_a=(40,))
    widx = verifier.words_by_idx(table_page())
    context = verifier.build_table_context(records, {"T": table_page()})
    reasons = verifier.check_qualifier_provenance(records[0], widx, records, context)
    assert any("value band" in r for r in reasons)


def test_self_containment_grades():
    records = table_records()
    context = verifier.build_table_context(records, {"T": table_page()})
    assert verifier.qualifier_grade(records[0], [], context) == ("full", "header_sourced")
    bare = card_record(qualifier_idx=[])
    assert verifier.qualifier_grade(bare, [], {}) == ("bare", "bare")
    misprov = table_records(qualifier_a=(40,))[0]
    assert verifier.qualifier_grade(misprov, ["reason"], context) == ("partial", "misprovenanced")


# --- OCR-lineage exclusion ---------------------------------------------------


def test_ocr_lineage_documents_are_refused(tmp_path):
    manifest = pd.DataFrame(
        [{"ticker": "NGVC", "pdf_stem": "NGVC-NATURAL GROCERS-2021"}]
    )
    manifest.to_csv(tmp_path / "manifest.csv", index=False)
    with pytest.raises(AssertionError, match="NGVC-2021"):
        verifier.assert_ocr_exclusion(tmp_path)


# --- end to end --------------------------------------------------------------


def test_run_verifier_end_to_end(tmp_path, card_page):
    input_dir = tmp_path / "in"
    (input_dir / "wordlists").mkdir(parents=True)
    (input_dir / "wordlists" / "P1.json").write_text(
        json.dumps(card_page), encoding="utf-8"
    )
    records = [
        card_record(),
        card_record(record_id="r2", value_idx=[3]),  # prose as value: must fail
    ]
    with open(input_dir / "records.jsonl", "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")

    output_dir = tmp_path / "out"
    verifier.run_verifier(input_dir, output_dir)

    results = pd.read_csv(output_dir / "verifier_results.csv")
    assert len(results) == 2
    by_id = results.set_index("record_id")
    assert bool(by_id.loc["r1", "pass"])
    assert not bool(by_id.loc["r2", "pass"])
    assert by_id.loc["r1", "self_containment_grade"] == "bare"

    recon = pd.read_csv(output_dir / "page_reconciliation.csv")
    assert recon.loc[0, "numeric_tokens_on_page"] == 1  # only "32%"
    assert recon.loc[0, "consumed_by_passing_records"] == 1
