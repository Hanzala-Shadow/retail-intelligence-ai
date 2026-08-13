from __future__ import annotations

import hashlib
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

import tiktoken


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "esg" / "src"))

import esg_chunker  # noqa: E402
import section_splitter_esg  # noqa: E402


class WordTokenizer:
    """Small deterministic tokenizer for subsection routing tests."""

    @staticmethod
    def encode(text: str, **_: object) -> list[int]:
        return list(range(len(re.findall(r"\w+|[^\w\s]", text))))


def long_body(label: str, sentences: int = 170) -> str:
    del sentences
    return (
        f"This {label} program has clear goals, partners, and measured community results. "
        "Teams report progress each year through normal narrative prose. "
    ) * 80


class ESGSubsectionContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bge = WordTokenizer()
        self.cl100k = tiktoken.get_encoding(esg_chunker.ENCODING)

    def candidate_chunks(self, first: str, second: str):
        text = f"{first}\n\n{long_body('First')}\n\n{second}\n\n{long_body('Second')}"
        sections = section_splitter_esg.split_esg_sections(text)
        same_code = [section for section in sections if section.section_code == "community"]
        self.assertEqual(len(same_code), 1)
        section = same_code[0]
        spans_json = section_splitter_esg.subsection_spans_json(
            section, section.source_start_char, section.source_end_char
        )
        metadata = {
            "company_name": "TEST COMPANY",
            "ticker": "TEST",
            "doc_type": "sustainability",
            "report_year": "2024",
            "section_code": "community",
            "section_title": first,
            "physical_section_title": first,
            "section_title_original": first,
            "subsection_spans_json": spans_json,
        }
        spans = esg_chunker.parse_subsection_spans(metadata, len(section.text))
        chunks = esg_chunker.chunk_section_v3(
            section.text, metadata, self.bge, self.cl100k, spans
        )
        return section, metadata, spans, chunks

    def test_svv_orly_and_tpr_keep_internal_subsections_in_one_physical_section(self):
        cases = [
            ("STORE COMMUNITY OUTREACH", "NONPROFIT PARTNER SPOTLIGHTS"),
            ("Community", "Feeding Our Communities Partners"),
            ("Blue Star Families - a national nonprofit", "SOCIAL IMPACT COUNCIL"),
        ]
        for first, second in cases:
            with self.subTest(second=second):
                section, metadata, spans, chunks = self.candidate_chunks(first, second)
                self.assertEqual(section.title, first)
                self.assertEqual([span.title for span in spans], [first, second])
                self.assertTrue(any(second in chunk.subsection_context for chunk in chunks))
                continuations = [
                    chunk for chunk in chunks if chunk.subsection_context == second
                ]
                self.assertGreaterEqual(len(continuations), 2)

                for chunk in chunks:
                    chunk_metadata, context, _ = esg_chunker.metadata_with_subsection(
                        metadata, spans, chunk.source_start, chunk.source_end
                    )
                    embedding_text = esg_chunker.final_embedding_text(
                        chunk_metadata, chunk.text, chunk.table_context
                    )
                    self.assertEqual(context, chunk.subsection_context)
                    self.assertTrue(embedding_text.endswith(chunk.text))
                    self.assertEqual(
                        hashlib.sha256(chunk.text.encode("utf-8")).hexdigest(),
                        hashlib.sha256(
                            embedding_text[-len(chunk.text):].encode("utf-8")
                        ).hexdigest(),
                    )

    def test_short_subsections_share_a_chunk_with_ordered_context(self):
        first = "Community"
        second = "Feeding Our Communities Partners"
        text = f"{first}\n\nShort evidence.\n\n{second}\n\nMore short evidence."
        sections = section_splitter_esg.split_esg_sections(text)
        section = sections[0]
        raw = section_splitter_esg.subsection_spans_json(
            section, section.source_start_char, section.source_end_char
        )
        spans = esg_chunker.parse_subsection_spans(
            {"subsection_spans_json": raw}, len(section.text)
        )
        context, titles = esg_chunker.subsection_context_for_range(
            spans, 0, len(section.text), first
        )
        self.assertEqual(titles, (first, second))
        self.assertEqual(context, f"{first} → {second}")

        metadata = {
            "company_name": "O'REILLY AUTOMOTIVE INC",
            "ticker": "ORLY",
            "doc_type": "sustainability",
            "report_year": "2024",
            "section_code": "community",
            "section_title": first,
            "physical_section_title": first,
            "section_title_original": first,
        }
        chunks = esg_chunker.chunk_section_v3(
            section.text, metadata, self.bge, self.cl100k, spans
        )
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].subsection_contexts, (first, second))

    def test_serialized_spans_are_ordered_and_section_relative(self):
        section, _, _, _ = self.candidate_chunks(
            "STORE COMMUNITY OUTREACH", "NONPROFIT PARTNER SPOTLIGHTS"
        )
        values = json.loads(
            section_splitter_esg.subsection_spans_json(
                section, section.source_start_char, section.source_end_char
            )
        )
        self.assertEqual(values[0]["start_char"], 0)
        self.assertLess(values[0]["start_char"], values[1]["start_char"])
        self.assertEqual(values[-1]["end_char"], len(section.text))

    def test_chunk_index_rows_receive_active_subsection_metadata(self):
        section, _, _, _ = self.candidate_chunks(
            "Community", "Feeding Our Communities Partners"
        )
        spans_json = section_splitter_esg.subsection_spans_json(
            section, section.source_start_char, section.source_end_char
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            section_file = (
                root
                / "sections"
                / "ORLY"
                / "ORLY-Report-2024__community__0001.txt"
            )
            section_file.parent.mkdir(parents=True)
            section_file.write_text(section.text, encoding="utf-8", newline="")
            plan = esg_chunker.build_section_plan(
                section_file,
                root / "chunks",
                self.cl100k,
                {
                    ("ORLY", "ORLY-Report-2024", "community__0001"): {
                        "section_code": "community",
                        "section_title": "Community",
                        "subsection_spans_json": spans_json,
                        "source_start_char": "0",
                        "source_end_char": str(len(section.text)),
                    }
                },
                {
                    ("ORLY", "ORLY-Report-2024"): {
                        "source_id": "ORLY__2024__sustainability__01",
                        "canonical_ticker": "ORLY",
                        "doc_type": "sustainability",
                        "include_in_esg_index": True,
                        "doc_quality_status": "ok",
                        "rag_action": "index_as_esg",
                    }
                },
                bge_tokenizer=self.bge,
                company_names={"ORLY": "O'REILLY AUTOMOTIVE INC"},
            )

        rows = [output.row for output in plan.outputs]
        self.assertEqual({row["physical_section_title"] for row in rows}, {"Community"})
        later = [
            row
            for row in rows
            if row["subsection_context"] == "Feeding Our Communities Partners"
        ]
        self.assertGreaterEqual(len(later), 2)
        self.assertTrue(
            all(
                json.loads(row["subsection_contexts_json"])
                == ["Feeding Our Communities Partners"]
                for row in later
            )
        )


if __name__ == "__main__":
    unittest.main()
