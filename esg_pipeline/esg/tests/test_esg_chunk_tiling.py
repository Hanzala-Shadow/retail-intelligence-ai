"""Chunk spans must tile their section without one chunk sitting inside another.

A chunk whose span is a subset of its neighbour's carries no evidence the
neighbour does not already carry. It reaches the index as a second vector over
the same sentences, under the same subsection context, so it can only crowd a
retrieval result with a duplicate. The greedy splitter used to emit one whenever
the overlap back-off landed on a start that could not reach the next boundary
under the token budget.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

import tiktoken


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "esg" / "src"))

import esg_chunker  # noqa: E402


class WordTokenizer:
    """Small deterministic tokenizer, matching the subsection-context tests."""

    @staticmethod
    def encode(text: str, **_: object) -> list[int]:
        return list(range(len(re.findall(r"\w+|[^\w\s]", text))))


def sentence(word: str, count: int) -> str:
    return " ".join([word] * count) + "."


def unreachable_boundary_text() -> str:
    """A section whose second-to-last sentence cannot be crossed in one chunk.

    The greedy pass fills a chunk, steps back into overlap for the next one,
    and then finds that the only boundary ahead is behind a sentence too long
    to fit in the remaining budget. The latest fitting end is therefore the
    previous chunk's end, and the chunk that used to be emitted there was a
    strict subset of the chunk before it.
    """
    parts = [sentence(f"alpha{index}", 40) for index in range(8)]
    parts.append(sentence("beta", 10))
    parts.append(sentence("gamma", 520))
    parts.append(sentence("delta", 30))
    return " ".join(parts)


class ESGChunkTilingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bge = WordTokenizer()
        self.cl100k = tiktoken.get_encoding(esg_chunker.ENCODING)
        self.metadata = {
            "company_name": "TEST COMPANY",
            "ticker": "TEST",
            "doc_type": "sustainability",
            "report_year": "2024",
            "section_code": "community",
            "section_title": "Community",
            "physical_section_title": "Community",
            "section_title_original": "Community",
        }

    def chunk(self, text: str):
        return esg_chunker.chunk_section_v3(text, self.metadata, self.bge, self.cl100k, [])

    def assert_no_containment(self, chunks) -> None:
        for previous, current in zip(chunks, chunks[1:]):
            span = (previous.source_start, previous.source_end)
            following = (current.source_start, current.source_end)
            self.assertGreater(
                current.source_end,
                previous.source_end,
                f"chunk {following} does not advance past {span}",
            )
            self.assertGreater(
                current.source_start,
                previous.source_start,
                f"chunk {following} swallows {span}",
            )

    def test_unreachable_boundary_does_not_emit_a_contained_chunk(self):
        text = unreachable_boundary_text()
        chunks = self.chunk(text)
        self.assert_no_containment(chunks)

    def test_unreachable_boundary_still_tiles_the_whole_section(self):
        text = unreachable_boundary_text()
        chunks = self.chunk(text)
        self.assertEqual(chunks[0].source_start, 0)
        self.assertEqual(chunks[-1].source_end, len(text))
        for previous, current in zip(chunks, chunks[1:]):
            self.assertLessEqual(
                current.source_start,
                previous.source_end,
                "chunks must not leave a gap",
            )
        self.assertEqual(esg_chunker.validate_v3_tiling(text, chunks), [])

    def test_dropping_the_overlap_is_preferred_over_a_duplicate_chunk(self):
        """The fix trades one boundary's overlap away, not the source text."""
        text = unreachable_boundary_text()
        chunks = self.chunk(text)
        covered = set()
        for chunk in chunks:
            covered.update(range(chunk.source_start, chunk.source_end))
            self.assertEqual(chunk.text, text[chunk.source_start : chunk.source_end])
        self.assertEqual(covered, set(range(len(text))))

    def test_ordinary_prose_keeps_its_designed_overlap(self):
        # Sentences short enough to fit the overlap budget, so every boundary
        # here is one the back-off can actually use.
        text = " ".join(sentence("impact", 20) for _ in range(60))
        chunks = self.chunk(text)
        self.assert_no_containment(chunks)
        self.assertGreater(len(chunks), 2)
        overlaps = [
            previous.source_end - current.source_start
            for previous, current in zip(chunks, chunks[1:])
        ]
        self.assertTrue(
            all(overlap > 0 for overlap in overlaps),
            "the guard must not suppress overlap on healthy boundaries",
        )

    def test_validate_v3_tiling_flags_a_contained_chunk(self):
        text = "abcdefghij"
        chunks = [
            esg_chunker.CandidateChunk(text[0:8], 0, 8, 8, 8),
            esg_chunker.CandidateChunk(text[4:8], 4, 8, 4, 4),
            esg_chunker.CandidateChunk(text[8:10], 8, 10, 2, 2),
        ]
        self.assertIn("contained_chunk", esg_chunker.validate_v3_tiling(text, chunks))

    def test_validate_v3_tiling_flags_a_chunk_that_swallows_its_predecessor(self):
        text = "abcdefghij"
        chunks = [
            esg_chunker.CandidateChunk(text[0:4], 0, 4, 4, 4),
            esg_chunker.CandidateChunk(text[0:10], 0, 10, 10, 10),
        ]
        self.assertIn(
            "contains_previous_chunk", esg_chunker.validate_v3_tiling(text, chunks)
        )

    def test_validate_v3_tiling_accepts_a_healthy_overlapping_tiling(self):
        text = "abcdefghij"
        chunks = [
            esg_chunker.CandidateChunk(text[0:6], 0, 6, 6, 6),
            esg_chunker.CandidateChunk(text[4:10], 4, 10, 6, 6),
        ]
        self.assertEqual(esg_chunker.validate_v3_tiling(text, chunks), [])


if __name__ == "__main__":
    unittest.main()
