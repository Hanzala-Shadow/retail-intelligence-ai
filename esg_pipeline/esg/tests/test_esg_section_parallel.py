import csv
import tempfile
import unittest
from pathlib import Path

import section_splitter_esg


class ParallelSectioningTests(unittest.TestCase):
    def test_two_workers_write_independent_documents_and_one_index(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_root = root / "input"
            for ticker, stem, body in (
                ("AAA", "AAA-Report-2024", "Climate\nWe reduced emissions with measured targets."),
                ("BBB", "BBB-Report-2024", "Water\nWe reduced water use with measured targets."),
            ):
                directory = input_root / ticker
                directory.mkdir(parents=True, exist_ok=True)
                (directory / f"{stem}.txt").write_text(body, encoding="utf-8")

            output_root = root / "sections"
            index = root / "sections.csv"
            rows = section_splitter_esg.run(
                input_root, output_root, index, workers=2
            )

            self.assertTrue(rows)
            self.assertTrue((output_root / "AAA").is_dir())
            self.assertTrue((output_root / "BBB").is_dir())
            with index.open(newline="", encoding="utf-8") as handle:
                indexed = list(csv.DictReader(handle))
            self.assertEqual({row["ticker"] for row in indexed}, {"AAA", "BBB"})


if __name__ == "__main__":
    unittest.main()
