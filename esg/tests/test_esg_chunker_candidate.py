from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

import tiktoken

try:
    from transformers import AutoTokenizer  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - exercised in the BGE test env
    AutoTokenizer = None

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "esg" / "src"))

from esg_chunker_candidate import (  # noqa: E402
    BGE_INPUT_LIMIT,
    chunk_section_candidate,
    final_bge_token_count,
    final_embedding_text,
    validate_candidate_tiling,
)


TOKENIZER_DIR = Path(
    os.environ.get(
        "ESG_BGE_TOKENIZER_DIR",
        ROOT.parent / "retail-intelligence-ESG-works" / "tmp" / "esg_task1_20260729" / "tokenizer_base",
    )
)


def metadata(**changes):
    row = {
        "company_name": "ADVANCE AUTO PARTS INC",
        "ticker": "AAP",
        "cik": "0001158449",
        "doc_type": "sustainability",
        "report_year": "2019",
        "report_year_status": "parsed",
        "report_year_span": "",
        "section_code": "supply_chain_ethics",
        "section_title_original": "Supplier Standards and Ethical Sourcing",
    }
    row.update(changes)
    return row


@unittest.skipUnless(
    TOKENIZER_DIR.exists() and AutoTokenizer is not None,
    "pinned BGE tokenizer or transformers is not available",
)
class ExactPrefixedInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bge = AutoTokenizer.from_pretrained(str(TOKENIZER_DIR), local_files_only=True)
        cls.cl100k = tiktoken.get_encoding("cl100k_base")

    def test_final_input_has_the_production_seven_line_prefix(self):
        text = final_embedding_text(metadata(), "A short sentence.")
        header, separator, body = text.partition("\n\n")
        self.assertEqual(separator, "\n\n")
        self.assertEqual(
            [line.split(": ", 1)[0] for line in header.splitlines()],
            ["Company", "Ticker", "Document", "Reporting year", "ESG topic", "Subsection", "Content type"],
        )
        self.assertEqual(body, "A short sentence.")

    def test_final_input_keeps_source_body_byte_exact(self):
        body = "Line  one.\n\nLine two with\tspacing."
        self.assertEqual(final_embedding_text(metadata(), body).partition("\n\n")[2], body)

    def test_candidate_budgets_against_the_exact_prefixed_bge_input(self):
        sentence = "The company reports measurable progress across its supplier network. "
        source = "Supplier Standards\n" + sentence * 240
        chunks = chunk_section_candidate(source, metadata(), self.bge, self.cl100k)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.bge_tokens <= BGE_INPUT_LIMIT for chunk in chunks))
        self.assertEqual(
            [final_bge_token_count(metadata(), chunk.text, self.bge) for chunk in chunks],
            [chunk.bge_tokens for chunk in chunks],
        )

    def test_candidate_preserves_exact_source_spans_and_clean_sentence_seams(self):
        sentence = "The company reports measurable progress across its supplier network. "
        source = sentence * 220
        chunks = chunk_section_candidate(source, metadata(), self.bge, self.cl100k)
        self.assertEqual(validate_candidate_tiling(source, chunks), [])
        for chunk in chunks:
            self.assertEqual(source[chunk.source_start : chunk.source_end], chunk.text)
        for previous, current in zip(chunks, chunks[1:]):
            self.assertLessEqual(current.source_start, previous.source_end)
            self.assertIn(previous.text.rstrip()[-1], ".!?")
            self.assertTrue(current.text.lstrip()[0].isupper())

    def test_table_rows_are_boundaries_and_continuations_get_header_context(self):
        table = "Metric 2018 2019 2020\n" + "\n".join(
            f"Scope {i} {1000+i:,} {1100+i:,} {1200+i:,}" for i in range(160)
        )
        chunks = chunk_section_candidate(table, metadata(), self.bge, self.cl100k)
        self.assertGreater(len(chunks), 1)
        self.assertIn("Metric 2018 2019 2020", chunks[0].text)
        self.assertEqual(chunks[0].table_context, "")
        self.assertTrue(
            all(chunk.table_context == "Metric 2018 2019 2020" for chunk in chunks[1:])
        )
        self.assertTrue(
            all(
                "Table context: Metric 2018 2019 2020"
                in final_embedding_text(metadata(), chunk.text, chunk.table_context)
                for chunk in chunks[1:]
            )
        )
        self.assertTrue(all(chunk.bge_tokens <= BGE_INPUT_LIMIT for chunk in chunks))
        self.assertTrue(all(not chunk.text.lstrip().startswith("Scope") or i > 0 for i, chunk in enumerate(chunks)))

    def test_metadata_safety_state_is_not_mutated(self):
        row = metadata(rag_action="exclude_from_esg_index", include_in_esg_index=False)
        before = dict(row)
        chunk_section_candidate("Navigation.\n" * 20, row, self.bge, self.cl100k)
        self.assertEqual(row, before)

    def test_long_prose_line_is_not_used_as_table_context(self):
        false_header = "2024 " + "very long narrative disclosure " * 100
        source = false_header + "\n" + "Metric 100 200 300\n" * 180
        chunks = chunk_section_candidate(source, metadata(), self.bge, self.cl100k)
        self.assertTrue(all(chunk.bge_tokens <= BGE_INPUT_LIMIT for chunk in chunks))
        self.assertNotIn(false_header.strip(), {chunk.table_context for chunk in chunks})


if __name__ == "__main__":
    unittest.main()
