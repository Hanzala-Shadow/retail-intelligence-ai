import csv
import tempfile
import unittest
from pathlib import Path

from scripts import run_production_retrieval_benchmark as benchmark


class FakeRetriever:
    def retrieve(self, question, sources):
        return {
            "candidate_counts_by_source": [20 for _ in sources],
            "evidence": [
                {
                    "final_rank": rank,
                    "chunk_id": 1000 + rank,
                    "cross_encoder_score": 1.0 / rank,
                }
                for rank in range(1, 6)
            ],
        }


def question_row(question_id="q1", group="Item_1"):
    return {
        "question_id": question_id,
        "question_group": group,
        "question": "question text",
        "expected_tickers": "AAA|BBB",
        "expected_years": "2024|2025",
        "required_doc_type": "10-K",
        "required_sections": "Item_1|Item_7",
        "supporting_accession_numbers": "acc-a|acc-b",
        "supporting_chunk_ids": "999|998",
    }


class BenchmarkAdapterTests(unittest.TestCase):
    def test_build_sources_uses_routing_fields_positionally(self):
        sources = benchmark.build_sources(question_row())
        self.assertEqual(len(sources), 2)
        self.assertEqual(sources[0].ticker, "AAA")
        self.assertEqual(sources[0].filing_year, 2024)
        self.assertEqual(sources[0].accession_number, "acc-a")
        self.assertEqual(sources[0].section_code, "Item_1")
        self.assertEqual(sources[1].ticker, "BBB")
        self.assertEqual(sources[1].section_code, "Item_7")

    def test_build_sources_does_not_depend_on_gold_chunk_ids(self):
        first = question_row()
        second = question_row()
        second["supporting_chunk_ids"] = "malicious|values"
        self.assertEqual(benchmark.build_sources(first), benchmark.build_sources(second))

    def test_positional_mismatch_fails_closed(self):
        row = question_row()
        row["required_sections"] = "Item_1|Item_7|Item_8"
        with self.assertRaises(ValueError):
            benchmark.build_sources(row)

    def test_run_writes_only_supported_questions(self):
        fields = list(question_row())
        rows = [question_row(f"q{index}") for index in range(1, 25)]
        rows.append(question_row("refuse", "refusal"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "questions.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            result = benchmark.run_benchmark(path, FakeRetriever())
        self.assertEqual(len(result), 120)
        self.assertNotIn("refuse", {row["question_id"] for row in result})
        self.assertEqual(
            [row["rank"] for row in result[:5]],
            [1, 2, 3, 4, 5],
        )

    def test_run_auto_detects_fifty_supported_questions(self):
        fields = list(question_row())
        rows = [question_row(f"q{index}") for index in range(1, 51)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "questions.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            result = benchmark.run_benchmark(path, FakeRetriever())
        self.assertEqual(len(result), 250)
        self.assertEqual(len({row["question_id"] for row in result}), 50)

    def test_explicit_expected_count_fails_closed(self):
        fields = list(question_row())
        rows = [question_row(f"q{index}") for index in range(1, 4)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "questions.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            with self.assertRaisesRegex(RuntimeError, "expected 50 supported"):
                benchmark.run_benchmark(
                    path,
                    FakeRetriever(),
                    expected_supported=50,
                    expected_refusals=0,
                )


if __name__ == "__main__":
    unittest.main()
