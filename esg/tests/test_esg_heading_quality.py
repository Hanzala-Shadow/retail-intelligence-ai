import re
import sys
import unittest
from pathlib import Path
from unittest import mock


import esg_chunker
import section_splitter_esg


AMZN_SCOPE3_ROWS = [
    "| Greenhouse Gas Protocol Aligned Scope 3 Categories |  | 2022 | 2023 |",
    "| --- | --- | --- | --- |",
    "| Purchased Goods and Services (Amazon corporate purchases made for Amazon’s operations and services, Amazon-branded products) |  | 20.60 | 19.86 |",
    "| Capital Goods |  | 10.25 | 8.95 |",
    "| Fuel- and Energy-Related Activities |  | 4.76 | 4.97 |",
    "| Upstream Transportation and Distribution |  | 10.65 | 9.30 |",
    "| Business Travel |  | 0.61 | 0.63 |",
    "| Employee Commuting |  | 2.78 | 2.88 |",
    "| Downstream Transportation and Distribution |  | 3.41 | 3.63 |",
    "| Use of Sold Products (Amazon Devices) |  | 1.18 | 1.50 |",
    "| End-of-Life Treatment of Sold Products (Amazon Devices) |  | 0.04 | 0.04 |",
]


class WordTokenizer:
    @staticmethod
    def encode(text: str, **_: object) -> list[int]:
        return list(range(len(re.findall(r"\w+|[^\w\s]", text))))


def paged_text(pages: list[str]) -> tuple[str, list[dict]]:
    text_parts: list[str] = []
    page_spans: list[dict] = []
    offset = 0
    for page_number, page in enumerate(pages, start=1):
        page_text = page.rstrip("\n")
        start = offset
        text_parts.append(page_text)
        offset += len(page_text)
        page_spans.append(
            {
                "page": str(page_number),
                "char_start": str(start),
                "char_end": str(offset),
                "char_count": str(len(page_text)),
            }
        )
        text_parts.append("\n")
        offset += 1
    return "".join(text_parts), page_spans


class EsgHeadingQualityTests(unittest.TestCase):
    def test_pipeline_run_defaults_to_frozen_sectioner(self):
        expected = [{"section_instance_id": "safe__0001"}]
        with mock.patch("section_splitter_esg_legacy.run", return_value=expected) as run:
            actual = section_splitter_esg.run("input", "output", "index")
        self.assertEqual(actual, expected)
        run.assert_called_once()
        self.assertFalse(run.call_args.kwargs["force"])

    def test_amzn_scope3_markdown_rows_stay_in_one_section(self):
        table = "\n".join(AMZN_SCOPE3_ROWS)
        text = (
            "Carbon Footprint\n"
            "Amazon reports its full greenhouse gas inventory below.\n"
            f"{table}\n"
            "Fuel and Energy\n"
            "This prose section describes energy efficiency projects."
        )

        candidates = section_splitter_esg.collect_heading_candidates(text)
        titles = [candidate.title for candidate in candidates]
        self.assertNotIn(AMZN_SCOPE3_ROWS[0], titles)
        self.assertFalse(any("Fuel- and Energy-Related Activities" in title for title in titles))
        self.assertFalse(any("End-of-Life Treatment of Sold Products" in title for title in titles))
        self.assertIn("Fuel and Energy", titles)

        sections = section_splitter_esg.split_esg_sections(text)
        table_sections = [section for section in sections if AMZN_SCOPE3_ROWS[0] in section.text]
        self.assertEqual(len(table_sections), 1)
        table_section = table_sections[0]
        for row in AMZN_SCOPE3_ROWS:
            self.assertEqual(table_section.text.count(row), 1, row)
            self.assertEqual(text.count(row), 1, row)
        self.assertLess(
            table_section.text.index(AMZN_SCOPE3_ROWS[0]),
            table_section.text.index(AMZN_SCOPE3_ROWS[4]),
        )

        self.assertEqual(sections[0].source_start_char, 0)
        self.assertEqual(sections[-1].source_end_char, len(text))
        for section in sections:
            self.assertEqual(text[section.source_start_char:section.source_end_char], section.text)
        for left, right in zip(sections, sections[1:]):
            self.assertLessEqual(left.source_end_char, right.source_start_char)
            self.assertFalse(text[left.source_end_char:right.source_start_char].strip())

    def test_pipe_character_alone_does_not_block_a_real_heading(self):
        self.assertEqual(
            section_splitter_esg.map_heading_to_code("Fuel and Energy | Strategy"),
            "energy",
        )

    def test_later_table_chunk_carries_the_true_markdown_header(self):
        header = AMZN_SCOPE3_ROWS[0]
        separator = AMZN_SCOPE3_ROWS[1]
        data_rows = [
            f"| Category {index} with purchased products and transportation activity |  | {index}.25 | {index}.50 |"
            for index in range(1, 81)
        ]
        text = "Carbon Footprint\n" + "\n".join([header, separator, *data_rows])
        metadata = {
            "company_name": "AMAZON.COM INC",
            "ticker": "AMZN",
            "doc_type": "sustainability",
            "report_year": "2023",
            "section_code": "emissions",
            "section_title": "Carbon Footprint",
            "physical_section_title": "Carbon Footprint",
            "section_title_original": "Carbon Footprint",
        }
        tokenizer = WordTokenizer()

        chunks = esg_chunker.chunk_section_v3(text, metadata, tokenizer, tokenizer, [])

        later_table_chunks = [
            chunk
            for chunk in chunks
            if chunk.source_start > text.index(header) and "| Category" in chunk.text
        ]
        self.assertGreaterEqual(len(later_table_chunks), 1)
        expected_context = " ".join(header.split())
        self.assertTrue(
            all(chunk.table_context == expected_context for chunk in later_table_chunks)
        )
        self.assertTrue(all(chunk.table_header_start == text.index(header) for chunk in later_table_chunks))

    def test_numeric_value_is_not_used_as_table_context(self):
        tokenizer = WordTokenizer()
        self.assertEqual(esg_chunker._credible_table_context("11,748", tokenizer), "")

    def test_new_table_does_not_inherit_the_prior_table_header(self):
        prior_header = "| Carbon Intensity | 2019 | 2020 | 2021 | 2022 | 2023 | YoY% |"
        prior_rows = [
            f"| Prior carbon category {index} with a longer descriptive label | {index} | {index} | {index} | {index} | {index} | -3% |"
            for index in range(1, 31)
        ]
        scope_header = AMZN_SCOPE3_ROWS[0]
        scope_rows = [
            f"| Scope 3 category {index} with purchased goods and transport |  | {index}.25 | {index}.50 |"
            for index in range(1, 31)
        ]
        text = "\n".join(
            [
                "Amazon’s Carbon Footprint",
                prior_header,
                "| --- | --- | --- | --- | --- | --- | --- |",
                *prior_rows,
                "",
                scope_header,
                AMZN_SCOPE3_ROWS[1],
                *scope_rows,
            ]
        )
        metadata = {
            "company_name": "AMAZON.COM INC",
            "ticker": "AMZN",
            "doc_type": "sustainability",
            "report_year": "2023",
            "section_code": "emissions",
            "section_title": "Amazon’s Carbon Footprint",
            "physical_section_title": "Amazon’s Carbon Footprint",
            "section_title_original": "Amazon’s Carbon Footprint",
        }
        tokenizer = WordTokenizer()

        chunks = esg_chunker.chunk_section_v3(text, metadata, tokenizer, tokenizer, [])

        scope_header_chunk = next(chunk for chunk in chunks if scope_header in chunk.text)
        self.assertIn(scope_rows[0], scope_header_chunk.text)

        scope_data_start = text.index(scope_header) + len(scope_header)
        scope_chunks = [
            chunk
            for chunk in chunks
            if chunk.source_start > scope_data_start and "| Scope 3 category" in chunk.text
        ]
        expected_context = " ".join(scope_header.split())
        self.assertTrue(scope_chunks)
        self.assertTrue(all(chunk.table_context == expected_context for chunk in scope_chunks))
        self.assertTrue(all(chunk.table_context != prior_header for chunk in scope_chunks))

    def test_prose_fragments_and_table_rows_are_not_headings(self):
        false_headings = [
            "For instance, as we work to become a net zero",
            "See chapters: Supporting Our Team Members, Serving and Strengthening Communities",
            "Promoting and Protecting Human Rights p. 63–64",
            "» Verified the Apple reportable F-GHG avoided emissions by",
            "Our zero-waste and carbon reduction initiatives have had an",
            "The composition of other recycled materials can",
            "GHG emissions 2021 2022 2023",
            "Access to products Introduction Environmental Social Governance Indexes and Glossary",
            "Post-Industrial Recycled into NIKE Products 46,220 50,569",
            "Disclose the metrics used by the organization to assess climate",
            "Compliance Program is inspired by the United Nations Guiding Principles",
            "Our goal is consistent with the Intergovernmental Panel on Climate",
            "Since 2018, we've powered every Apple facility with renewable",
            "As of March 31, 2024, our Board of Directors",
            "Energy Saved (MJ)",
            "GRI 407: Freedom of Association and Collective Bargaining",
            "• HIPAA • Water Policy",
            "« The verification statement issued by an independent third-party",
            "Greenhouse gas (GHG) emissions quantification is subject to significant",
            "Kaniti project works with seven indigenous communities to conserve 119,837 hectares",
            "In addition to supporting growth in the diversity",
            "GHG EMISSIONS (WITH CARBON GHG EMISSIONS (WITH CARBON",
            "TCFD Change in scope 3 GHG emissions from 2017 baseline 19.9%17 12.0%18 -1.6% Target goal",
            "WATER SAVED (LITERS OF WATER) WATER SAVED (LITERS OF WATER)",
            "Improvement First-Year Energy Savings (MWh) Conversion Factor mtCO2e",
            "Percentage of Quantitative Percentage CG-MR- Gender: Pg. 18 - Inclusion",
            "Compliance Initiative (BSCI), Worldwide Responsible Accredited Production (WRAP), and Sedex",
            "Member of the Corporate Governance member of the Finance Committee",
            "Renewable Energy Goals, Sustainable Operations, Supply Chain Environmental Responsibility",
            "GHG EMISSIONS (WITH CARBON UPTAKE)",
            "(RAW MATERIAL) IMPACT TOTAL",
            "Development’s (WBCSD) Greenhouse Gas Protocol Initiative’s Corporate",
            "Black communities, Indigenous communities and other communities",
            "Manufacturing Factory",
            "V&L Packaging No. 11, Tan Lien Industrial",
        ]
        for line in false_headings:
            with self.subTest(line=line):
                self.assertIsNone(section_splitter_esg.map_heading_to_code(line))

    def test_real_short_headings_remain_eligible(self):
        expected = {
            "Climate Action": "climate",
            "Supply Chain Labor & Human Rights Policies": "supply_chain_ethics",
            "Representation": "diversity_equity_inclusion",
            "Materials": "environmental",
            "Stakeholder Engagement": "governance",
            "Achieve carbon neutrality for our entire carbon footprint": "emissions",
            "How Apple conducts our product greenhouse gas life cycle assessment": "emissions",
        }
        for line, section_code in expected.items():
            with self.subTest(line=line):
                self.assertEqual(section_splitter_esg.map_heading_to_code(line), section_code)

    def test_repeated_navigation_chrome_is_removed(self):
        pages = [
            "Introduction 2025 Targets Our Approach Appendix\n"
            f"Climate Action\nPage {page} climate body " + ("evidence " * 60)
            for page in range(1, 6)
        ]
        text, page_spans = paged_text(pages)
        candidates = section_splitter_esg.collect_heading_candidates(text, page_spans=page_spans)
        titles = [candidate.title for candidate in candidates]

        self.assertNotIn("Introduction 2025 Targets Our Approach Appendix", titles)
        self.assertEqual(titles.count("Climate Action"), 1)

    def test_repeated_ribbon_occurrences_do_not_block_later_real_heading(self):
        ribbon_pages = [
            "Overview Community Environment Governance Appendix\n"
            "Environmental\n"
            "Social\n"
            "Governance\n"
            f"Page {page} body " + ("evidence " * 60)
            for page in range(1, 5)
        ]
        pages = [
            *ribbon_pages,
            "Environmental\n"
            "Our environmental program has clear goals and measured results. "
            + ("The company reports progress in normal narrative prose. " * 20),
        ]
        text, page_spans = paged_text(pages)
        candidates = section_splitter_esg.collect_heading_candidates(text, page_spans)
        environmental = [item for item in candidates if item.title == "Environmental"]

        self.assertEqual(len(environmental), 1)
        self.assertEqual(int(page_spans[4]["char_start"]), environmental[0].char_offset)

    def test_distant_bottom_ribbon_occurrences_are_removed(self):
        pages = [
            ("Narrative evidence " * 80)
            + "\nOur Customers\nOur Employees\nOur Communities\nOur Environment\n1",
            "Climate Action\n" + ("transition evidence " * 80),
            "Water Stewardship\n" + ("watershed evidence " * 80),
            "Waste and Circularity\n" + ("circularity evidence " * 80),
            ("Narrative evidence " * 80)
            + "\nOur Customers\nOur Employees\nOur Communities\nOur Environment\n5",
        ]
        text, page_spans = paged_text(pages)
        titles = [
            item.title
            for item in section_splitter_esg.collect_heading_candidates(text, page_spans)
        ]

        self.assertNotIn("Our Communities", titles)

    def test_repeated_index_rows_are_not_classified_as_ribbon(self):
        pages = [
            "Impact topics  Circularity and New Guest Models  47\n"
            "Climate Action  34\nWater and Chemistry  50",
            "Material Topics\nCircularity and New Guest Models\n"
            "Product Innovation\nCircularity and New Guest Models",
            "Material Topics\nProduct Innovation\n"
            "Circularity and New Guest Models\nSASB Index",
        ]
        text, page_spans = paged_text(pages)
        lines_with_endings = text.splitlines(keepends=True)
        lines = [line.rstrip("\r\n") for line in lines_with_endings]
        raw = []
        offset = 0
        for line_index, line_with_ending in enumerate(lines_with_endings):
            line = line_with_ending.rstrip("\r\n")
            code = section_splitter_esg.map_heading_to_code(line)
            if code:
                raw.append(
                    section_splitter_esg.HeadingCandidate(
                        line_index,
                        offset,
                        code,
                        section_splitter_esg.normalize_heading_text(line),
                        section_splitter_esg.has_page_reference(line),
                    )
                )
            offset += len(line_with_ending)

        positions = section_splitter_esg._candidate_page_positions(raw, page_spans)
        page_ends = {
            int(row["page"]): int(row["char_end"])
            for row in page_spans
        }
        ribbon_indexes = section_splitter_esg._local_navigation_ribbon_indexes(
            raw, positions, page_ends, lines
        )

        self.assertFalse(
            any(
                index in ribbon_indexes
                and candidate.title == "Circularity and New Guest Models"
                for index, candidate in enumerate(raw)
            )
        )

    def test_page_header_run_does_not_merge_later_real_occurrence(self):
        pages = [
            "Climate Action\nFirst chapter body " + ("climate evidence " * 45),
            "Climate Action\nContinuation body " + ("operational evidence " * 45),
            "Water Stewardship\nWater body " + ("water evidence " * 45),
            "Water Stewardship\nWater continuation " + ("watershed evidence " * 45),
            "Waste and Circularity\nWaste body " + ("waste evidence " * 45),
            "Climate Action\nA separate later topic " + ("transition evidence " * 45),
        ]
        text, page_spans = paged_text(pages)
        candidates = section_splitter_esg.collect_heading_candidates(text, page_spans=page_spans)
        climate_candidates = [candidate for candidate in candidates if candidate.title == "Climate Action"]
        self.assertEqual(len(climate_candidates), 2)

        sections = section_splitter_esg.split_esg_sections(text, page_spans=page_spans)
        climate_sections = [section for section in sections if section.section_code == "climate"]
        self.assertEqual(len(climate_sections), 2)
        for section in sections:
            self.assertEqual(
                text[section.source_start_char : section.source_end_char],
                section.text,
            )

    def test_mid_page_table_header_run_keeps_only_first_copy(self):
        padding = "Background evidence " * 40
        pages = [
            f"{padding}\nGHG Emissions\nFirst table body " + ("measurement " * 40),
            f"{padding}\nGHG Emissions\nContinued table body " + ("measurement " * 40),
            "Water Stewardship\nWater evidence " + ("watershed " * 40),
            "Waste and Circularity\nWaste evidence " + ("circularity " * 40),
            "Climate Action\nClimate evidence " + ("transition " * 40),
            "GHG Emissions\nSeparate inventory topic " + ("inventory " * 40),
        ]
        text, page_spans = paged_text(pages)
        candidates = section_splitter_esg.collect_heading_candidates(text, page_spans=page_spans)
        emissions = [candidate for candidate in candidates if candidate.title == "GHG Emissions"]
        self.assertEqual(len(emissions), 2)

    def test_page_number_before_repeated_header_does_not_create_section(self):
        pages = [
            f"{page}\nEnvironmental Responsibility\n" + ("environment evidence " * 60)
            for page in range(4, 8)
        ]
        text, page_spans = paged_text(pages)
        titles = [
            candidate.title
            for candidate in section_splitter_esg.collect_heading_candidates(
                text, page_spans=page_spans
            )
        ]
        self.assertNotIn("Environmental Responsibility", titles)

    def test_repeated_footer_does_not_create_section(self):
        pages = [
            ("governance evidence " * 60) + f"\nGovernance\nPage {page}"
            for page in range(1, 5)
        ]
        text, page_spans = paged_text(pages)
        titles = [
            candidate.title
            for candidate in section_splitter_esg.collect_heading_candidates(
                text, page_spans=page_spans
            )
        ]
        self.assertNotIn("Governance", titles)

    def test_table_cells_and_index_rows_are_not_sections(self):
        text = (
            "Segregated 1% 7% 1%\n"
            "Materials\n"
            "Mass Balance 72% 82% 45%\n"
            "Effluents and Waste 2016 p. 13-17\n"
            "Waste & Circular Economy\n"
            "Product and packaging design p. 15-16\n"
        )
        titles = [candidate.title for candidate in section_splitter_esg.collect_heading_candidates(text)]
        self.assertNotIn("Materials", titles)
        self.assertNotIn("Waste & Circular Economy", titles)

    def test_real_heading_followed_by_prose_is_not_a_table_cell(self):
        text = "Materials\nOur material strategy prioritizes durable and recycled inputs across product lines."
        titles = [candidate.title for candidate in section_splitter_esg.collect_heading_candidates(text)]
        self.assertIn("Materials", titles)

    def test_gri_index_topic_cell_is_not_a_heading(self):
        text = (
            "GRI Standard Number GRI Disclosure Location and Notes Omission SDG Mapping\n"
            "Water\n"
            "Material Aspects: Air and Water Pollution\n"
        )
        titles = [candidate.title for candidate in section_splitter_esg.collect_heading_candidates(text)]
        self.assertNotIn("Water", titles)

    def test_multi_topic_navigation_variants_are_not_boundaries(self):
        pages = [
            "Introduction Climate Action Circular Economy\n" + ("report body " * 70),
            "Introduction Climate Action Circular Economy Digital Inclusion\n" + ("report body " * 70),
            "Introduction Climate Action Circular Economy Inclusive Workforce\n" + ("report body " * 70),
        ]
        text, page_spans = paged_text(pages)
        titles = [item.title for item in section_splitter_esg.collect_heading_candidates(text, page_spans)]
        self.assertFalse(any(title.startswith("Introduction Climate Action") for title in titles))

    def test_breadcrumb_variants_are_not_supply_chain_sections(self):
        pages = [
            "People in Supply Chains Animal Welfare Product Supply Chain Sustainability\n" + ("evidence " * 70),
            "People in Supply Chains Product Supply Chain Sustainability Animal Welfare\n" + ("evidence " * 70),
            "People in Supply Chains Animal Welfare Product Supply Chain Sustainability\n" + ("evidence " * 70),
        ]
        text, page_spans = paged_text(pages)
        sections = section_splitter_esg.split_esg_sections(text, page_spans)
        self.assertFalse(any(item.section_code == "supply_chain_ethics" for item in sections))

    def test_repeated_uppercase_table_headers_do_not_create_boundaries(self):
        pages = [
            "CATEGORY / STATE EMPLOYEES BRANDS\nFull-time 100 4\n" + ("workforce evidence " * 55),
            "CATEGORY / STATE EMPLOYEES BRANDS\nPart-time 50 3\n" + ("workforce evidence " * 55),
            "CATEGORY / STATE EMPLOYEES BRANDS\nSeasonal 25 2\n" + ("workforce evidence " * 55),
        ]
        text, page_spans = paged_text(pages)
        titles = [item.title for item in section_splitter_esg.collect_heading_candidates(text, page_spans)]
        self.assertNotIn("CATEGORY / STATE EMPLOYEES BRANDS", titles)

    def test_real_repeated_energy_subtopics_keep_spans_and_instance_ids(self):
        pages = [
            "Energy\n" + ("renewable electricity evidence " * 70),
            "Water Stewardship\n" + ("watershed evidence " * 70),
            "Waste and Circularity\n" + ("circularity evidence " * 70),
            "Energy\n" + ("energy efficiency evidence " * 70),
            "Governance\n" + ("oversight evidence " * 70),
            "Social Impact\n" + ("community evidence " * 70),
            "Energy\n" + ("supplier energy evidence " * 70),
        ]
        text, page_spans = paged_text(pages)
        sections = section_splitter_esg.split_esg_sections(text, page_spans)
        energy = [item for item in sections if item.section_code == "energy"]
        self.assertEqual(len(energy), 3)
        for left, right in zip(sections, sections[1:]):
            self.assertLessEqual(left.source_end_char, right.source_start_char)
            self.assertFalse(text[left.source_end_char:right.source_start_char].strip())
        for item in sections:
            self.assertEqual(text[item.source_start_char:item.source_end_char], item.text)
        assigned = section_splitter_esg._assign_section_instance_ids(sections)
        self.assertEqual(
            [item.section_instance_id for item in assigned if item.section_code == "energy"],
            ["energy__0001", "energy__0002", "energy__0003"],
        )

    def test_narrative_headings_are_preserved_in_title_case_and_caps(self):
        cases = {
            "BETTERING THE LIVES OF PEOPLE IN OUR SUPPLY CHAIN": "supply_chain_ethics",
            "CLIMATE CHANGE, ENERGY AND EMISSIONS": "emissions",
            "BOARD OF DIRECTORS / RISK MANAGEMENT / CSR PROGRAM GOVERNANCE": "governance",
        }
        body = (
            "This section explains the company program and its oversight. "
            "It describes targets, responsibilities, and progress in normal narrative prose. "
        ) * 8
        for title, code in cases.items():
            with self.subTest(title=title):
                sections = section_splitter_esg.split_esg_sections(f"{title}\n\n{body}")
                self.assertEqual(sections[0].section_code, code)
                self.assertEqual(sections[0].title, title)
                self.assertEqual(
                    [span.title for span in sections[0].subsection_spans], [title]
                )

    def test_adjacent_community_headings_keep_context_without_physical_boundaries(self):
        body = (
            "This program has clear goals, partners, and measured community results. "
            "Teams report progress each year through normal narrative prose. "
        ) * 8
        cases = [
            ("Store Community Outreach", "Nonprofit Partner Spotlights"),
            ("Community", "Feeding Our Communities Partners"),
            ("Blue Star Families - a national nonprofit", "Social Impact Council"),
        ]
        for first_title, second_title in cases:
            with self.subTest(first=first_title, second=second_title):
                text = f"{first_title}\n\n{body}\n\n{second_title}\n\n{body}"
                sections = section_splitter_esg.split_esg_sections(text)
                community = [item for item in sections if item.section_code == "community"]
                self.assertEqual(len(community), 1)
                self.assertEqual(community[0].title, first_title)
                self.assertEqual(
                    [span.title for span in community[0].subsection_spans],
                    [first_title, second_title],
                )
                self.assertEqual(
                    text[community[0].source_start_char:community[0].source_end_char],
                    community[0].text,
                )

    def test_prose_and_repeated_chrome_guards_stay_separate(self):
        self.assertIsNone(
            section_splitter_esg.map_heading_to_code(
                "Energy efficiency proceeds were allocated to Eligible Projects during the Reporting Period"
            )
        )
        pages = [
            "ABOUT US EMPOWERING PEOPLE SUSTAINING PLACES RESPONSIBLE PRACTICES APPENDICES\n"
            "Short page furniture only\n"
            for _ in range(3)
        ]
        text, page_spans = paged_text(pages)
        titles = [item.title for item in section_splitter_esg.collect_heading_candidates(text, page_spans)]
        self.assertFalse(any("RESPONSIBLE PRACTICES APPENDICES" in title for title in titles))

    def test_known_noise_never_becomes_subsection_metadata(self):
        noise = [
            "Energy efficiency proceeds were allocated to Eligible Projects during the Reporting Period",
            "FY24 DECKERS FOOTWEAR ENERGY USAGE BY MATERIAL CATEGORY GATE BREAKDOWN",
        ]
        for line in noise:
            with self.subTest(line=line):
                text = f"{line}\n100 200 300\n400 500 600\n"
                sections = section_splitter_esg.split_esg_sections(text)
                self.assertFalse(
                    any(
                        span.title == line
                        for section in sections
                        for span in section.subsection_spans
                    )
                )

        pages = [
            f"{page}\nAPPENDIX ABOUT US GOVERNANCE CONCLUSION\nShort page furniture\n"
            for page in range(1, 4)
        ]
        text, page_spans = paged_text(pages)
        sections = section_splitter_esg.split_esg_sections(text, page_spans)
        self.assertFalse(any(section.subsection_spans for section in sections))


if __name__ == "__main__":
    unittest.main()
