from __future__ import annotations

import csv
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import esg_chunker
import esg_pipeline_qa
import section_splitter_esg


class ESGRAGQualityTests(unittest.TestCase):
    def _build_plan_for_text(
        self,
        text: str,
        *,
        ticker: str = "TEST",
        pdf_stem: str = "TEST-Report-2024",
        section_instance_id: str = "environmental__0001",
        section_code: str = "environmental",
    ) -> esg_chunker.SectionPlan:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            parsed_file = root / "parsed" / ticker / f"{pdf_stem}.txt"
            parsed_file.parent.mkdir(parents=True)
            with parsed_file.open("w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            parsed_digest = hashlib.sha256(parsed_file.read_bytes()).hexdigest()
            page_map_file = root / "parsed" / ticker / f"{pdf_stem}.pages.csv"
            page_map_file.write_text(
                "page,char_start,char_end,char_count\n"
                f"1,0,{len(text)},{len(text)}\n",
                encoding="utf-8",
            )
            section_file = (
                root
                / "sections"
                / ticker
                / f"{pdf_stem}__{section_instance_id}.txt"
            )
            section_file.parent.mkdir(parents=True)
            with section_file.open("w", encoding="utf-8", newline="\n") as f:
                f.write(text)

            return esg_chunker.build_section_plan(
                section_file,
                root / "chunks",
                esg_chunker.tiktoken.get_encoding(esg_chunker.ENCODING),
                {
                    (ticker, pdf_stem, section_instance_id): {
                        "section_code": section_code,
                        "source_start_char": "0",
                        "source_end_char": str(len(text)),
                        "source_sha256": parsed_digest,
                    }
                },
                {
                    (ticker, pdf_stem): {
                        "source_id": f"{ticker}__2024__sustainability__01",
                        "source_version_id": f"{ticker}__2024__sustainability__01__abc123",
                        "canonical_ticker": ticker,
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
                },
            )

    def test_report_year_filter_uses_matching_pdf_only(self) -> None:
        tracker = {"ticker": "ETSY", "report_year": "2024"}
        parse_rows = [
            {"ticker": "ETSY", "pdf_file": "ETSY-Etsy-2021.pdf", "source_pdf": "data/ETSY-Etsy-2021.pdf"},
            {"ticker": "ETSY", "pdf_file": "ETSY-Etsy-2024.pdf", "source_pdf": "data/ETSY-Etsy-2024.pdf"},
        ]

        matched = esg_pipeline_qa.filter_parse_rows_for_tracker(tracker, parse_rows)

        self.assertEqual([row["pdf_file"] for row in matched], ["ETSY-Etsy-2024.pdf"])

    def test_not_found_tracker_with_outputs_requires_cleanup(self) -> None:
        self.assertEqual(
            esg_pipeline_qa.status_for_row(
                tracker_status="not_found",
                pdf_count=1,
                parsed_count=1,
                ocr_required_count=0,
                failed_parse_count=0,
                section_count=1,
                chunk_count=1,
                invalid_chunk_count=0,
                doc_quality_status="ok",
                missing_citation_metadata_count=0,
            ),
            "tracker_needs_cleanup",
        )
        self.assertEqual(
            esg_pipeline_qa.status_for_row(
                tracker_status="not_found",
                pdf_count=0,
                parsed_count=0,
                ocr_required_count=0,
                failed_parse_count=0,
                section_count=0,
                chunk_count=0,
                invalid_chunk_count=0,
                doc_quality_status="",
                missing_citation_metadata_count=0,
            ),
            "not_found",
        )

    def test_wrong_doc_type_excludes_from_esg_rag(self) -> None:
        parse_row = {
            "status": "parsed",
            "quality_flags": "possible_10k",
            "possible_wrong_doc_type": "true",
        }

        self.assertEqual(
            esg_chunker.doc_quality_status(parse_row),
            "exclude_from_esg_rag",
        )
        self.assertEqual(
            esg_chunker.rag_action_for_status("exclude_from_esg_rag"),
            "exclude_from_esg_index",
        )
        self.assertEqual(
            esg_chunker.doc_type_for_parse_row(parse_row),
            "annual_report_with_esg",
        )

    def test_qa_registry_duplicate_exclusion_overrides_indexable_parse(self) -> None:
        doc_quality, rag_action, notes = esg_pipeline_qa.registry_aware_document_policy(
            registry_row={
                "source_type": "sustainability",
                "source_scope": "full_report",
                "retrieval_tier": "excluded",
                "include_in_esg_index": "false",
                "duplicate_of_source_id": "ARKO__2021__sustainability_report__01",
            },
            chunk_rows=[
                {
                    "include_in_esg_index": "false",
                    "rag_action": "exclude_from_esg_index",
                }
            ],
            doc_quality_status="ok",
            tracker_status="downloaded",
        )

        self.assertEqual(doc_quality, "source_registry_excluded")
        self.assertEqual(rag_action, "exclude_from_esg_index")
        self.assertIn("duplicate of ARKO__2021__sustainability_report__01", "; ".join(notes))

    def test_qa_registry_specialized_exclusion_overrides_indexable_parse(self) -> None:
        doc_quality, rag_action, notes = esg_pipeline_qa.registry_aware_document_policy(
            registry_row={
                "source_type": "program_impact_report",
                "source_scope": "topic_specific",
                "retrieval_tier": "specialized",
                "include_in_esg_index": "false",
            },
            chunk_rows=[
                {
                    "include_in_esg_index": "false",
                    "rag_action": "exclude_from_esg_index",
                }
            ],
            doc_quality_status="ok",
            tracker_status="downloaded",
        )

        self.assertEqual(doc_quality, "source_registry_excluded")
        self.assertEqual(rag_action, "exclude_from_esg_index")
        self.assertIn("specialized/topic-specific", "; ".join(notes))

    def test_qa_registry_primary_keeps_shoo_indexable_after_repair(self) -> None:
        doc_quality, rag_action, notes = esg_pipeline_qa.registry_aware_document_policy(
            registry_row={
                "source_type": "sustainability",
                "source_scope": "full_report",
                "retrieval_tier": "primary",
                "include_in_esg_index": "true",
            },
            chunk_rows=[
                {
                    "include_in_esg_index": "true",
                    "rag_action": "index_as_esg",
                }
            ],
            doc_quality_status="ok",
            tracker_status="downloaded",
        )

        self.assertEqual(doc_quality, "ok")
        self.assertEqual(rag_action, "index_as_esg")
        self.assertIn("tier=primary", "; ".join(notes))

    def test_qa_never_indexes_document_when_all_chunks_are_excluded(self) -> None:
        doc_quality, rag_action, notes = esg_pipeline_qa.registry_aware_document_policy(
            registry_row=None,
            chunk_rows=[
                {
                    "include_in_esg_index": "false",
                    "rag_action": "exclude_from_esg_index",
                }
            ],
            doc_quality_status="ok",
            tracker_status="downloaded",
        )

        self.assertEqual(doc_quality, "source_registry_excluded")
        self.assertEqual(rag_action, "exclude_from_esg_index")
        self.assertIn("QA cannot mark document index_as_esg", "; ".join(notes))

    def test_short_evidence_token_counts_are_valid_only_for_short_chunks(self) -> None:
        self.assertTrue(
            esg_pipeline_qa.valid_chunk_token_count(
                {"token_count": "50", "chunk_type": "short_evidence"}
            )
        )
        self.assertFalse(
            esg_pipeline_qa.valid_chunk_token_count(
                {"token_count": "50", "chunk_type": "normal"}
            )
        )
        self.assertFalse(
            esg_pipeline_qa.valid_chunk_token_count(
                {"token_count": "100", "chunk_type": "short_evidence"}
            )
        )

    def test_short_navigation_section_gets_trace_chunk_but_is_not_indexed(self) -> None:
        text = (
            "About This Report ................................................................ 3\n"
            "A Message from the CEO ....................................................... 4\n"
            "About CarMax ................................................................... 6\n"
            "Our Approach to ESG ................................................................. .8"
        )
        token_count = len(esg_chunker.tiktoken.get_encoding(esg_chunker.ENCODING).encode(text))

        self.assertGreaterEqual(token_count, esg_chunker.SHORT_EVIDENCE_MIN_TOKENS)
        self.assertLess(token_count, esg_chunker.MIN_CHUNK_TOKENS)
        self.assertEqual(
            esg_chunker.classify_short_section(text, token_count),
            (esg_chunker.SHORT_SECTION_ACTION_EXCLUDED, "table_of_contents_or_navigation"),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            parsed_file = root / "parsed" / "KMX" / "KMX-CARMAX INC-2024.txt"
            parsed_file.parent.mkdir(parents=True)
            parsed_file.write_bytes(text.encode("utf-8"))
            page_map_file = root / "parsed" / "KMX" / "KMX-CARMAX INC-2024_pages.csv"
            page_map_file.write_text(
                "page,char_start,char_end,char_count\n"
                f"1,0,{len(text)},{len(text)}\n",
                encoding="utf-8",
            )
            section_file = (
                root
                / "sections"
                / "KMX"
                / "KMX-CARMAX INC-2024__about_this_report__0001.txt"
            )
            section_file.parent.mkdir(parents=True)
            section_file.write_bytes(text.encode("utf-8"))

            plan = esg_chunker.build_section_plan(
                section_file,
                root / "chunks",
                esg_chunker.tiktoken.get_encoding(esg_chunker.ENCODING),
                {
                    (
                        "KMX",
                        "KMX-CARMAX INC-2024",
                        "about_this_report__0001",
                    ): {
                        "section_code": "about_this_report",
                        "source_start_char": "0",
                        "source_end_char": str(len(text)),
                        "source_sha256": "",
                    }
                },
                {
                    ("KMX", "KMX-CARMAX INC-2024"): {
                        "parsed_text_file": str(parsed_file),
                        "page_map_file": str(page_map_file),
                        "doc_quality_status": "ok",
                        "rag_action": "index_as_esg",
                        "include_in_esg_index": True,
                    }
                },
            )

        self.assertEqual(len(plan.outputs), 1)
        row = plan.outputs[0].row
        self.assertEqual(row["chunk_type"], "short_evidence")
        self.assertEqual(row["short_section_action"], esg_chunker.SHORT_SECTION_ACTION_EXCLUDED)
        self.assertEqual(row["include_in_esg_index"], "false")
        self.assertEqual(row["rag_action"], "exclude_from_esg_index")
        self.assertIn(esg_chunker.QUALITY_FLAG_SHORT_SECTION_EXCLUDED, row["quality_flags"])
        self.assertEqual(row["citation_validation_status"], "verified_exact")

    def test_meaningful_short_section_remains_indexable(self) -> None:
        text = (
            "Climate goals\n"
            "We reduced operational energy use and expanded renewable electricity coverage "
            "across stores, distribution centers, and offices during the reporting year."
        )
        token_count = len(esg_chunker.tiktoken.get_encoding(esg_chunker.ENCODING).encode(text))

        self.assertEqual(
            esg_chunker.classify_short_section(text, token_count),
            (esg_chunker.SHORT_SECTION_ACTION_PRESERVED, "meaningful_short_section"),
        )

    def test_normal_sized_toc_trace_is_excluded_up_to_150_tokens(self) -> None:
        encoder = esg_chunker.tiktoken.get_encoding(esg_chunker.ENCODING)
        text = "\n".join(
            [
                "2021 GLOBAL CORPORATE RESPONSIBILITY REPORT",
                "DELIVERING REAL VALUE EVERY DAY",
                "Overview  CEO Letter  Workplace  Communities  Environment  Responsible Business  Governance  Appendix",
                "TABLE OF CONTENTS",
                "Overview ................................................................ 2",
                "Letter from our CEO and President ........................ 6",
                "Our Workplace ....................................................... 8",
                "Our Communities ................................................. 29",
                "Environmental Sustainability ............................... 46",
                "Responsible Business .......................................... 71",
                "Governance .......................................................... 96",
                "Appendix ........................................................... 118",
            ]
        )
        token_count = len(encoder.encode(text))

        self.assertGreaterEqual(token_count, esg_chunker.MIN_CHUNK_TOKENS)
        self.assertLessEqual(token_count, esg_chunker.NAVIGATION_TRACE_MAX_TOKENS)
        plan = self._build_plan_for_text(
            text,
            ticker="TJX",
            pdf_stem="TJX-Tjx Cos Inc-2021",
            section_instance_id="other__0001",
            section_code="other",
        )

        self.assertFalse(plan.is_short)
        self.assertEqual(plan.short_section_action, esg_chunker.SHORT_SECTION_ACTION_EXCLUDED)
        self.assertEqual(plan.short_section_reason, "table_of_contents_or_navigation")
        self.assertEqual(len(plan.outputs), 1)
        row = plan.outputs[0].row
        self.assertEqual(row["chunk_type"], esg_chunker.CHUNK_TYPE_NORMAL)
        self.assertEqual(row["short_section_action"], esg_chunker.SHORT_SECTION_ACTION_EXCLUDED)
        self.assertEqual(row["include_in_esg_index"], "false")
        self.assertEqual(row["rag_action"], "exclude_from_esg_index")
        self.assertIn(esg_chunker.QUALITY_FLAG_SHORT_SECTION_EXCLUDED, row["quality_flags"])
        self.assertEqual(row["citation_validation_status"], "verified_exact")

    def test_normal_sized_genuine_esg_prose_remains_eligible(self) -> None:
        encoder = esg_chunker.tiktoken.get_encoding(esg_chunker.ENCODING)
        text = (
            "Climate strategy\n"
            "We reduced operational energy use across stores and distribution centers while "
            "expanding renewable electricity coverage. Our sustainability team reviewed "
            "progress with business leaders, evaluated supplier energy performance, and "
            "prioritized projects that lower emissions without reducing service quality. "
            "During the reporting year, we improved building controls, trained facility "
            "teams, and used utility data to identify additional efficiency opportunities. "
            "The program also improved associate engagement by sharing monthly results, "
            "assigning accountable owners, and reviewing capital projects through a "
            "cross-functional governance process. Leaders used those reviews to approve "
            "new site-level reduction plans."
        )
        token_count = len(encoder.encode(text))

        self.assertGreaterEqual(token_count, esg_chunker.MIN_CHUNK_TOKENS)
        self.assertLessEqual(token_count, esg_chunker.NAVIGATION_TRACE_MAX_TOKENS)
        plan = self._build_plan_for_text(text)

        self.assertFalse(plan.is_short)
        self.assertEqual(plan.short_section_action, "")
        self.assertEqual(len(plan.outputs), 1)
        row = plan.outputs[0].row
        self.assertEqual(row["chunk_type"], esg_chunker.CHUNK_TYPE_NORMAL)
        self.assertEqual(row["short_section_action"], "")
        self.assertEqual(row["include_in_esg_index"], "true")
        self.assertEqual(row["rag_action"], "index_as_esg")
        self.assertEqual(row["citation_validation_status"], "verified_exact")

    def test_normalized_chunk_span_lookup_tolerates_whitespace(self) -> None:
        source = "Climate goals include Scope 1,\nScope 2, and Scope 3 emissions."
        needle = "Scope 1, Scope 2, and Scope 3"

        start, end = esg_chunker.locate_text_span(source, needle)

        self.assertIsNotNone(start)
        self.assertIsNotNone(end)
        self.assertIn("Scope 1", source[start:end])
        self.assertIn("Scope 3", source[start:end])

    def test_repeated_section_codes_remain_separate_contiguous_instances(self) -> None:
        def long_body(label: str) -> str:
            return (f"{label} action and measurable progress. " * 20).strip()

        text = "\r\n".join(
            [
                "Climate",
                long_body("First climate"),
                "Community",
                long_body("Community"),
                "Climate",
                long_body("Second climate"),
            ]
        )

        sections = section_splitter_esg._output_sections(text)

        self.assertEqual(
            [section.section_code for section in sections],
            ["climate", "community", "climate"],
        )
        self.assertEqual(
            [section.section_instance_id for section in sections],
            ["climate__0001", "community__0001", "climate__0002"],
        )
        for section in sections:
            self.assertEqual(
                text[section.source_start_char : section.source_end_char],
                section.text,
            )
        self.assertLess(sections[0].source_end_char, sections[2].source_start_char)

    def test_only_adjacent_same_code_sections_coalesce(self) -> None:
        def long_body(label: str) -> str:
            return (f"{label} evidence and measurable community progress. " * 20).strip()

        text = "\n\n".join(
            [
                f"Community\n{long_body('First')}",
                f"Community Partnerships\n{long_body('Subheading')}",
                f"Governance\n{long_body('Governance')}",
                f"Community\n{long_body('Final')}",
            ]
        )

        sections = section_splitter_esg._output_sections(text)

        self.assertEqual(
            [section.section_code for section in sections],
            ["community", "governance", "community"],
        )
        self.assertEqual(
            [section.section_instance_id for section in sections],
            ["community__0001", "governance__0001", "community__0002"],
        )
        self.assertIn("Community Partnerships", sections[0].text)
        self.assertNotIn("Governance", sections[0].text)
        for section in sections:
            self.assertEqual(
                text[section.source_start_char : section.source_end_char],
                section.text,
            )

    def test_section_rows_use_instance_ids_and_exact_provenance(self) -> None:
        def long_body(label: str) -> str:
            return (f"{label} result with supporting evidence. " * 20).strip()

        source_text = "\n".join(
            [
                "Climate",
                long_body("First"),
                "Community",
                long_body("Middle"),
                "Climate",
                long_body("Last"),
            ]
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            parsed_file = root / "parsed" / "TEST" / "TEST-Report-2024.txt"
            parsed_file.parent.mkdir(parents=True)
            parsed_file.write_text(source_text, encoding="utf-8")
            output_root = root / "sections"

            rows = section_splitter_esg.process_text_file(parsed_file, output_root)
            normalized_source = parsed_file.read_text(encoding="utf-8")

            self.assertEqual(len(rows), 3)
            self.assertEqual(
                [row["section_instance_id"] for row in rows],
                ["climate__0001", "community__0001", "climate__0002"],
            )
            for row in rows:
                self.assertEqual(row["provenance_version"], "contiguous_v1")
                section_file = Path(row["section_file"])
                self.assertTrue(section_file.is_file())
                self.assertTrue(section_file.name.endswith(f"__{row['section_instance_id']}.txt"))
                self.assertEqual(
                    normalized_source[row["source_start_char"] : row["source_end_char"]],
                    section_file.read_text(encoding="utf-8"),
                )

    def test_section_discovery_can_filter_one_exact_pdf_stem(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            input_root = Path(temp_dir)
            ticker_dir = input_root / "AAP"
            ticker_dir.mkdir()
            wanted = ticker_dir / "AAP-Advance Auto Parts-2021.txt"
            wanted.touch()
            (ticker_dir / "AAP-Advance Auto Parts-2022.txt").touch()

            discovered = section_splitter_esg.discover_text_files(
                input_root,
                ticker="AAP",
                pdf_stem="AAP-Advance Auto Parts-2021",
            )

            self.assertEqual(discovered, [wanted])

    def test_scoped_upsert_preserves_unrelated_legacy_section_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            index_path = Path(temp_dir) / "esg_sections_index.csv"
            with index_path.open("w", newline="", encoding="utf-8") as index_file:
                writer = csv.DictWriter(
                    index_file,
                    fieldnames=["ticker", "pdf_stem", "section_code", "section_file"],
                )
                writer.writeheader()
                writer.writerows(
                    [
                        {
                            "ticker": "WMT",
                            "pdf_stem": "WMT-Walmart-2024",
                            "section_code": "climate",
                            "section_file": "legacy-wmt.txt",
                        },
                        {
                            "ticker": "AAP",
                            "pdf_stem": "AAP-Advance Auto Parts-2021",
                            "section_code": "climate",
                            "section_file": "legacy-aap.txt",
                        },
                    ]
                )

            section_splitter_esg.upsert_index(
                index_path,
                new_rows=[
                    {
                        "ticker": "AAP",
                        "pdf_stem": "AAP-Advance Auto Parts-2021",
                        "section_instance_id": "climate__0001",
                        "section_code": "climate",
                        "section_file": "pilot-aap.txt",
                        "provenance_version": "contiguous_v1",
                    }
                ],
                processed_keys={("AAP", "AAP-Advance Auto Parts-2021")},
            )

            rows = section_splitter_esg.read_existing_index(index_path)
            by_document = {(row["ticker"], row["pdf_stem"]): row for row in rows}

            self.assertEqual(len(rows), 2)
            preserved = by_document[("WMT", "WMT-Walmart-2024")]
            self.assertEqual(preserved["section_instance_id"], "climate__0001")
            self.assertEqual(preserved["section_file"], "legacy-wmt.txt")
            self.assertEqual(
                by_document[("AAP", "AAP-Advance Auto Parts-2021")]["section_file"],
                "pilot-aap.txt",
            )


if __name__ == "__main__":
    unittest.main()
