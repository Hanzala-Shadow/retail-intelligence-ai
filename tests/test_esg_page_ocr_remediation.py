from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pipeline_ocr_remediation_stage as remediation


def test_replacement_character_anywhere_in_page_is_detected() -> None:
    text = ("clean sustainability evidence " * 300) + "refinersï¿½standards"
    assert "replacement_character" in remediation.detect_page_quality(text)


def test_changed_page_regenerates_document_and_page_map_once() -> None:
    pages = {1: "clean opening page", 2: "(cid:12) broken", 3: "clean final page"}
    calls: list[int] = []
    final, outcomes = remediation.remediate_page_texts(
        pages, lambda page: calls.append(page) or ("Verified readable climate progress and emissions evidence." * 3)
    )
    text, page_map = remediation.regenerate_document(final)
    assert calls == [2]
    assert outcomes[0]["action"] == "approved_page_override"
    assert len(page_map) == 3
    assert text[page_map[1]["char_start"] : page_map[1]["char_end"]] == final[2].strip()


def test_failed_ocr_preserves_old_text_and_holds_for_review() -> None:
    pages = {1: "(cid:2) broken source"}
    def fail(_: int) -> str:
        raise RuntimeError("engine unavailable")
    final, outcomes = remediation.remediate_page_texts(pages, fail)
    assert final == pages
    assert outcomes[0]["action"] == "manual_review_hold"
    assert outcomes[0]["verification"] == "ocr_failed"


def test_page_override_never_duplicates_whole_page_across_chunks() -> None:
    pages = {1: "(cid:2) broken", 2: "clean second page"}
    final, _ = remediation.remediate_page_texts(
        pages, lambda _: "Unique verified first-page evidence about Scope 1 emissions." * 3
    )
    text, page_map = remediation.regenerate_document(final)
    first = text[page_map[0]["char_start"] : page_map[0]["char_end"]]
    second = text[page_map[1]["char_start"] : page_map[1]["char_end"]]
    assert first != second
    assert "Unique verified" not in second


def test_override_index_records_why_ocr_was_rejected() -> None:
    """The reject reason is the diagnostic the removed run log used to carry.

    remediate_page_texts must surface it in `note`, because esg_page_ocr_overrides.csv
    is now the only place it is written.
    """
    pages = {1: "(cid:2) broken source"}
    def fail(_: int) -> str:
        raise RuntimeError("engine unavailable")
    _, outcomes = remediation.remediate_page_texts(pages, fail)
    assert outcomes[0]["note"] == "engine unavailable"

    _, residual = remediation.remediate_page_texts(pages, lambda _: "(cid:9) still broken")
    assert residual[0]["action"] == "manual_review_hold"
    assert "cid_artifact" in residual[0]["note"]


DATA_PAGES = {
    "multi_year_appendix": (
        "2019 2020 2021 2022 2023 2024\n"
        "Scope 1 14,203 13,881 12,431 11,982 10,744 9,318\n"
        "Scope 3 1,301,442 1,255,003 1,204,553 1,188,201 1,142,908 1,097,443\n"
        "Waste 58.2 59.9 61.0 64.3 68.8 72.5"
    ),
    "bare_figures": "1,204.5 1,188.2 1,142.9\n38.2% 44.7% 52.1%\n(1,204) (1,188) (1,142)",
    "sasb_index": "CG-MR-000.A 1,204 1,188 1,142\nCG-MR-110a.1 48,220 44,109 39,887",
    "currency_and_multiples": "$1,204.5 $1,188.2 EUR 48,220 12.4x 3.5x 38.2%",
}
GARBLED_PAGES = {
    "symbol_soup": "| [ ]1 ~- ' ,, ;; f l1 |[ ]| ~~ .. :: '' `` -- || }{ ><",
    "ocr_speckle": "l 1 I | f t . , ' ` r n m u v w a e o s c",
    "punctuation_noise": ".,;:!?()[]{}<>/\\|~`^*_=+ .,;:!?()[]{} <>/\\|~`^*_=+",
}


def test_numeric_data_pages_are_not_mistaken_for_garbled_text() -> None:
    """A clean metrics table is mostly digits, and must not read as garbage.

    Scoring readability by letter share flagged these pages for OCR they did not
    need, then refused the OCR result at verification for the same reason, so the
    pages carrying the actual ESG metrics could never be remediated.
    """
    for name, text in DATA_PAGES.items():
        assert remediation.detect_page_quality(text) == [], name


def test_genuinely_garbled_text_is_still_detected() -> None:
    for name, text in GARBLED_PAGES.items():
        assert "garbled_or_low_readable_text" in remediation.detect_page_quality(text), name


def test_numeric_page_can_win_verification_against_a_broken_page() -> None:
    """The verification gate must let a repaired data page through."""
    pages = {1: "(cid:12) (cid:45) broken emissions table"}
    final, outcomes = remediation.remediate_page_texts(
        pages, lambda _: DATA_PAGES["multi_year_appendix"]
    )
    assert outcomes[0]["action"] == "approved_page_override"
    assert final[1] == DATA_PAGES["multi_year_appendix"]


def test_isolated_character_speckle_scores_below_real_content() -> None:
    """Stray single letters are high letter-share but carry no content."""
    assert remediation.quality_score(GARBLED_PAGES["ocr_speckle"]) < remediation.quality_score(
        DATA_PAGES["multi_year_appendix"]
    )


CLEAN = "Verified readable climate progress and emissions evidence across operations."


def test_cheap_extractor_is_preferred_and_ocr_never_runs() -> None:
    """Most flagged pages have an intact text layer that a re-read recovers.

    Rendering and OCRing a page whose text is already in the PDF is slower, adds
    a Tesseract dependency, and loses line structure, so OCR must stay last.
    """
    ocr_calls: list[int] = []
    final, outcomes = remediation.remediate_page_texts(
        {1: "(cid:12) broken"},
        sources=(
            ("pdfium_text", lambda _: CLEAN),
            ("ocr", lambda page: ocr_calls.append(page) or CLEAN),
        ),
    )
    assert ocr_calls == []
    assert outcomes[0]["method"] == "pdfium_text"
    assert outcomes[0]["attempted"] == "pdfium_text"
    assert final[1] == CLEAN


def test_ocr_runs_only_after_the_cheap_extractor_fails() -> None:
    """A truly scanned page has no text layer, so OCR is the correct fallback."""
    ocr_calls: list[int] = []
    final, outcomes = remediation.remediate_page_texts(
        {1: "(cid:12) broken"},
        sources=(
            ("pdfium_text", lambda _: ""),
            ("ocr", lambda page: ocr_calls.append(page) or CLEAN),
        ),
    )
    assert ocr_calls == [1]
    assert outcomes[0]["method"] == "ocr"
    assert outcomes[0]["attempted"] == "pdfium_text;ocr"
    assert final[1] == CLEAN


def test_hold_records_every_method_tried() -> None:
    _, outcomes = remediation.remediate_page_texts(
        {1: "(cid:12) broken"},
        sources=(("pdfium_text", lambda _: ""), ("ocr", lambda _: "(cid:9) still broken")),
    )
    assert outcomes[0]["action"] == "manual_review_hold"
    assert outcomes[0]["method"] == ""
    assert outcomes[0]["attempted"] == "pdfium_text;ocr"
    assert "cid_artifact" in outcomes[0]["note"]


def test_engine_that_never_ran_is_distinct_from_one_that_did_not_help() -> None:
    """`ocr_failed` must mean no engine ran, so a real outage stays visible."""
    def boom(_: int) -> str:
        raise RuntimeError("engine unavailable")

    _, never_ran = remediation.remediate_page_texts(
        {1: "(cid:2) broken"}, sources=(("pdfium_text", boom), ("ocr", boom))
    )
    assert never_ran[0]["verification"] == "ocr_failed"

    _, ran_no_help = remediation.remediate_page_texts(
        {1: "(cid:2) broken"}, sources=(("pdfium_text", boom), ("ocr", lambda _: "(cid:9) bad"))
    )
    assert ran_no_help[0]["verification"] == "ocr_not_verified_or_not_better"


def test_sources_require_at_least_one_provider() -> None:
    try:
        remediation.remediate_page_texts({1: "(cid:2) broken"})
    except ValueError:
        return
    raise AssertionError("a missing ocr callable and missing sources must raise")


class _FakeBitmap:
    def to_pil(self) -> str:
        return "rendered-image"

    def close(self) -> None:
        pass


class _FakePage:
    def render(self, scale: float) -> _FakeBitmap:
        return _FakeBitmap()

    def close(self) -> None:
        pass


class _FakeDoc:
    def __getitem__(self, index: int) -> _FakePage:
        return _FakePage()


def test_ocr_page_keeps_reading_order_instead_of_flattening(monkeypatch) -> None:
    """Joining Tesseract's raw word list with spaces collapses a page to one line.

    A two-column ESG page flattened that way reaches sections and chunks with no
    structure left to split on, so ocr_page must return the ordered line text
    that ocr_pdf.py builds.
    """
    import ocr_pdf
    ordered = "SUSTAINABLE PACKAGING\nOur unboxing experience reflects\nour commitment."
    monkeypatch.setattr(remediation, "discover_tesseract", lambda: "tesseract")
    monkeypatch.setattr(ocr_pdf, "preprocess_image", lambda image: image)
    monkeypatch.setattr(
        ocr_pdf, "ocr_image_page",
        lambda image, page, min_conf, raw: type("R", (), {"text": ordered})(),
    )

    result = remediation.ocr_page(_FakeDoc(), 22)
    assert result == ordered
    assert len(result.splitlines()) == 3


def test_ocr_page_passes_the_configured_confidence_floor(monkeypatch) -> None:
    import ocr_pdf
    seen: dict = {}
    monkeypatch.setattr(remediation, "discover_tesseract", lambda: "tesseract")
    monkeypatch.setattr(ocr_pdf, "preprocess_image", lambda image: image)
    monkeypatch.setattr(
        ocr_pdf, "ocr_image_page",
        lambda image, page, min_conf, raw: seen.update(page=page, min_conf=min_conf, raw=raw)
        or type("R", (), {"text": "text"})(),
    )

    remediation.ocr_page(_FakeDoc(), 22)
    assert seen == {"page": 22, "min_conf": remediation.MIN_OCR_CONFIDENCE, "raw": False}


def test_every_override_field_is_declared() -> None:
    """extrasaction='ignore' drops undeclared keys silently; a typo would lose a column."""
    for field in (
        "note", "parsed_doc_before_sha256", "parsed_doc_after_sha256",
        "recovery_method", "attempted_methods",
    ):
        assert field in remediation.OVERRIDE_FIELDS
