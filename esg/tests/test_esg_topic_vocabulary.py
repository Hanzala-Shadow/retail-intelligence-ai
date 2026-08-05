"""Regression tests for heading -> section_code mapping.

Each case here comes from a labelled read of real chunks, not from intuition.
The vocabulary is order-sensitive -- HEADING_PATTERNS is first-match-wins -- so
a token added to the wrong code is not merely imprecise, it silently outranks
the code that should have had the heading.
"""

from __future__ import annotations

import unittest

import section_splitter_esg as splitter


class SocialComplianceTests(unittest.TestCase):
    """'Social compliance' is supplier auditing, not a social-topic heading.

    A 300-chunk labelled read scored `social` at 4% precision: 27 of its 47
    usable sections were really supply chain, and these headings were most of
    them. The phrase reads like the `social` code but names the factory audit
    programme that apparel and retail companies run over their suppliers.
    """

    SUPPLY_CHAIN = [
        "SOCIAL COMPLIANCE",
        "Social Compliance Program",
        "Warby Parker Social Compliance Program",
        "ONLY-AT-KOHLS BRANDS SOCIAL COMPLIANCE PROGRAM PERFORMANCE",
        "Social Compliance Audit Summary",
        "Social Compliance Training",
        "Social Compliance Risk Segmentation",
    ]

    def test_social_compliance_headings_are_supply_chain(self):
        for title in self.SUPPLY_CHAIN:
            with self.subTest(title=title):
                self.assertEqual(
                    splitter.map_heading_to_code(title), "supply_chain_ethics"
                )

    def test_genuine_social_headings_are_untouched(self):
        """The fix must not empty the `social` code of its real members."""
        for title in ("Social", "Society", "Corporate Social Responsibility",
                      "Social Sustainability", "Social Responsibility"):
            with self.subTest(title=title):
                self.assertEqual(splitter.map_heading_to_code(title), "social")

    def test_the_phrase_lives_in_exactly_one_pattern(self):
        """Two codes matching it would make the outcome depend on table order."""
        owners = [
            code for code, pattern in splitter.HEADING_PATTERNS
            if "social\\s+compliance" in pattern
        ]
        self.assertEqual(owners, ["supply_chain_ethics"])


if __name__ == "__main__":
    unittest.main()
