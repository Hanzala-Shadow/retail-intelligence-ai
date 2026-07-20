from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import esg_reading_order
import pdf_parser


PAGE_WIDTH = 800.0
PAGE_HEIGHT = 500.0


def make_word(text: str, x0: float, top: float) -> dict:
    return {"text": text, "x0": x0, "x1": x0 + 14, "top": top, "bottom": top + 8}


def add_line(words: list[dict], prefix: str, x0: float, top: float, count: int = 6) -> None:
    for index in range(count):
        words.append(make_word(f"{prefix}_{index}", x0 + index * 18, top))


def two_column_words() -> list[dict]:
    """Twelve stable left/right prose rows: the layout reconstruction targets."""

    words: list[dict] = []
    for line in range(12):
        add_line(words, f"LEFT{line}", 50, 80 + line * 24)
        add_line(words, f"RIGHT{line}", 470, 80 + line * 24)
    return words


def row_order_text(words: list[dict]) -> str:
    """Render words the way a PDF extractor does: visual rows, left to right.

    For a two-column page this is precisely the damaged order the coordinate
    reconstruction exists to repair, so it is what the fake page must emit.
    """

    rows: dict[int, list[dict]] = {}
    for word in words:
        rows.setdefault(round(float(word["top"])), []).append(word)
    return "\n".join(
        " ".join(word["text"] for word in sorted(rows[top], key=lambda item: item["x0"]))
        for top in sorted(rows)
    )


class FakePage:
    def __init__(
        self,
        words: list[dict] | None = None,
        text: str | None = None,
        rects: int = 0,
        images: int = 0,
        curves: int = 0,
    ) -> None:
        self._words = words or []
        self._text = row_order_text(self._words) if text is None else text
        self.width = PAGE_WIDTH
        self.height = PAGE_HEIGHT
        self.rects = [{}] * rects
        self.images = [{}] * images
        self.curves = [{}] * curves

    def extract_text_simple(self) -> str:
        return self._text

    def extract_words(self, **kwargs) -> list[dict]:
        return list(self._words)

    def find_tables(self) -> list:
        return []

    def flush_cache(self) -> None:
        return None


class FakePDF:
    def __init__(self, pages: list[FakePage]) -> None:
        self.pages = pages
        self._pages = pages

    def __enter__(self) -> "FakePDF":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


def parse_pages(pages: list[FakePage], **kwargs):
    with mock.patch.object(pdf_parser.pdfplumber, "open", return_value=FakePDF(pages)):
        return pdf_parser.PDFParser().parse(Path("fake.pdf"), company="TEST", **kwargs)


def repaired(doc) -> list[int]:
    value = doc.reading_order_repaired_pages
    return [int(page) for page in value.split(";") if page]


def unresolved(doc) -> list[int]:
    value = doc.reading_order_unresolved_pages
    return [int(page) for page in value.split(";") if page]


class DecorativePageReconstructionTests(unittest.TestCase):
    def test_decorative_page_above_the_old_visual_object_gate_now_reconstructs(self) -> None:
        # 60 visual objects tripped the retired raw structural gate. This page is
        # ordinary two-column prose with decorative artwork and no grid signature.
        page = FakePage(two_column_words(), rects=60)
        metrics = pdf_parser.page_layout_grid_metrics(page, page.extract_text_simple(), page._words)

        self.assertGreaterEqual(metrics["visual_objects"], 60)
        self.assertFalse(pdf_parser.layout_grid_risk_from_metrics(metrics))

        doc = parse_pages([page])

        self.assertEqual(repaired(doc), [1])
        self.assertEqual(doc.parser_policy, pdf_parser.AUTO_PDFPLUMBER_COLUMN_POLICY)
        self.assertIn("_column_order", doc.parser_used)
        self.assertEqual(doc.page_text_change_reasons, "1:coordinate_column_order")
        # The repair is a real reordering: every left-column word precedes the
        # first right-column word, which the extractor's row order interleaves.
        self.assertLess(doc.raw_text.index("LEFT11_0"), doc.raw_text.index("RIGHT0_0"))

    def test_complete_grid_signature_still_refuses_reconstruction(self) -> None:
        words = two_column_words()
        # Three repeated left edges plus wide gutters and metric text: the full
        # table/grid signature, which must stay fail-closed.
        for index in range(3):
            add_line(words, f"MID{index}", 150, 400 + index * 12, count=2)
            add_line(words, f"TAIL{index}", 250, 440 + index * 12, count=2)
        grid_text = "\n".join(
            ["Scope 1 emissions 12.5 tons", "Scope 2 intensity 3.2 million"]
            + [f"Row {index} label" for index in range(14)]
        )
        page = FakePage(words, text=grid_text, rects=10)
        metrics = pdf_parser.page_layout_grid_metrics(page, grid_text, words)

        self.assertTrue(pdf_parser.layout_grid_risk_from_metrics(metrics))

        doc = parse_pages([page])

        self.assertEqual(repaired(doc), [])
        self.assertEqual(unresolved(doc), [1])
        self.assertEqual(doc.page_text_change_reasons, "")
        self.assertEqual(doc.layout_risk_pages, "1")

    def test_navigation_contents_page_still_refuses_reconstruction(self) -> None:
        words = two_column_words()
        words.append(make_word("Contents", 50, 40))
        for index in range(8):
            words.append(make_word(str(index + 1), 60, 55 + index * 10))
        page = FakePage(words, rects=60)

        doc = parse_pages([page])

        self.assertEqual(repaired(doc), [])
        self.assertEqual(unresolved(doc), [1])

    def test_ordinary_full_width_prose_is_not_reordered(self) -> None:
        words: list[dict] = []
        for line in range(12):
            add_line(words, f"BODY{line}", 50, 80 + line * 24)
        page = FakePage(words, rects=60)

        doc = parse_pages([page])

        self.assertEqual(repaired(doc), [])
        self.assertEqual(unresolved(doc), [])
        self.assertEqual(doc.page_text_change_reasons, "")
        self.assertEqual(doc.raw_text.strip(), page.extract_text_simple().strip())

    def test_preservation_ratio_floor_still_refuses_lossy_reconstruction(self) -> None:
        words = two_column_words()
        # A word carrying its own space cannot survive the token-preservation
        # check, which is exactly what the floor exists to catch.
        for index in range(3):
            words.append(make_word("Net Zero", 50 + index * 18, 400))

        result = esg_reading_order.reconstruct_column_order(words, PAGE_WIDTH, PAGE_HEIGHT)

        self.assertEqual(result.status, "ambiguous")
        self.assertTrue(result.reason.startswith("word_preservation_ratio="))
        self.assertLess(result.preservation_ratio, esg_reading_order.MIN_PRESERVATION_RATIO)


class PerPageTextLayerFallbackTests(unittest.TestCase):
    """A broken page must not cost its document every column repair."""

    CID_NATIVE = "Metric value (cid:20)(cid:23)7 text. " * 20
    CID_CLEAN = "Metric value 147 readable recovered text for the page. " * 20

    def build_document(self) -> list[FakePage]:
        pages = [FakePage(two_column_words(), rects=60) for _ in range(2)]
        pages.append(FakePage(text=self.CID_NATIVE))
        pages.extend(FakePage(two_column_words(), rects=60) for _ in range(3))
        return pages

    def fake_pdfium(self, pages: list[FakePage]):
        # Snapshot the text now: the parser releases each page object as it goes,
        # exactly as pdfplumber's cache clearing does.
        texts = [
            (number, self.CID_CLEAN if number == 3 else page.extract_text_simple())
            for number, page in enumerate(pages, start=1)
        ]

        def _extract(file_path, log_pages=False, company=None):
            return list(texts)

        return _extract

    def test_per_page_fallback_replaces_only_the_broken_page(self) -> None:
        pages = self.build_document()
        with mock.patch.object(
            pdf_parser, "extract_with_pdfium", self.fake_pdfium(pages)
        ):
            doc = parse_pages(pages)

        # Page 3 was the only page whose own text failed; the other five keep
        # their pdfplumber text and their coordinate repairs.
        self.assertEqual(doc.text_layer_fallback_pages, "3")
        self.assertEqual(repaired(doc), [1, 2, 4, 5, 6])
        self.assertEqual(doc.parser_policy, pdf_parser.AUTO_TEXT_LAYER_FALLBACK_POLICY)
        self.assertIn("pypdfium_text_pages", doc.parser_used)
        self.assertIn("_column_order", doc.parser_used)
        self.assertIn("pypdfium_text_pages=1", doc.parser_reason)

        self.assertIn("3:pdfium_text_page", doc.page_text_change_reasons)
        self.assertIn("1:coordinate_column_order", doc.page_text_change_reasons)

        # The recovered page carries PDFium text and no CID artifacts survive.
        self.assertIn("Metric value 147 readable", doc.raw_text)
        self.assertEqual(pdf_parser.count_cid_artifacts(doc.raw_text), 0)
        # The surviving repairs are real: left column still precedes right.
        self.assertLess(doc.raw_text.index("LEFT11_0"), doc.raw_text.index("RIGHT0_0"))

    def test_healthy_document_never_calls_the_fallback(self) -> None:
        pages = [FakePage(two_column_words(), rects=60) for _ in range(6)]
        with mock.patch.object(pdf_parser, "extract_with_pdfium") as extract:
            doc = parse_pages(pages)

        extract.assert_not_called()
        self.assertEqual(doc.text_layer_fallback_pages, "")
        self.assertEqual(repaired(doc), [1, 2, 3, 4, 5, 6])

    def test_thinly_broken_text_layer_replaces_every_page(self) -> None:
        # No page trips the defect test -- each clears MIN_PAGE_CHARS with no CID
        # or garbled text -- yet PDFium recovers far more of every one. Each page
        # fails the per-page comparison on its own, so the merge covers the whole
        # document without needing a document-wide branch.
        pages = [FakePage(text="Sparse page text, barely there") for _ in range(6)]

        def _extract(file_path, log_pages=False, company=None):
            return [(number, "Recovered sustainability narrative. " * 60) for number in range(1, 7)]

        with mock.patch.object(pdf_parser, "extract_with_pdfium", _extract):
            doc = parse_pages(pages)

        self.assertEqual(doc.text_layer_fallback_pages, "1;2;3;4;5;6")
        self.assertEqual(doc.parser_policy, pdf_parser.AUTO_TEXT_LAYER_FALLBACK_POLICY)
        self.assertIn("pypdfium_text_pages=6", doc.parser_reason)
        self.assertIn("Recovered sustainability narrative.", doc.raw_text)

    def test_one_unimprovable_page_does_not_cost_the_document_its_repairs(self) -> None:
        """The BBY-2021 shape: a single CID artifact in a healthy document.

        PDFium reads that page no better than pdfplumber, so nothing is replaced
        and every other page keeps its coordinate repair. Replacing the document
        here is what discarded 97 healthy pages of extraction.
        """
        pages = [FakePage(two_column_words(), rects=60) for _ in range(5)]
        damaged = FakePage(text="Metric value (cid:20) in otherwise readable text. " * 24)
        pages.insert(2, damaged)

        def _extract(file_path, log_pages=False, company=None):
            # Marginally worse on the damaged page, and no better anywhere else.
            return [(number, "Metric value in otherwise readable text. " * 24) for number in range(1, 7)]

        with mock.patch.object(pdf_parser, "extract_with_pdfium", _extract):
            doc = parse_pages(pages)

        self.assertEqual(doc.text_layer_fallback_pages, "")
        self.assertEqual(repaired(doc), [1, 2, 4, 5, 6])
        self.assertEqual(doc.parser_policy, pdf_parser.AUTO_PDFPLUMBER_COLUMN_POLICY)
        self.assertIn("pypdfium_text_no_page_improved", doc.parser_reason)
        self.assertLess(doc.raw_text.index("LEFT11_0"), doc.raw_text.index("RIGHT0_0"))


class ResumePolicyVersionTests(unittest.TestCase):
    def test_legacy_unversioned_text_layer_rows_are_reprocessed(self) -> None:
        # These rows replaced whole documents from PDFium and carry none of the
        # coordinate repairs that apply to the rest of the document.
        self.assertFalse(
            pdf_parser._parser_policy_matches_request(
                {"parser_policy": pdf_parser.LEGACY_TEXT_LAYER_FALLBACK_POLICY},
                expected_parser_policy=pdf_parser.AUTO_PDFPLUMBER_COLUMN_POLICY,
                auto_layout_pdfium=False,
            )
        )

    def test_versioned_text_layer_rows_are_resumed(self) -> None:
        self.assertTrue(
            pdf_parser._parser_policy_matches_request(
                {"parser_policy": pdf_parser.AUTO_TEXT_LAYER_FALLBACK_POLICY},
                expected_parser_policy=pdf_parser.AUTO_PDFPLUMBER_COLUMN_POLICY,
                auto_layout_pdfium=False,
            )
        )

    def test_v1_coordinate_rows_are_reprocessed(self) -> None:
        self.assertFalse(
            pdf_parser._parser_policy_matches_request(
                {"parser_policy": "auto_pdfplumber_column_order_v1"},
                expected_parser_policy=pdf_parser.AUTO_PDFPLUMBER_COLUMN_POLICY,
                auto_layout_pdfium=False,
            )
        )

    def test_legacy_and_versioned_layout_grid_rows_both_reprocess_in_report_only_mode(
        self,
    ) -> None:
        for policy in (
            pdf_parser.LEGACY_LAYOUT_GRID_FALLBACK_POLICY,
            pdf_parser.AUTO_LAYOUT_GRID_FALLBACK_POLICY,
        ):
            with self.subTest(policy=policy):
                self.assertFalse(
                    pdf_parser._parser_policy_matches_request(
                        {"parser_policy": policy},
                        expected_parser_policy="",
                        auto_layout_pdfium=False,
                    )
                )


if __name__ == "__main__":
    unittest.main()
