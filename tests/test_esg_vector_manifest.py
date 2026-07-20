from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "src"))

import build_esg_vector_manifest
import esg_layout_qa


class LayoutAuditVersionCouplingTests(unittest.TestCase):
    def test_manifest_layout_version_matches_the_audit(self) -> None:
        """The manifest duplicates the audit version; drift quarantines everything.

        Any audit row whose version differs from the manifest's constant is
        treated as stale, and a stale page holds its chunk. If the audit is
        bumped and this constant is not, every chunk in the corpus is held.
        """
        self.assertEqual(
            build_esg_vector_manifest.LAYOUT_AUDIT_VERSION,
            esg_layout_qa.AUDIT_VERSION,
        )


CHUNK_FIELDS = [
    "chunk_id",
    "source_id",
    "source_version_id",
    "ticker",
    "canonical_ticker",
    "doc_type",
    "source_type",
    "source_scope",
    "retrieval_tier",
    "include_in_esg_index",
    "duplicate_of_source_id",
    "doc_quality_status",
    "rag_action",
    "quality_flags",
    "pdf_stem",
    "section_code",
    "section_instance_id",
    "chunk_index",
    "chunk_type",
    "short_section_action",
    "short_section_reason",
    "merged_section_ids",
    "token_count",
    "char_count",
    "chunk_file",
    "source_section_file",
    "source_size_bytes",
    "source_mtime_utc",
    "source_sha256",
    "parsed_text_sha256",
    "section_text_sha256",
    "source_start_char",
    "source_end_char",
    "page_start",
    "page_end",
    "citation_ready",
    "citation_validation_status",
    "citation_validation_version",
]

REGISTRY_FIELDS = [
    "source_id",
    "observed_ticker",
    "canonical_ticker",
    "pdf_stem",
    "source_type",
    "source_scope",
    "retrieval_tier",
    "include_in_esg_index",
    "duplicate_of_source_id",
    "notes",
]


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def base_chunk(**overrides) -> dict:
    row = {field: "" for field in CHUNK_FIELDS}
    row.update(
        {
            "chunk_id": "TEST__old__climate__0001__chunk_0000",
            "source_id": "TEST__old",
            "source_version_id": "TEST__old__abc123",
            "ticker": "TEST",
            "canonical_ticker": "TEST",
            "doc_type": "sustainability",
            "source_type": "sustainability",
            "source_scope": "full_report",
            "retrieval_tier": "primary",
            "include_in_esg_index": "true",
            "doc_quality_status": "ok",
            "rag_action": "index_as_esg",
            "pdf_stem": "TEST-Report-2024",
            "section_code": "climate",
            "section_instance_id": "climate__0001",
            "chunk_index": "0",
            "chunk_type": "normal",
            "token_count": "120",
            "citation_ready": "true",
            "citation_validation_status": "verified_exact",
            "page_start": "1",
            "page_end": "1",
            "chunk_file": "chunks/TEST.txt",
        }
    )
    row.update(overrides)
    return row


class ESGVectorManifestTests(unittest.TestCase):
    def test_build_manifest_has_one_row_per_chunk_and_excludes_navigation_trace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            chunks = root / "chunks.csv"
            registry = root / "registry.csv"
            out = root / "manifest.csv"
            write_csv(
                chunks,
                CHUNK_FIELDS,
                [
                    base_chunk(),
                    base_chunk(
                        chunk_id="TEST__old__appendix__0001__chunk_0000",
                        section_code="appendix",
                        section_instance_id="appendix__0001",
                        chunk_type="short_evidence",
                        short_section_action="excluded",
                        include_in_esg_index="false",
                        rag_action="exclude_from_esg_index",
                        token_count="50",
                    ),
                ],
            )
            write_csv(
                registry,
                REGISTRY_FIELDS,
                [
                    {
                        "source_id": "TEST__2024__sustainability_report__01",
                        "observed_ticker": "TEST",
                        "canonical_ticker": "TEST",
                        "pdf_stem": "TEST-Report-2024",
                        "source_type": "sustainability",
                        "source_scope": "full_report",
                        "retrieval_tier": "primary",
                        "include_in_esg_index": "true",
                        "duplicate_of_source_id": "",
                        "notes": "",
                    }
                ],
            )

            rows = build_esg_vector_manifest.build_manifest(chunks, registry, out)

            self.assertEqual(len(rows), 2)
            by_id = {row["chunk_id"]: row for row in rows}
            self.assertEqual(
                by_id["TEST__old__climate__0001__chunk_0000"]["source_id"],
                "TEST__2024__sustainability_report__01",
            )
            self.assertEqual(
                by_id["TEST__old__climate__0001__chunk_0000"]["eligibility_decision"],
                "eligible",
            )
            nav = by_id["TEST__old__appendix__0001__chunk_0000"]
            self.assertEqual(nav["eligibility_decision"], "excluded")
            self.assertIn("navigation_trace_chunk", nav["eligibility_reason"])

    def test_build_manifest_fails_on_missing_or_duplicate_chunk_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            registry = root / "registry.csv"
            write_csv(registry, REGISTRY_FIELDS, [])

            missing = root / "missing.csv"
            write_csv(missing, CHUNK_FIELDS, [base_chunk(chunk_id="")])
            with self.assertRaisesRegex(ValueError, "missing chunk_id"):
                build_esg_vector_manifest.build_manifest(missing, registry, root / "missing_out.csv")

            duplicate = root / "duplicate.csv"
            write_csv(duplicate, CHUNK_FIELDS, [base_chunk(), base_chunk()])
            with self.assertRaisesRegex(ValueError, "duplicate chunk_id"):
                build_esg_vector_manifest.build_manifest(duplicate, registry, root / "dup_out.csv")

    def test_layout_auto_hold_excludes_only_overlapping_chunk_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            chunks = root / "chunks.csv"
            registry = root / "registry.csv"
            layout = root / "layout.csv"
            out = root / "manifest.csv"
            first = base_chunk(source_sha256="source", parsed_text_sha256="parsed", page_start="1", page_end="1")
            second = base_chunk(
                chunk_id="TEST__old__climate__0001__chunk_0001",
                source_sha256="source",
                parsed_text_sha256="parsed",
                page_start="2",
                page_end="2",
            )
            write_csv(chunks, CHUNK_FIELDS, [first, second])
            write_csv(registry, REGISTRY_FIELDS, [])
            layout_fields = [
                "ticker",
                "pdf_stem",
                "page",
                "source_sha256",
                "parsed_text_sha256",
                "audit_version",
                "decision",
            ]
            write_csv(
                layout,
                layout_fields,
                [
                    {
                        "ticker": "TEST",
                        "pdf_stem": "TEST-Report-2024",
                        "page": "1",
                        "source_sha256": "source",
                        "parsed_text_sha256": "parsed",
                        "audit_version": build_esg_vector_manifest.LAYOUT_AUDIT_VERSION,
                        "decision": "auto_pass",
                    },
                    {
                        "ticker": "TEST",
                        "pdf_stem": "TEST-Report-2024",
                        "page": "2",
                        "source_sha256": "source",
                        "parsed_text_sha256": "parsed",
                        "audit_version": build_esg_vector_manifest.LAYOUT_AUDIT_VERSION,
                        "decision": "auto_hold",
                    },
                ],
            )

            rows = build_esg_vector_manifest.build_manifest(
                chunks,
                registry,
                out,
                layout_audit_path=layout,
                require_layout_audit=True,
            )

            by_id = {row["chunk_id"]: row for row in rows}
            self.assertEqual(by_id[first["chunk_id"]]["eligibility_decision"], "eligible")
            held = by_id[second["chunk_id"]]
            self.assertEqual(held["eligibility_decision"], "excluded")
            self.assertEqual(held["layout_qa_status"], "auto_hold")
            self.assertIn("layout_auto_hold_page=2", held["eligibility_reason"])

    def test_stale_layout_audit_version_fails_closed(self) -> None:
        chunk = base_chunk(page_start="1", page_end="1")
        status, reason = build_esg_vector_manifest.layout_policy_for_chunk(
            chunk,
            {
                ("TEST", "TEST-Report-2024"): {
                    1: {
                        "audit_version": "layout_v1",
                        "source_sha256": "",
                        "parsed_text_sha256": "",
                        "decision": "auto_pass",
                    }
                }
            },
        )

        self.assertEqual(status, "auto_hold")
        self.assertEqual(reason, "layout_audit_stale_version_page=1")


if __name__ == "__main__":
    unittest.main()
