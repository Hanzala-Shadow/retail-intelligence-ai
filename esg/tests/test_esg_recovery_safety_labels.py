"""The recovery gate, scored against real pages a human actually looked at.

``test_esg_order_recovery`` pins the gate's behaviour on development gold
pages, where the label says what the *text* should be. These cases come from
``data/00_reference/esg_recovery_safety_labels.csv`` instead, where the label
says whether the text the gate certified was safe to embed -- which is the
question the gate is actually answering, and the one it got wrong six times out
of twelve in the first Terra sample.

Two properties, in priority order:

* **no unsafe page may be recovered.** This is absolute. A page a reviewer
  found interleaved must stay held, whatever it costs in coverage.
* **most safe controls should survive.** This is a floor, not a target. It
  exists so that a future "fix" cannot buy safety by holding the whole corpus,
  and it is deliberately below the count of safe labels: two of the thirty-one are
  held today for reasons recorded in ``HELD_SAFE_CONTROLS``.

Every case re-hashes the PDF and the parser text before scoring. A label
describes one exact page as it stood when it was reviewed; if the bytes have
moved underneath it the label no longer means anything, and these tests fail
rather than report a verdict about a page nobody reviewed.
"""

from __future__ import annotations

import csv
import hashlib
import unittest
from pathlib import Path

import pytest

import config
from esg_order_recovery import OUTCOME_CURRENT, PARSER_CURRENT, recover_reading_order
from esg_order_safety import has_full_page_image, has_wide_content_image
from esg_layout_qa import _column_metrics
from esg_page_role import classify_page_role

LABELS = Path(config.REFERENCE_DIR) / "esg_recovery_safety_labels.csv"

#: Of the thirty-one pages the reviewer called safe, this many must still be kept.
#: Set below thirty-one deliberately: see HELD_SAFE_CONTROLS.
MIN_SAFE_CONTROLS_KEPT = 29

#: Safe controls the gate holds anyway, with the check that holds them. Pinned
#: so that recovering one shows up as a failing test to be looked at, rather
#: than as silence -- and so the cost of the current rules stays visible.
HELD_SAFE_CONTROLS = {
    # Its four vendor-diversity panels repeat one row-key column on both sides
    # of a gutter, which is the same geometry as the VZ p19 merged tables the
    # reviewer called unsafe. Nothing deterministic separates the two, so both
    # are held.
    "recovery_review_008_GROV_p53": "table_parallel",
    # SASB disclosure table whose cells wrap over several lines each. Too few
    # of its lines are whole rows for a row-major read to be provable, so its
    # wrapped cells are judged as prose panels and come out interleaved.
    "recovery_review_010_HD_p105": "regions_blocked",
}


def _rows() -> list[dict[str, str]]:
    if not LABELS.exists():
        pytest.skip(f"recovery safety labels are not present: {LABELS}")
    with LABELS.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _repo(path: str) -> Path:
    return Path(config.REPO_ROOT) / path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _current_text(row: dict[str, str]) -> str:
    """The exact page text the reviewer compared against the image.

    Sliced out of the parsed text file with the page map, the same way the
    sampler did, so that a reparse which shifts every offset is caught by the
    hash check rather than silently scoring a different page.
    """

    text_path, map_path = _repo(row["parsed_text_file"]), _repo(row["page_map_file"])
    if not text_path.exists() or not map_path.exists():
        pytest.skip(f"parsed text for {row['item_id']} is not in this checkout")
    with map_path.open(newline="", encoding="utf-8-sig") as handle:
        pages = {int(item["page"]): item for item in csv.DictReader(handle)}
    page = pages.get(int(row["page"]))
    if not page:
        raise AssertionError(f"{row['item_id']}: page map has no row for this page")
    text = text_path.read_text(encoding="utf-8")
    return text[int(page["char_start"]) : int(page["char_end"])]


def _recover(row: dict[str, str]):
    import pdfplumber

    source = _repo(row["source_pdf"])
    if not source.exists():
        pytest.skip(f"source PDF for {row['item_id']} is not in this checkout")
    current = _current_text(row)
    with pdfplumber.open(source) as pdf:
        page = pdf.pages[int(row["page"]) - 1]
        words = (
            page.extract_words(
                use_text_flow=False, keep_blank_chars=False, extra_attrs=["size", "upright"]
            )
            or []
        )
        table_like = any(line.strip().startswith("|") for line in current.splitlines())
        visual_object_count = (
            len(getattr(page, "rects", []) or [])
            + len(getattr(page, "images", []) or [])
            + min(len(getattr(page, "curves", []) or []), 25)
        )
        layout = _column_metrics(words, float(page.width), visual_object_count)
        return recover_reading_order(
            words,
            float(page.width),
            float(page.height),
            current,
            table_like=table_like,
            visual_object_count=visual_object_count,
            mixed_column_lines=int(layout["mixed_column_lines"]),
            full_page_image=has_full_page_image(
                list(getattr(page, "images", []) or []),
                float(page.width),
                float(page.height),
            ),
            wide_content_image=has_wide_content_image(
                list(getattr(page, "images", []) or []),
                float(page.width),
                float(page.height),
            ),
        )


class LabelProvenanceTests(unittest.TestCase):
    """A stale label is worse than no label, so these fail rather than skip."""

    def test_labels_are_well_formed(self):
        rows = _rows()
        self.assertTrue(rows, "label file is empty")
        verdicts = {row["expected_verdict"] for row in rows}
        self.assertTrue(verdicts <= {"safe", "unsafe"}, f"unexpected verdicts: {verdicts}")
        ids = [row["item_id"] for row in rows]
        self.assertEqual(len(ids), len(set(ids)), "duplicate item_id in label file")
        for row in rows:
            with self.subTest(page=row["item_id"]):
                self.assertEqual(len(row["source_sha256"]), 64)
                self.assertEqual(len(row["current_text_sha256"]), 64)
                self.assertEqual(len(row["image_sha256"]), 64)
                if row["expected_verdict"] == "unsafe":
                    self.assertTrue(
                        row["issue_codes"], f"{row['item_id']} is unsafe with no issue code"
                    )

    def test_held_safe_controls_are_all_labelled_safe(self):
        labelled = {r["item_id"]: r["expected_verdict"] for r in _rows()}
        for item_id in HELD_SAFE_CONTROLS:
            with self.subTest(page=item_id):
                self.assertEqual(
                    labelled.get(item_id),
                    "safe",
                    f"{item_id} is pinned as a held safe control but is not labelled safe",
                )

    def test_minimum_kept_is_below_the_safe_control_count(self):
        safe = sum(1 for row in _rows() if row["expected_verdict"] == "safe")
        self.assertLessEqual(
            MIN_SAFE_CONTROLS_KEPT,
            safe,
            "the coverage floor cannot exceed the number of safe labels",
        )

    @pytest.mark.slow
    def test_source_pdfs_still_match_their_labels(self):
        for row in _rows():
            with self.subTest(page=row["item_id"]):
                source = _repo(row["source_pdf"])
                if not source.exists():
                    self.skipTest("source PDF is not in this checkout")
                self.assertEqual(
                    _sha256_file(source),
                    row["source_sha256"],
                    f"{row['item_id']}: source PDF changed since it was reviewed; "
                    "the label no longer describes this page",
                )

    @pytest.mark.slow
    def test_current_text_still_matches_its_label(self):
        for row in _rows():
            with self.subTest(page=row["item_id"]):
                digest = hashlib.sha256(_current_text(row).encode("utf-8")).hexdigest()
                self.assertEqual(
                    digest,
                    row["current_text_sha256"],
                    f"{row['item_id']}: parser text changed since it was reviewed; "
                    "re-render and re-review before trusting this label",
                )

    @pytest.mark.slow
    def test_rendered_images_still_match_their_labels(self):
        for row in _rows():
            with self.subTest(page=row["item_id"]):
                image = _repo(row["image_path"])
                if not image.exists():
                    self.skipTest("rendered review image is not in this checkout")
                self.assertEqual(
                    _sha256_file(image),
                    row["image_sha256"],
                    f"{row['item_id']}: the reviewed image changed on disk",
                )


@pytest.mark.slow
class RecoverySafetyTests(unittest.TestCase):
    """Opens real PDFs, so these run with the slow marker."""

    def test_no_unsafe_page_is_retrievable(self):
        """Absolute. Coverage never justifies embedding a page like these."""

        for row in _rows():
            if row["expected_verdict"] != "unsafe":
                continue
            with self.subTest(page=row["item_id"]):
                result = _recover(row)
                if result.outcome == OUTCOME_CURRENT:
                    role = classify_page_role(_current_text(row))
                    self.assertTrue(
                        role.is_navigation,
                        f"{row['item_id']} was certified on its current text, but the "
                        f"reviewer found it unsafe: {row['evidence']}",
                    )

    def test_enough_safe_controls_are_kept(self):
        kept = [
            row["item_id"]
            for row in _rows()
            if row["expected_verdict"] == "safe"
            and _recover(row).outcome == OUTCOME_CURRENT
        ]
        self.assertGreaterEqual(
            len(kept),
            MIN_SAFE_CONTROLS_KEPT,
            f"only {len(kept)} safe controls kept ({sorted(kept)}); the gate is "
            "holding too much to be worth running",
        )

    def test_the_held_safe_controls_are_the_known_ones(self):
        """Pins the cost. Recovering one of these should be noticed, not silent."""

        for row in _rows():
            if row["expected_verdict"] != "safe":
                continue
            with self.subTest(page=row["item_id"]):
                result = _recover(row)
                held = result.outcome != OUTCOME_CURRENT
                expected = row["item_id"] in HELD_SAFE_CONTROLS
                if held and not expected:
                    self.fail(
                        f"{row['item_id']} is newly held: "
                        f"{result.evaluations[PARSER_CURRENT].reason}"
                    )
                if expected and not held:
                    self.fail(
                        f"{row['item_id']} is now recovered; if that is right, drop it "
                        "from HELD_SAFE_CONTROLS and raise MIN_SAFE_CONTROLS_KEPT"
                    )
                if held:
                    self.assertIn(
                        HELD_SAFE_CONTROLS[row["item_id"]],
                        result.evaluations[PARSER_CURRENT].reason,
                        "held for a different reason than the one recorded",
                    )


if __name__ == "__main__":
    unittest.main()
