"""A failed manual review must be able to stop one section, not a whole document.

The document-level quality gate has no way to say "this report is fine but this
one section is not". Sections that fail manual review -- a corrupted table, a
physical boundary in the wrong place, interleaved reading order -- are recorded
in a sparse hold registry. Held sections are still chunked and still citable, so
the lineage survives, but they never reach the index unreviewed.
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

import tiktoken


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "esg" / "src"))

import esg_chunker  # noqa: E402


HOLD_CSV = (
    "ticker,pdf_stem,section_instance_id,rag_action,reason\n"
    "DELL,DELL-DELL TECHNOLOGIES INC-2023,data_summary__0010,"
    "manual_review_before_indexing,severe_table_extraction_corruption\n"
)


class WordTokenizer:
    @staticmethod
    def encode(text: str, **_: object) -> list[int]:
        return list(range(len(re.findall(r"\w+|[^\w\s]", text))))


class SectionHoldRegistryTests(unittest.TestCase):
    def write_registry(self, root: Path, body: str) -> Path:
        path = root / "esg_section_hold.csv"
        path.write_text(body, encoding="utf-8", newline="")
        return path

    def test_omitted_registry_holds_nothing(self):
        self.assertEqual(esg_chunker.load_section_hold_registry(None), {})

    def test_missing_supplied_registry_fails_closed(self):
        with self.assertRaises(FileNotFoundError):
            esg_chunker.load_section_hold_registry(Path("does-not-exist.csv"))

    def test_registry_is_keyed_by_ticker_stem_and_section(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.write_registry(Path(temp_dir), HOLD_CSV)
            holds = esg_chunker.load_section_hold_registry(path)
        self.assertEqual(
            list(holds),
            [("DELL", "DELL-DELL TECHNOLOGIES INC-2023", "data_summary__0010")],
        )

    def test_registry_rejects_an_unsupported_action(self):
        body = HOLD_CSV.replace("manual_review_before_indexing", "index_as_esg")
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.write_registry(Path(temp_dir), body)
            with self.assertRaises(ValueError):
                esg_chunker.load_section_hold_registry(path)

    def test_registry_rejects_missing_required_columns(self):
        body = "ticker,pdf_stem,section_instance_id,rag_action\nDELL,report,section,index_as_esg\n"
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.write_registry(Path(temp_dir), body)
            with self.assertRaisesRegex(ValueError, "missing column"):
                esg_chunker.load_section_hold_registry(path)

    def test_registry_rejects_a_blank_section_key(self):
        body = HOLD_CSV.replace("data_summary__0010", "")
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.write_registry(Path(temp_dir), body)
            with self.assertRaisesRegex(ValueError, "section_instance_id are required"):
                esg_chunker.load_section_hold_registry(path)

    def test_registry_rejects_duplicate_sections(self):
        body = HOLD_CSV + HOLD_CSV.split("\n", 1)[1]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.write_registry(Path(temp_dir), body)
            with self.assertRaisesRegex(ValueError, "duplicate section hold"):
                esg_chunker.load_section_hold_registry(path)

    def test_merge_rejects_a_hold_for_an_unknown_section(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.write_registry(Path(temp_dir), HOLD_CSV)
            holds = esg_chunker.load_section_hold_registry(path)
        with self.assertRaises(ValueError):
            esg_chunker.merge_section_holds({}, holds)

    def test_merge_records_the_decision_on_the_section_metadata(self):
        key = ("DELL", "DELL-DELL TECHNOLOGIES INC-2023", "data_summary__0010")
        section_metadata = {key: {"section_code": "data_summary"}}
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.write_registry(Path(temp_dir), HOLD_CSV)
            holds = esg_chunker.load_section_hold_registry(path)
        self.assertEqual(esg_chunker.merge_section_holds(section_metadata, holds), 1)
        self.assertEqual(
            section_metadata[key]["hold_rag_action"], "manual_review_before_indexing"
        )
        self.assertEqual(
            section_metadata[key]["hold_reason"], "severe_table_extraction_corruption"
        )


class ApplySectionHoldTests(unittest.TestCase):
    def setUp(self) -> None:
        self.doc_meta = {
            "include_in_esg_index": True,
            "rag_action": "index_as_esg",
            "quality_flags": "existing_flag",
        }

    def test_an_unheld_section_is_left_alone(self):
        self.assertIs(esg_chunker.apply_section_hold(self.doc_meta, {}), self.doc_meta)

    def test_a_held_section_is_taken_out_of_the_index(self):
        held = esg_chunker.apply_section_hold(
            self.doc_meta,
            {
                "hold_rag_action": "manual_review_before_indexing",
                "hold_reason": "severe_table_extraction_corruption",
            },
        )
        self.assertFalse(held["include_in_esg_index"])
        self.assertEqual(held["rag_action"], "manual_review_before_indexing")
        self.assertIn(esg_chunker.QUALITY_FLAG_SECTION_HELD, held["quality_flags"])
        self.assertIn("severe_table_extraction_corruption", held["quality_flags"])
        self.assertIn("existing_flag", held["quality_flags"])

    def test_the_original_metadata_is_not_mutated(self):
        esg_chunker.apply_section_hold(
            self.doc_meta, {"hold_rag_action": "exclude_from_esg_index"}
        )
        self.assertTrue(self.doc_meta["include_in_esg_index"])
        self.assertEqual(self.doc_meta["rag_action"], "index_as_esg")

    def test_an_unsupported_action_is_refused(self):
        with self.assertRaises(ValueError):
            esg_chunker.apply_section_hold(
                self.doc_meta, {"hold_rag_action": "index_as_esg"}
            )


class HeldSectionPlanTests(unittest.TestCase):
    """A held section still produces chunks; it just is not indexable."""

    def build(self, section_meta_extra: dict) -> list[dict]:
        text = (
            " ".join(
                " ".join(["The community program reported measured results"] * 6) + "."
                for _ in range(40)
            )
        )
        section_meta = {
            "section_code": "data_summary",
            "section_title": "Data Summary",
            "source_start_char": "0",
            "source_end_char": str(len(text)),
            **section_meta_extra,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            section_file = (
                root / "sections" / "DELL" / "DELL-Report-2023__data_summary__0010.txt"
            )
            section_file.parent.mkdir(parents=True)
            section_file.write_text(text, encoding="utf-8", newline="")
            plan = esg_chunker.build_section_plan(
                section_file,
                root / "chunks",
                tiktoken.get_encoding(esg_chunker.ENCODING),
                {("DELL", "DELL-Report-2023", "data_summary__0010"): section_meta},
                {
                    ("DELL", "DELL-Report-2023"): {
                        "source_id": "DELL__2023__sustainability__01",
                        "canonical_ticker": "DELL",
                        "doc_type": "sustainability",
                        "include_in_esg_index": True,
                        "doc_quality_status": "ok",
                        "rag_action": "index_as_esg",
                    }
                },
                bge_tokenizer=WordTokenizer(),
                company_names={"DELL": "DELL TECHNOLOGIES INC"},
            )
        return [output.row for output in plan.outputs]

    def test_an_unheld_section_is_indexed(self):
        rows = self.build({})
        self.assertTrue(rows)
        self.assertEqual({row["rag_action"] for row in rows}, {"index_as_esg"})
        self.assertEqual({row["include_in_esg_index"] for row in rows}, {"true"})

    def test_a_held_section_keeps_its_chunks_but_loses_eligibility(self):
        rows = self.build(
            {
                "hold_rag_action": "manual_review_before_indexing",
                "hold_reason": "severe_table_extraction_corruption",
            }
        )
        self.assertTrue(rows, "a held section must still produce citable chunks")
        self.assertEqual(
            {row["rag_action"] for row in rows}, {"manual_review_before_indexing"}
        )
        self.assertEqual({row["include_in_esg_index"] for row in rows}, {"false"})
        for row in rows:
            self.assertIn(esg_chunker.QUALITY_FLAG_SECTION_HELD, row["quality_flags"])
            self.assertTrue(row["chunk_text_sha256"])

    def test_holding_does_not_change_the_chunk_text(self):
        unheld = self.build({})
        held = self.build({"hold_rag_action": "manual_review_before_indexing"})
        self.assertEqual(
            [row["chunk_text_sha256"] for row in unheld],
            [row["chunk_text_sha256"] for row in held],
        )


if __name__ == "__main__":
    unittest.main()
