"""Page-role classification: is a page navigation, or is it content?

The shapes below are reduced from development-split pages of the AI gold set.
Holdout pages are deliberately absent -- nothing here may be tuned against them.
"""

from __future__ import annotations

import unittest

from esg_page_role import (
    AUTO_EXCLUDE_NAVIGATION,
    NAVIGATION_CONTENTS,
    NAVIGATION_LINK_HUB,
    NAVIGATION_STANDARDS_INDEX,
    apply_navigation_override,
    classify_page_role,
)


# gold_041_NGVC_p5: masthead split across two lines, page number first.
NGVC_CONTENTS = """Table of
Contents
  6 TIMELINE
  8 FIVE FOUNDING PRINCIPLES
  9 STATEMENT ON SUSTAINABILITY
  10 PRODUCT STANDARDS
  26 OPERATIONS
  28 PEOPLE
  34 CORPORATE OVERSIGHT
  36 SASB INDEX
FISCAL YEAR  |  2024 5
"""

# gold_043_CASY_p2: column repair pushes the "TABLE OF" / "CONTENTS" halves
# apart and interleaves entries from several columns.
CASY_CONTENTS = """Introduction Environmental Impact Social Impact  Our Communities

Introduction  Social Impact
TABLE OF
04   Message from our Board Chair,   21     Our Team
    President and CEO
CONTENTS
23    Casey's CARES
41     Corporate Governance and Ethics
05   About Casey's
26    Providing New Opportunities and
43    Risk Management
07    About This Report      Career Growth
43    Data Security and Guest Privacy
07    Our Sustainability Approach
46    Supply Chain Management
10    2025 Highlights
"""

# gold_054_UPBD_p2: a contents list with a real prose paragraph interleaved.
UPBD_CONTENTS = """Contents
Introduction 2
Letter from the CEO 3-4
Upbound at a Glance 5-9 Introduction
Our Approach to Sustainability 10-14
At Upbound Group, we are committed to sustainability responsibilities
Priority Topics 11
in  ways  that  align  with  our  overall  business  strategy.  Our  2023
2023 Accomplishments 13 Sustainability  Report  highlights  our  ongoing  efforts  to  develop  and
2024 Commitments 14 implement our sustainability strategy, while also providing a data-driven
Environmental Impact 15-19
Greenhouse Gas Inventory 16-17
Fuel & Climate Initiatives 18-19
"""

# recovery_review_v2_001_VSXY_p5: a wide running navigation row splits the
# TABLE OF / CONTENTS title across two different extracted lines.
VSXY_SECTION_INDEX = """TABLE OF  A MESSAGE FROM  ABOUT THIS  INDEX
2023 ESG REPORT
CONTENTS OUR CEO REPORT
About VS&Co
6  Our Business at a Glance
7  Our Values & Brand Purpose
8  Our Material ESG Issues
9  Year at a Glance
10  Our Stakeholders
5
"""

MELI_LINK_HUB = """ABOUT THIS REPORT BUSINESS EXPERIENCE TEAM SOCIAL ENVIRONMENT
Content
GO TO GO TO GO TO GO TO
About this report Business User experience Our team
page 4 page 8 page 23 page 32
GO TO GO TO GO TO
Social impact Environment GRI, SASB & IR
page 44 page 55 page 72
2023 Impact Report 2
"""

# gold_008_SNBR_p64: a standards cross-reference index. No page numbers at all,
# so the contents rule cannot see it.
SNBR_STANDARDS_INDEX = """GRI Context Index

| GRI/SASB STANDARD | INDICATOR/METRIC | DISCLOSURE |
| --- | --- | --- |
| ORGANIZATION AND REPORTING PRACTICES |  |  |
| STAKEHOLDER ENGAGEMENT |  |  |
| GRI 2: General Disclosures 2021 | GRI 2.29 Approach to Stakeholder Engagement | Corporate Sustainability Report, Governance - Stakeholder Engagement |
|  | GRI 2.30a b Collective bargaining agreements | Corporate Sustainability Report, Appendix, Human Rights Policy |
| MATERIALITY |  |  |
| GRI 3: Material Topics 2021 | GRI 2021 3.1 Process to determine Material Topics | Corporate Sustainability Report, Governance |
|  | GRI 2021 3.2 List of Material Topics | Corporate Sustainability Report, Governance |
"""

# gold_042_LEG_p11: ordinary prose. The reading-order module labels this page
# navigation_contents_layout, which is exactly the confusion this module exists
# to undo -- it must stay content.
LEG_CONTENT = """2024 LEGGETT & PLATT SUSTAINABILITY REPORT OUR PEOPLE 9
Engagement and Satisfaction
We want employees to have positive work experiences at Leggett & Platt. Our HR team leads efforts to improve
employee engagement. Employee satisfaction, feedback, and turnover data are analyzed via targeted employee
surveys, employee focus groups, and turnover analyses to identify improvement opportunities. We develop
action plans based on specific needs and implement them in collaboration with local management.
In 2023, initiatives to further drive engagement and satisfaction included employee opportunities for various
touchpoints with leadership. For example, our senior leaders held on-site roundtable discussions at several
L&P facilities.
"""

# gold_032_WWW_p64: a data table whose rows end in small integers, so entry-line
# counting alone scores it high. It carries no contents title and must stay in.
WWW_DATA_TABLE = """Environmental Performance Data

| Metric | 2022 | 2023 |
| --- | --- | --- |
| Scope 1 emissions | 12 | 11 |
| Scope 2 emissions | 44 | 39 |
Total energy consumed 88
Renewable electricity share 24
Water withdrawn 61
Waste diverted 73
GRI 302-1 energy within the organization 12
SASB CG-AA-130a.1 15
"""


class ContentsPageTests(unittest.TestCase):
    def test_split_masthead_contents_page_is_navigation(self):
        result = classify_page_role(NGVC_CONTENTS)
        self.assertTrue(result.is_navigation)
        self.assertEqual(result.reason, NAVIGATION_CONTENTS)

    def test_contents_page_with_halves_pushed_apart_is_navigation(self):
        result = classify_page_role(CASY_CONTENTS)
        self.assertTrue(result.is_navigation)
        self.assertEqual(result.reason, NAVIGATION_CONTENTS)

    def test_contents_page_survives_interleaved_prose(self):
        result = classify_page_role(UPBD_CONTENTS)
        self.assertTrue(result.is_navigation)
        self.assertEqual(result.reason, NAVIGATION_CONTENTS)

    def test_split_title_with_running_navigation_is_navigation(self):
        result = classify_page_role(VSXY_SECTION_INDEX)
        self.assertTrue(result.is_navigation)
        self.assertEqual(result.reason, NAVIGATION_CONTENTS)

    def test_link_hub_with_page_controls_is_navigation(self):
        result = classify_page_role(MELI_LINK_HUB)
        self.assertTrue(result.is_navigation)
        self.assertEqual(result.reason, NAVIGATION_LINK_HUB)


class StandardsIndexTests(unittest.TestCase):
    def test_standards_cross_reference_index_is_navigation(self):
        result = classify_page_role(SNBR_STANDARDS_INDEX)
        self.assertTrue(result.is_navigation)
        self.assertEqual(result.reason, NAVIGATION_STANDARDS_INDEX)

    def test_contents_rule_alone_would_miss_the_standards_index(self):
        """Pins why the second rule exists: this page has no entry lines."""
        body = [
            line
            for line in SNBR_STANDARDS_INDEX.splitlines()
            if line.strip() and not line.strip().startswith("|")
        ]
        self.assertEqual(len(body), 1)


class ContentPageTests(unittest.TestCase):
    def test_prose_page_labelled_navigation_by_layout_stays_content(self):
        result = classify_page_role(LEG_CONTENT)
        self.assertFalse(result.is_navigation)
        self.assertEqual(result.reason, "")

    def test_data_table_with_trailing_integers_stays_content(self):
        result = classify_page_role(WWW_DATA_TABLE)
        self.assertFalse(result.is_navigation)

    def test_empty_page_is_not_navigation(self):
        result = classify_page_role("")
        self.assertFalse(result.is_navigation)
        self.assertEqual(result.detail, "empty_page_text")

    def test_bare_contents_title_without_entries_stays_content(self):
        """A section opener titled "Contents" is not by itself a contents page."""
        result = classify_page_role(
            "Contents\nThis section describes how we manage water across our sites.\n"
        )
        self.assertFalse(result.is_navigation)


class NavigationOverrideTests(unittest.TestCase):
    def test_override_beats_every_auto_pass_path(self):
        for certified in (
            "auto_pass",
            "auto_pass_verified_table_extraction",
            "auto_pass_navigation_contents",
            "auto_pass_column_order_reconstructed",
            "auto_pass_region_order_reconstructed",
            "auto_pass_pdfium_coverage",
        ):
            with self.subTest(decision=certified):
                decision, reason, _ = apply_navigation_override(
                    certified, "original", NGVC_CONTENTS
                )
                self.assertEqual(decision, AUTO_EXCLUDE_NAVIGATION)
                self.assertIn(NAVIGATION_CONTENTS, reason)

    def test_override_also_beats_an_existing_hold(self):
        """A held navigation page must be excluded, not left held.

        ``scripts/run_esg_vlm.py`` selects ``auto_hold`` pages for vision
        re-extraction, so a hold is a queue, not a terminal state. Leaving a
        navigation page held would send it to a VLM and let it back in.
        """
        decision, reason, _ = apply_navigation_override(
            "auto_hold", "auto_hold_ambiguous_coordinate_layout", NGVC_CONTENTS
        )
        self.assertEqual(decision, AUTO_EXCLUDE_NAVIGATION)
        self.assertIn(NAVIGATION_CONTENTS, reason)

    def test_content_page_keeps_its_hold(self):
        decision, reason, _ = apply_navigation_override(
            "auto_hold", "auto_hold_ambiguous_coordinate_layout", LEG_CONTENT
        )
        self.assertEqual(decision, "auto_hold")
        self.assertEqual(reason, "auto_hold_ambiguous_coordinate_layout")

    def test_content_page_decision_is_untouched(self):
        decision, reason, _ = apply_navigation_override(
            "auto_pass_navigation_contents",
            "auto_pass_navigation_contents_layout: no_prose_order_required",
            LEG_CONTENT,
        )
        self.assertEqual(decision, "auto_pass_navigation_contents")
        self.assertEqual(
            reason, "auto_pass_navigation_contents_layout: no_prose_order_required"
        )


if __name__ == "__main__":
    unittest.main()
