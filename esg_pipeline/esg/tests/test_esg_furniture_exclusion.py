"""An exclusion has to say why, or nobody can review or reverse it.

The furniture gate drops a chunk built mostly from contents listings, covers or
part-title dividers. It recorded which furniture in page_role, but page_role is
not carried into the database, so every chunk this gate excluded reached QA with
no reviewable cause -- 215 of them on the 682-document build, reported by
Checkpoint 5 Q26 as "(no reason code recorded)". Every other exclusion path
writes its reason into quality_flags; this one now does too.
"""

from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "esg" / "src"))

import esg_chunker  # noqa: E402


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


CONTENT = "Our emissions fell twelve percent against a two thousand nineteen baseline. "
FURNITURE = "Contents Introduction Approach Governance Appendix "


def page_spans_for(text: str, split_at: int, first_role: str, second_role: str):
    """Two pages over one text, so a span's furniture share is controllable."""
    return [
        {"page": 1, "char_start": 0, "char_end": split_at, "page_role": first_role},
        {
            "page": 2,
            "char_start": split_at,
            "char_end": len(text),
            "page_role": second_role,
        },
    ]


class FurnitureExclusionReasonTests(unittest.TestCase):
    def build(self, text: str, page_spans: list[dict], doc_meta: dict | None = None):
        fingerprint = esg_chunker.SourceFingerprint(
            size_bytes=len(text),
            mtime_utc="2026-01-01T00:00:00+00:00",
            sha256="0" * 64,
        )
        meta = {
            "source_id": "TST__TST_TEST_INC_2024",
            "source_version_id": "TST__TST_TEST_INC_2024__abc123abc123",
            "doc_quality_status": "ok",
            "rag_action": "index_as_esg",
            "include_in_esg_index": True,
        }
        meta.update(doc_meta or {})
        return esg_chunker.build_chunk_output(
            ticker="TST",
            pdf_stem="TST-TEST INC-2024",
            section_code="environmental",
            section_instance_id="environmental__0001",
            section_file=Path("environmental__0001.txt"),
            output_root=Path("out"),
            chunk_index=0,
            chunk_text=text,
            token_count=len(text.split()),
            source_fingerprint=fingerprint,
            parsed_text=text,
            parsed_text_sha256=sha256_text(text),
            expected_parsed_text_sha256=sha256_text(text),
            section_text=text,
            section_source_start=0,
            section_source_end=len(text),
            local_start=0,
            local_end=len(text),
            page_spans=page_spans,
            doc_meta=meta,
        ).row

    def test_furniture_exclusion_records_its_reason(self):
        text = FURNITURE * 6 + CONTENT
        split = len(FURNITURE) * 6
        row = self.build(text, page_spans_for(text, split, "toc", "content"))

        self.assertEqual(row["rag_action"], "exclude_from_esg_index")
        self.assertIn(
            esg_chunker.QUALITY_FLAG_FURNITURE_SPAN,
            row["quality_flags"].split("|"),
        )

    def test_page_role_still_carries_which_furniture(self):
        text = FURNITURE * 6 + CONTENT
        split = len(FURNITURE) * 6
        row = self.build(text, page_spans_for(text, split, "toc", "content"))

        # The flag names the gate; page_role keeps the detail, for whoever
        # reads the index rather than the database.
        self.assertEqual(row["page_role"], "content|toc")

    def test_a_mostly_content_chunk_is_neither_excluded_nor_flagged(self):
        text = FURNITURE + CONTENT * 6
        split = len(FURNITURE)
        row = self.build(text, page_spans_for(text, split, "toc", "content"))

        self.assertEqual(row["rag_action"], "index_as_esg")
        self.assertEqual(row["quality_flags"], "")

    def test_the_flag_joins_an_existing_reason_rather_than_replacing_it(self):
        text = FURNITURE * 6 + CONTENT
        split = len(FURNITURE) * 6
        row = self.build(
            text,
            page_spans_for(text, split, "toc", "content"),
            doc_meta={"quality_flags": esg_chunker.QUALITY_FLAG_SECTION_HELD},
        )

        self.assertEqual(
            row["quality_flags"].split("|"),
            [
                esg_chunker.QUALITY_FLAG_SECTION_HELD,
                esg_chunker.QUALITY_FLAG_FURNITURE_SPAN,
            ],
        )


if __name__ == "__main__":
    unittest.main()
