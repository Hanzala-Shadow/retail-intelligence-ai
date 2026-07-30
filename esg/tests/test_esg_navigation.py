from __future__ import annotations

import unittest

import pdfplumber

import config
from esg_navigation import build_navigation_profile, character_angle, clean_navigation


CASES = {
    "WSM": ("WSM", "WSM-WILLIAMS-SONOMA INC-2023.pdf", 31),
    "BURL": ("BURL", "BURL-BURLINGTON STORES INC-2023.pdf", 30),
    "GES": ("GES", "GES-GUESS-2024.pdf", 9),
    "BBY": ("BBY", "BBY-BEST BUY CO INC-2024.pdf", 5),
    "AMZN62": ("AMZN", "AMZN-AMAZON.COM INC-2023.pdf", 62),
    "AMZN63": ("AMZN", "AMZN-AMAZON.COM INC-2023.pdf", 63),
    "AMZN64": ("AMZN", "AMZN-AMAZON.COM INC-2023.pdf", 64),
}


def words(page):
    return page.extract_words(
        use_text_flow=False, keep_blank_chars=False, extra_attrs=["size", "upright"]
    ) or []


class NavigationCleanerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = {}
        profiles = {}
        raw_root = config.RAW_SUSTAINABILITY_DIR
        for name, (ticker, filename, page_number) in CASES.items():
            with pdfplumber.open(raw_root / ticker / filename) as pdf:
                if filename not in profiles:
                    profiles[filename] = build_navigation_profile(
                        [(page.chars, float(page.width), float(page.height)) for page in pdf.pages]
                    )
                profile = profiles[filename]
                page = pdf.pages[page_number - 1]
                source = words(page)
                result = clean_navigation(
                    source, page.chars, float(page.width), float(page.height), profile
                )
                cls.results[name] = (source, result)

    def test_wsm_sidebar_letters_are_removed_from_body(self):
        _, result = self.results["WSM"]
        nav_text = " ".join(item["text"] for item in result.navigation_items)
        body_text = " ".join(word["text"] for word in result.body_words)
        self.assertIn("INTRO", nav_text)
        self.assertIn("PLANET", nav_text)
        self.assertNotIn(" I N T R O ", f" {body_text} ")

    def test_burl_rotated_navigation_is_removed_from_body(self):
        _, result = self.results["BURL"]
        nav_text = " ".join(item["text"] for item in result.navigation_items)
        nav_indices = {
            i for item in result.navigation_items if item["item_type"] == "vertical"
            for i in item["source_indices"]
        }
        self.assertIn("OVERVIEW", nav_text)
        self.assertGreater(len(nav_indices), 70)
        self.assertTrue(all(not word.get("upright", True) for i, word in enumerate(self.results["BURL"][0]) if i in nav_indices))

    def test_ges_horizontal_nonupright_text_is_kept(self):
        source, result = self.results["GES"]
        self.assertGreater(sum(not w.get("upright", True) for w in source), 900)
        source_nonupright = [word for word in source if not word.get("upright", True)]
        body_ids = {id(word) for word in result.body_words}
        self.assertTrue(all(id(word) in body_ids for word in source_nonupright))
        self.assertTrue(all(item["item_type"] in {"header", "footer"} for item in result.navigation_items))
        self.assertEqual(result.rotated_content_items, [])

    def test_bby_materiality_axis_is_preserved_as_content(self):
        _, result = self.results["BBY"]
        rotated_text = " ".join(item["text"] for item in result.rotated_content_items)
        self.assertIn("Importance to Stakeholders", rotated_text)
        self.assertTrue(all(item["item_type"] in {"header", "footer"} for item in result.navigation_items))

    def test_slight_angle_and_horizontal_shear_stay_in_body(self):
        words = [
            {"text": "angled", "x0": 10, "x1": 60, "top": 100, "bottom": 110},
            {"text": "sheared", "x0": 70, "x1": 130, "top": 100, "bottom": 110},
        ]
        chars = [
            {"text": "angled", "x0": 10, "x1": 60, "top": 100, "bottom": 110,
             "matrix": (1.0, 0.10, -0.10, 1.0, 0, 0)},
            {"text": "sheared", "x0": 70, "x1": 130, "top": 100, "bottom": 110,
             "matrix": (1.0, 0.0, 0.35, 1.0, 0, 0)},
        ]
        result = clean_navigation(words, chars, 600, 800, ())
        self.assertAlmostEqual(character_angle(chars[0]), 5.7106, places=3)
        self.assertEqual(character_angle(chars[1]), 0.0)
        self.assertEqual(result.body_words, words)
        self.assertEqual(result.rotated_content_items, [])

    def test_amzn_repeated_horizontal_header_is_navigation(self):
        source, result = self.results["AMZN63"]
        nav_text = " | ".join(item["text"] for item in result.navigation_items)
        body_text = " ".join(word["text"] for word in result.body_words)
        self.assertIn("2023 Amazon Sustainability Report", nav_text)
        self.assertIn("Overview", nav_text)
        self.assertNotIn("2023 Amazon Sustainability Report", body_text)
        self.assertLess(len(result.body_words), len(source))

    def test_amzn_nearby_pages_share_horizontal_navigation(self):
        for name in ("AMZN62", "AMZN63", "AMZN64"):
            _, result = self.results[name]
            nav_text = " | ".join(item["text"] for item in result.navigation_items)
            self.assertIn("2023 Amazon Sustainability Report", nav_text)
            self.assertIn("Overview", nav_text)
            self.assertTrue(all(item["item_type"] == "header" for item in result.navigation_items))

    def test_each_source_item_is_accounted_for_once(self):
        for source, result in self.results.values():
            body_ids = {id(word) for word in result.body_words}
            nav = [i for item in result.navigation_items for i in item["source_indices"]]
            rotated = [i for item in result.rotated_content_items for i in item["source_indices"]]
            accounted = len(body_ids) + len(nav) + len(rotated)
            self.assertEqual(accounted, len(source))
            self.assertEqual(len(nav), len(set(nav)))
            self.assertEqual(len(rotated), len(set(rotated)))
            self.assertTrue(set(nav).isdisjoint(rotated))


if __name__ == "__main__":
    unittest.main()
