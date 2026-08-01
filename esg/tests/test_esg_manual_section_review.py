import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "build_esg_manual_section_review.py"
SPEC = importlib.util.spec_from_file_location("build_esg_manual_section_review", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_missing_subsection_spans_are_supported():
    assert MODULE.load_subsection_spans({"section_title": "Frozen legacy row"}) == []


def test_malformed_subsection_spans_are_ignored():
    assert MODULE.load_subsection_spans({"subsection_spans_json": "not-json"}) == []


def test_valid_subsection_spans_are_loaded():
    row = {"subsection_spans_json": '[{"title": "Energy"}, 3]'}
    assert MODULE.load_subsection_spans(row) == [{"title": "Energy"}]
