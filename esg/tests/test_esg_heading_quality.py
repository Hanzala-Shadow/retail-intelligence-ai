import sys
import unittest
from pathlib import Path


import section_splitter_esg


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


if __name__ == "__main__":
    unittest.main()
