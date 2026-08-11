import importlib.util
import sys
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "esg"
    / "scripts"
    / "esg_database_tiers_2"
    / "checkpoint2_outliers.py"
)
SPEC = importlib.util.spec_from_file_location("checkpoint2_outliers", SCRIPT)
checkpoint2 = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = checkpoint2
SPEC.loader.exec_module(checkpoint2)


def document(**overrides):
    row = {
        "doc_id": 1,
        "ticker": "BIRD",
        "report_year": 2024,
        "chunk_count": 5,
        "section_count": 2,
        "page_count": 15,
        "parsed_chars": 6691,
        "byte_size": 20_162_461,
        "parse_status": "parsed",
        "doc_quality_status": "ok",
        "filepath": "BIRD-2024.pdf",
    }
    row.update(overrides)
    return row


def test_q15_does_not_call_low_chunk_count_a_failed_parse(tmp_path):
    result = checkpoint2.check_q15([document()], [], tmp_path, examples_wanted=5)

    assert result.status == "WARN"
    assert result.stats["defects"] == 0
    assert result.stats["review_needed"] == 1


def test_q15_still_fails_a_long_document_with_no_output(tmp_path):
    result = checkpoint2.check_q15(
        [document(chunk_count=0, section_count=0, parsed_chars=0)],
        [],
        tmp_path,
        examples_wanted=5,
    )

    assert result.status == "FAIL"
    assert result.stats["defects"] == 1
