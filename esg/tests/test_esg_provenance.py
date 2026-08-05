from __future__ import annotations

import csv
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

import tiktoken


import esg_chunker


class ESGProvenanceTests(unittest.TestCase):
    def test_prefix_only_match_is_rejected(self) -> None:
        shared_prefix = "A" * 300
        source = f"{shared_prefix} correct ending"
        altered = f"{shared_prefix} altered ending"

        self.assertEqual(esg_chunker.locate_text_span(source, altered), (None, None))

    def test_exact_and_whitespace_normalized_citations_are_distinguished(self) -> None:
        parsed = "Climate\nScope 1,\nScope 2, and Scope 3 targets.\nAppendix"
        section_start = parsed.index("Climate")
        section_end = parsed.index("\nAppendix")
        section = parsed[section_start:section_end]
        page_map = [{"page": "7", "char_start": "0", "char_end": str(len(parsed))}]
        digest = hashlib.sha256(parsed.encode("utf-8")).hexdigest()

        exact_chunk = "Scope 1,\nScope 2"
        exact_start, exact_end = esg_chunker.locate_text_span(section, exact_chunk)
        exact = esg_chunker.validate_chunk_citation(
            parsed_text=parsed,
            parsed_text_sha256=digest,
            expected_parsed_text_sha256=digest,
            section_text=section,
            section_start=section_start,
            section_end=section_end,
            chunk_text=exact_chunk,
            local_start=exact_start,
            local_end=exact_end,
            page_spans=page_map,
        )
        self.assertEqual(exact["status"], "verified_exact")

        normalized_chunk = "Scope 1, Scope 2"
        normalized_start, normalized_end = esg_chunker.locate_text_span(
            section, normalized_chunk
        )
        normalized = esg_chunker.validate_chunk_citation(
            parsed_text=parsed,
            parsed_text_sha256=digest,
            expected_parsed_text_sha256=digest,
            section_text=section,
            section_start=section_start,
            section_end=section_end,
            chunk_text=normalized_chunk,
            local_start=normalized_start,
            local_end=normalized_end,
            page_spans=page_map,
        )
        self.assertEqual(normalized["status"], "verified_whitespace_normalized")

    def test_invalid_bounds_and_missing_page_map_are_not_verified(self) -> None:
        parsed = "Climate evidence and target"
        digest = hashlib.sha256(parsed.encode("utf-8")).hexdigest()

        invalid = esg_chunker.validate_chunk_citation(
            parsed_text=parsed,
            parsed_text_sha256=digest,
            expected_parsed_text_sha256=digest,
            section_text=parsed,
            section_start=0,
            section_end=len(parsed) + 1,
            chunk_text="Climate",
            local_start=0,
            local_end=7,
            page_spans=[],
        )
        self.assertEqual(invalid["status"], "invalid_section_bounds")

        no_page = esg_chunker.validate_chunk_citation(
            parsed_text=parsed,
            parsed_text_sha256=digest,
            expected_parsed_text_sha256=digest,
            section_text=parsed,
            section_start=0,
            section_end=len(parsed),
            chunk_text="Climate",
            local_start=0,
            local_end=7,
            page_spans=[],
        )
        self.assertEqual(no_page["status"], "missing_page_mapping")
        self.assertNotIn(no_page["status"], esg_chunker.VERIFIED_CITATION_STATUSES)

    def test_v2_and_legacy_section_filenames_parse(self) -> None:
        self.assertEqual(
            esg_chunker.parse_section_filename(
                Path("AAP-Report-2021__community__0002.txt")
            ),
            ("AAP-Report-2021", "community__0002"),
        )
        self.assertEqual(
            esg_chunker.parse_section_filename(Path("AAP-Report-2021__community.txt")),
            ("AAP-Report-2021", "community"),
        )

    def test_repeated_code_instances_create_unique_verified_chunk_ids(self) -> None:
        encoder = tiktoken.get_encoding(esg_chunker.ENCODING)
        community_one = ("Community first evidence and measurable result. " * 45).strip()
        governance = ("Governance oversight and accountability. " * 10).strip()
        community_two = ("Community second evidence and measurable result. " * 45).strip()
        parsed = f"{community_one}\n\n{governance}\n\n{community_two}"
        first_start = 0
        first_end = len(community_one)
        second_start = parsed.index(community_two)
        second_end = second_start + len(community_two)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            section_root = root / "sections" / "AAP"
            section_root.mkdir(parents=True)
            parsed_file = root / "AAP-Report-2021.txt"
            parsed_file.write_text(parsed, encoding="utf-8")
            parsed_digest = hashlib.sha256(parsed_file.read_bytes()).hexdigest()
            page_map_file = root / "AAP-Report-2021.pages.csv"
            with page_map_file.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(
                    f, fieldnames=["page", "char_start", "char_end"]
                )
                writer.writeheader()
                writer.writerow(
                    {"page": 1, "char_start": 0, "char_end": len(parsed)}
                )

            first_file = section_root / "AAP-Report-2021__community__0001.txt"
            second_file = section_root / "AAP-Report-2021__community__0002.txt"
            first_file.write_text(community_one, encoding="utf-8")
            second_file.write_text(community_two, encoding="utf-8")
            metadata = {
                ("AAP", "AAP-Report-2021", "community__0001"): {
                    "section_code": "community",
                    "source_start_char": str(first_start),
                    "source_end_char": str(first_end),
                    "source_sha256": parsed_digest,
                },
                ("AAP", "AAP-Report-2021", "community__0002"): {
                    "section_code": "community",
                    "source_start_char": str(second_start),
                    "source_end_char": str(second_end),
                    "source_sha256": parsed_digest,
                },
            }
            doc_metadata = {
                ("AAP", "AAP-Report-2021"): {
                    "source_id": "AAP__2021__sustainability__01",
                    "source_version_id": "AAP__2021__sustainability__01__abc123",
                    "canonical_ticker": "AAP",
                    "doc_type": "sustainability",
                    "source_type": "sustainability",
                    "source_scope": "full_report",
                    "retrieval_tier": "primary",
                    "include_in_esg_index": True,
                    "doc_quality_status": "ok",
                    "rag_action": "index_as_esg",
                    "quality_flags": "",
                    "parsed_text_file": str(parsed_file),
                    "page_map_file": str(page_map_file),
                }
            }
            cache: dict[Path, tuple[str, str]] = {}
            first_plan = esg_chunker.build_section_plan(
                first_file, root / "chunks", encoder, metadata, doc_metadata, cache
            )
            second_plan = esg_chunker.build_section_plan(
                second_file, root / "chunks", encoder, metadata, doc_metadata, cache
            )

            first_ids = {output.row["chunk_id"] for output in first_plan.outputs}
            second_ids = {output.row["chunk_id"] for output in second_plan.outputs}
            self.assertTrue(first_ids)
            self.assertTrue(second_ids)
            self.assertTrue(first_ids.isdisjoint(second_ids))
            for output in [*first_plan.outputs, *second_plan.outputs]:
                self.assertEqual(output.row["citation_ready"], "true")
                self.assertEqual(
                    output.row["citation_validation_status"], "verified_exact"
                )
                self.assertEqual(
                    output.row["citation_validation_version"], "semantic_v1"
                )

    def test_meaningful_short_section_creates_citation_ready_short_evidence(self) -> None:
        encoder = tiktoken.get_encoding(esg_chunker.ENCODING)
        short_text = (
            "Environment\n"
            "We reduced packaging waste, expanded renewable electricity purchasing, "
            "tracked supplier energy performance across priority facilities, "
            "and reviewed progress with our sustainability steering team."
        )
        self.assertGreaterEqual(
            len(encoder.encode(short_text)),
            esg_chunker.SHORT_EVIDENCE_MIN_TOKENS,
        )
        self.assertLess(len(encoder.encode(short_text)), esg_chunker.MIN_CHUNK_TOKENS)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            section_root = root / "sections" / "TEST"
            section_root.mkdir(parents=True)
            parsed_file = root / "TEST-Report-2024.txt"
            with parsed_file.open("w", encoding="utf-8", newline="\n") as f:
                f.write(short_text)
            parsed_digest = hashlib.sha256(parsed_file.read_bytes()).hexdigest()
            page_map_file = root / "TEST-Report-2024.pages.csv"
            with page_map_file.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(
                    f, fieldnames=["page", "char_start", "char_end"]
                )
                writer.writeheader()
                writer.writerow(
                    {"page": 3, "char_start": 0, "char_end": len(short_text)}
                )

            section_file = section_root / "TEST-Report-2024__environmental__0001.txt"
            with section_file.open("w", encoding="utf-8", newline="\n") as f:
                f.write(short_text)
            metadata = {
                ("TEST", "TEST-Report-2024", "environmental__0001"): {
                    "section_code": "environmental",
                    "source_start_char": "0",
                    "source_end_char": str(len(short_text)),
                    "source_sha256": parsed_digest,
                },
            }
            doc_metadata = {
                ("TEST", "TEST-Report-2024"): {
                    "source_id": "TEST__2024__sustainability__01",
                    "source_version_id": "TEST__2024__sustainability__01__abc123",
                    "canonical_ticker": "TEST",
                    "doc_type": "sustainability",
                    "source_type": "sustainability",
                    "source_scope": "full_report",
                    "retrieval_tier": "primary",
                    "include_in_esg_index": True,
                    "doc_quality_status": "ok",
                    "rag_action": "index_as_esg",
                    "quality_flags": "",
                    "parsed_text_file": str(parsed_file),
                    "page_map_file": str(page_map_file),
                }
            }

            plan = esg_chunker.build_section_plan(
                section_file, root / "chunks", encoder, metadata, doc_metadata, {}
            )

            self.assertTrue(plan.is_short)
            self.assertEqual(plan.short_section_action, "preserved")
            self.assertEqual(len(plan.outputs), 1)
            output = plan.outputs[0]
            self.assertEqual(output.row["chunk_type"], "short_evidence")
            self.assertEqual(output.row["short_section_reason"], "meaningful_short_section")
            self.assertEqual(output.row["citation_ready"], "true")
            self.assertEqual(output.row["citation_validation_status"], "verified_exact")
            self.assertEqual(output.row["page_start"], "3")
            esg_chunker.commit_section_outputs(plan, root / "chunks")
            self.assertTrue(esg_chunker.section_is_complete(plan, [output.row], encoder))
            output.path.write_text(output.text + " tampered", encoding="utf-8")
            self.assertFalse(
                esg_chunker.section_rows_are_complete(plan, [output.row], encoder)
            )

    def test_table_of_contents_short_section_is_excluded_with_reason(self) -> None:
        encoder = tiktoken.get_encoding(esg_chunker.ENCODING)
        toc_text = (
            "Overview ................................................................ 1\n"
            "Climate ................................................................ 5\n"
            "Water .................................................................. 8\n"
            "Waste .................................................................. 9\n"
            "Governance ............................................................ 14"
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            section_root = root / "sections" / "TEST"
            section_root.mkdir(parents=True)
            parsed_file = root / "TEST-Report-2024.txt"
            with parsed_file.open("w", encoding="utf-8", newline="\n") as f:
                f.write(toc_text)
            parsed_digest = hashlib.sha256(parsed_file.read_bytes()).hexdigest()
            page_map_file = root / "TEST-Report-2024.pages.csv"
            with page_map_file.open("w", encoding="utf-8", newline="\n") as f:
                f.write("page,char_start,char_end,char_count\n")
                f.write(f"1,0,{len(toc_text)},{len(toc_text)}\n")
            section_file = section_root / "TEST-Report-2024__appendix__0001.txt"
            with section_file.open("w", encoding="utf-8", newline="\n") as f:
                f.write(toc_text)
            metadata = {
                ("TEST", "TEST-Report-2024", "appendix__0001"): {
                    "section_code": "appendix",
                    "source_start_char": "0",
                    "source_end_char": str(len(toc_text)),
                    "source_sha256": parsed_digest,
                },
            }
            doc_metadata = {
                ("TEST", "TEST-Report-2024"): {
                    "source_id": "TEST__2024__sustainability__01",
                    "source_version_id": "TEST__2024__sustainability__01__abc123",
                    "canonical_ticker": "TEST",
                    "doc_type": "sustainability",
                    "source_type": "sustainability",
                    "source_scope": "full_report",
                    "retrieval_tier": "primary",
                    "include_in_esg_index": True,
                    "doc_quality_status": "ok",
                    "rag_action": "index_as_esg",
                    "quality_flags": "",
                    "parsed_text_file": str(parsed_file),
                    "page_map_file": str(page_map_file),
                }
            }

            plan = esg_chunker.build_section_plan(
                section_file, root / "chunks", encoder, metadata, doc_metadata, {}
            )

            self.assertFalse(plan.is_unhandled_short)
            self.assertEqual(plan.short_section_action, "excluded")
            self.assertEqual(plan.short_section_reason, "table_of_contents_or_navigation")
            self.assertEqual(len(plan.outputs), 1)
            row = plan.outputs[0].row
            self.assertEqual(row["chunk_type"], "short_evidence")
            self.assertEqual(row["include_in_esg_index"], "false")
            self.assertEqual(row["rag_action"], "exclude_from_esg_index")
            self.assertEqual(row["citation_validation_status"], "verified_exact")
            self.assertEqual(row["citation_ready"], "true")

    def test_source_registry_marks_etsy_excerpt_without_excluding_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            parse_index = root / "parse.csv"
            registry = root / "registry.csv"
            with parse_index.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=[
                        "ticker",
                        "pdf_file",
                        "source_pdf",
                        "status",
                        "quality_flags",
                        "possible_wrong_doc_type",
                        "source_sha256",
                        "parsed_text_file",
                        "page_map_file",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "ticker": "ETSY",
                        "pdf_file": "ETSY-ETSY INC-2024.pdf",
                        "source_pdf": "ETSY-ETSY INC-2024.pdf",
                        "status": "parsed",
                        "quality_flags": "",
                        "possible_wrong_doc_type": "false",
                        "source_sha256": "a" * 64,
                    }
                )
            with registry.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=[
                        "source_id",
                        "observed_ticker",
                        "canonical_ticker",
                        "pdf_stem",
                        "source_type",
                        "source_scope",
                        "retrieval_tier",
                        "include_in_esg_index",
                        "duplicate_of_source_id",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "source_id": "ETSY__2024__annual_report_with_esg__01",
                        "observed_ticker": "ETSY",
                        "canonical_ticker": "ETSY",
                        "pdf_stem": "ETSY-ETSY INC-2024",
                        "source_type": "annual_report_with_esg",
                        "source_scope": "excerpt",
                        "retrieval_tier": "supplementary",
                        "include_in_esg_index": "true",
                    }
                )

            metadata = esg_chunker.load_doc_metadata(parse_index, registry)
            etsy = metadata[("ETSY", "ETSY-ETSY INC-2024")]
            self.assertEqual(etsy["source_id"], "ETSY__2024__annual_report_with_esg__01")
            self.assertEqual(etsy["source_type"], "annual_report_with_esg")
            self.assertEqual(etsy["source_scope"], "excerpt")
            self.assertTrue(etsy["include_in_esg_index"])
            self.assertEqual(etsy["rag_action"], "index_as_esg")

    def test_resume_rows_are_grouped_once_by_physical_section(self) -> None:
        rows = [
            {
                "ticker": "AAP",
                "pdf_stem": "AAP-Report-2021",
                "section_instance_id": "climate__0001",
                "chunk_index": "0",
            },
            {
                "ticker": "AAP",
                "pdf_stem": "AAP-Report-2021",
                "section_instance_id": "climate__0001",
                "chunk_index": "1",
            },
            {
                "ticker": "WMT",
                "pdf_stem": "WMT-Report-2024",
                "section_instance_id": "water__0001",
                "chunk_index": "0",
            },
            {"ticker": "", "pdf_stem": "", "section_instance_id": ""},
        ]

        grouped, unkeyed = esg_chunker.group_index_rows(rows)

        self.assertEqual(len(grouped[("AAP", "AAP-Report-2021", "climate__0001")]), 2)
        self.assertEqual(len(grouped[("WMT", "WMT-Report-2024", "water__0001")]), 1)
        self.assertEqual(unkeyed, [rows[-1]])
        self.assertCountEqual(esg_chunker.flatten_index_rows(grouped, unkeyed), rows)

    def test_chunk_files_are_indexed_with_one_directory_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir)
            ticker_dir = output_root / "AAP"
            ticker_dir.mkdir()
            first = ticker_dir / "AAP-Report-2021__climate__0001__chunk_0000.txt"
            second = ticker_dir / "AAP-Report-2021__climate__0001__chunk_0001.txt"
            other = ticker_dir / "AAP-Report-2021__water__0001__chunk_0000.txt"
            ignored = ticker_dir / "unrelated.txt"
            for path in (first, second, other, ignored):
                path.write_text("evidence", encoding="utf-8")

            climate_key = ("AAP", "AAP-Report-2021", "climate__0001")
            water_key = ("AAP", "AAP-Report-2021", "water__0001")
            grouped = esg_chunker.index_section_chunk_files(
                output_root,
                {climate_key, water_key},
            )

            self.assertEqual(grouped[climate_key], [first, second])
            self.assertEqual(grouped[water_key], [other])
            self.assertNotIn(ignored, [path for paths in grouped.values() for path in paths])


if __name__ == "__main__":
    unittest.main()
