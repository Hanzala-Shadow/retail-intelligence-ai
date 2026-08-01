"""Regression cases for the deterministic reading-order recovery gate.

Every page here is on the DEVELOPMENT split of `esg_ai_gold_v1`. No holdout page
appears, and no threshold in `esg_order_safety` may be moved to satisfy one.

The cases are the ones named in the recovery brief:

* unsafe reconstructions must never be chosen (JWN p22, RL p83, CASY p30,
  WWW p64, BIRD p43, CROX p36);
* good current text must be preferred over a reconstruction (BRLT p28,
  UEIC p6, VRA p9, FOSL p42);
* a region reader should fix genuine interleaving (DLTR p29, PLCE p5);
* anything ambiguous stays held.

Two of these do not pass yet. `test_known_gaps_are_still_gaps` pins that fact
so the gap cannot be lost, and so that closing it shows up as a failing test
rather than as silence.

The companion file `test_esg_recovery_safety_labels.py` scores the same gate
against pages a reviewer looked at. These cases ask whether the gate picks the
right *reader*; those ask whether what it certified was safe to embed.
"""

from __future__ import annotations

import csv
import json
import unittest
from pathlib import Path

import pytest

import config
from esg_order_recovery import (
    OUTCOME_AMBIGUOUS,
    OUTCOME_CURRENT,
    OUTCOME_NONE,
    OUTCOME_REPARSE,
    PARSER_CURRENT,
    recover_reading_order,
)

RUN = Path(config.REPO_ROOT) / "outputs" / "esg_ai_gold_parser_20260731"
PAGES = RUN / "selected_page_text.jsonl"
INDEX = RUN / "parser_output" / "esg_parse_index.csv"

REJECT_RECONSTRUCTION = [
    "gold_019_JWN_p22",
    "gold_020_RL_p83",
    "gold_026_CASY_p30",
    "gold_032_WWW_p64",
    "gold_009_BIRD_p43",
    "gold_005_CROX_p36",
]
PREFER_CURRENT = [
    "gold_028_BRLT_p28",
    "gold_051_UEIC_p6",
    "gold_001_VRA_p9",
    "gold_031_FOSL_p42",
]
SHOULD_IMPROVE = ["gold_007_DLTR_p29", "gold_036_PLCE_p5"]


def _load():
    if not PAGES.exists() or not INDEX.exists():
        pytest.skip("isolated AI-gold parser run is not present in this checkout")
    pages = {json.loads(l)["item_id"]: json.loads(l) for l in PAGES.open(encoding="utf-8")}
    index = {r["pdf_file"]: r for r in csv.DictReader(INDEX.open(encoding="utf-8"))}
    return pages, index


def _recover(item_id: str):
    import pdfplumber

    pages, index = _load()
    row = pages[item_id]
    source = Path(config.REPO_ROOT) / index[row["pdf_file"]]["source_pdf"]
    with pdfplumber.open(source) as pdf:
        page = pdf.pages[int(row["page"]) - 1]
        words = (
            page.extract_words(
                use_text_flow=False, keep_blank_chars=False, extra_attrs=["size", "upright"]
            )
            or []
        )
        current = row["parser_text"]
        table_like = any(line.strip().startswith("|") for line in current.splitlines())
        return recover_reading_order(
            words, float(page.width), float(page.height), current, table_like=table_like
        )


@pytest.mark.slow
class RecoverySelectionTests(unittest.TestCase):
    """Opens real PDFs, so these run with the slow marker."""

    def test_unsafe_reconstructions_are_never_selected(self):
        for item in REJECT_RECONSTRUCTION:
            with self.subTest(page=item):
                result = _recover(item)
                # Either the page is held, or the current text was kept. What
                # must never happen is swapping good text for a bad reordering.
                self.assertIn(
                    result.outcome,
                    {OUTCOME_NONE, OUTCOME_AMBIGUOUS, OUTCOME_CURRENT},
                    f"{item} selected reconstruction {result.parser}",
                )
                if result.outcome == OUTCOME_CURRENT:
                    self.assertEqual(result.parser, PARSER_CURRENT)

    def test_good_current_text_is_never_replaced(self):
        for item in PREFER_CURRENT:
            with self.subTest(page=item):
                result = _recover(item)
                self.assertNotEqual(
                    result.outcome,
                    OUTCOME_REPARSE,
                    f"{item} would replace good current text with {result.parser}",
                )

    def test_ueic_p6_is_held_on_its_split_priority_cell(self):
        """This case used to assert the opposite, and the assertion was wrong.

        UEIC p6 is a three-column list of numbered priorities. Read across the
        rows -- which is right for two-line items and wrong for one -- it emits

            3. Product Safety & Quality 10. Employee Benefits &  21. Policy Influence
            4. Responsible Sourcing Compensation 22. Water Use

        so priority 10's label is split from the word that finishes it by two
        items belonging to other columns. The old gate certified the page
        because its structure checks had collapsed into a single region and
        could not fail (see the module docstring in `esg_order_safety`). The
        table check now sees that only 57% of that block's lines are whole
        rows and holds it, which is the fail-closed rule the brief asks for:
        table relationships not proven, so the page waits.

        No threshold was moved for this page; it is held by the same
        MIN_FULL_ROW_SHARE that every other table on the corpus meets or does
        not.
        """

        result = _recover("gold_051_UEIC_p6")
        self.assertNotEqual(result.outcome, OUTCOME_CURRENT)
        self.assertIn("table_rows_unproven", result.evaluations[PARSER_CURRENT].reason)

    def test_table_pages_are_refused_outright(self):
        for item in ("gold_001_VRA_p9", "gold_031_FOSL_p42", "gold_005_CROX_p36"):
            with self.subTest(page=item):
                result = _recover(item)
                self.assertNotEqual(result.outcome, OUTCOME_CURRENT)
                self.assertIn("not_grid", result.evaluations[PARSER_CURRENT].reason)

    def test_interleaved_current_text_is_detected(self):
        """DLTR p29 is the clearest interleaving case; the gate must see it."""
        result = _recover("gold_007_DLTR_p29")
        current = result.evaluations[PARSER_CURRENT]
        self.assertFalse(current.passed)
        self.assertIn("regions_blocked", current.reason)
        # The region reader is markedly cleaner on the same page.
        by_regions = result.evaluations["reconstruct_by_regions"]
        self.assertLess(
            by_regions.metrics["block_revisits"], current.metrics["block_revisits"] / 5
        )

    def test_known_gaps_are_still_gaps(self):
        """DLTR p29 and PLCE p5 are not recovered yet. Pinned deliberately.

        Both fail narrowly on the structural tolerances rather than on any
        safety property: DLTR p29's region reading leaves 2 block revisits
        against a budget of 1, and PLCE p5's leaves 9 column inversions. Moving
        those tolerances to pass two named pages would be fitting thresholds to
        individual examples, which is the failure mode already documented in
        reports/esg_reading_order_diagnosis_2026-07-31/. When the gap is closed
        properly this test should be updated, not deleted.

        The assertion is on the page still being held, not on which hold it
        gets. PLCE p5 moved from `held_no_safe_order` to `held_ambiguous_order`
        when real regions replaced the collapsed single bucket: both of its
        reconstructions now clear the structural checks and disagree with each
        other, so the page is held as ambiguous instead of as unreadable. That
        is a different description of the same gap, and both are fail-closed.
        """
        held = {OUTCOME_NONE, OUTCOME_AMBIGUOUS}
        for item in SHOULD_IMPROVE:
            with self.subTest(page=item):
                self.assertIn(_recover(item).outcome, held)


if __name__ == "__main__":
    unittest.main()
