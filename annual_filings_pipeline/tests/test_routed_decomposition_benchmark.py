from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from scripts import run_routed_decomposition_benchmark as runner
from src.query_decomposition import (
    ContractError,
    ProductionRetrieverAdapter,
    aggregate,
)


def question_row(
    question_id: str = "q1",
    group: str = "cross_company",
) -> dict[str, str]:
    return {
        "question_id": question_id,
        "question_group": group,
        "question": "Compare the approved sources.",
        "expected_tickers": "AAA|BBB",
        "expected_years": "2024|2025",
        "required_doc_type": "10-K",
        "required_sections": "Item_7|Item_8",
        "supporting_accession_numbers": "acc-a|acc-b",
        "refusal_expected": "FALSE",
        "expected_answer": "must not be used",
        "supporting_chunk_ids": "999|998",
        "supporting_passages": "gold passage one|gold passage two",
        "supporting_chunk_indexes": "1|2",
        "supporting_source_files": "a.html|b.html",
        "supporting_file_sha256": "hash-a|hash-b",
        "supporting_token_counts": "100|100",
    }


class FakeRetriever:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[object]]] = []

    def retrieve(self, question, sources):
        sources = list(sources)
        self.calls.append((question, sources))
        if len(sources) != 1:
            raise AssertionError("each routed retrieval must use one source")

        source = sources[0]
        call_number = len(self.calls)
        evidence = []
        for rank in range(1, 6):
            evidence.append(
                {
                    "chunk_id": call_number * 100 + rank,
                    "semantic_rank": rank,
                    "cross_encoder_rank_within_source": rank,
                    "cross_encoder_score": 1.0 / rank,
                    "ticker": source.ticker,
                    "filing_year": source.filing_year,
                    "doc_type": source.doc_type,
                    "accession_number": source.accession_number,
                    "section_code": source.section_code,
                    "chunk_text": f"{source.ticker} evidence {rank}",
                }
            )
        return {"evidence": evidence}


class WrongRouteRetriever(FakeRetriever):
    def retrieve(self, question, sources):
        result = super().retrieve(question, sources)
        result["evidence"][0]["ticker"] = "WRONG"
        return result


def write_frozen_shape(path: Path) -> None:
    fields = list(question_row())
    rows = []

    for index in range(1, 25):
        row = question_row(
            question_id=f"q{index:02d}",
            group="Item_1",
        )
        row["expected_tickers"] = "AAA"
        row["expected_years"] = "2024"
        row["required_sections"] = "Item_1"
        row["supporting_accession_numbers"] = "acc-a"
        rows.append(row)

    for index in range(1, 6):
        rows.append(
            question_row(
                question_id=f"refusal-{index}",
                group="refusal",
            )
        )

    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


class RoutedDecompositionTests(unittest.TestCase):
    def test_duplicate_temporal_tickers_remain_positional(self):
        row = question_row("tc")
        row["expected_tickers"] = "EBAY|EBAY"
        row["expected_years"] = "2024|2026"
        row["required_sections"] = "Item_1A|Item_1A"
        row["supporting_accession_numbers"] = "acc-2024|acc-2026"

        routes = runner.build_routed_subqueries(row)

        self.assertEqual(len(routes), 2)
        self.assertEqual(
            [(route.ticker, route.filing_year) for route in routes],
            [("EBAY", 2024), ("EBAY", 2026)],
        )
        self.assertNotEqual(
            routes[0].comparison_side_id,
            routes[1].comparison_side_id,
        )

    def test_xc004_mixed_sections_remain_positional(self):
        row = question_row("10K-V2-XC-004")
        row["expected_tickers"] = "GRWG|ATER"
        row["expected_years"] = "2024|2024"
        row["required_sections"] = "Item_7|Item_8"
        row["supporting_accession_numbers"] = "grwg-acc|ater-acc"

        routes = runner.build_routed_subqueries(row)

        self.assertEqual(
            [
                (
                    route.ticker,
                    route.filing_year,
                    route.section_code,
                    route.accession_number,
                )
                for route in routes
            ],
            [
                ("GRWG", 2024, "Item_7", "grwg-acc"),
                ("ATER", 2024, "Item_8", "ater-acc"),
            ],
        )

    def test_doc_type_broadcasts_and_non_10k_fails_closed(self):
        routes = runner.build_routed_subqueries(question_row())
        self.assertEqual(
            [route.doc_type for route in routes],
            ["10-K", "10-K"],
        )

        row = question_row()
        row["required_doc_type"] = "8-K"
        with self.assertRaisesRegex(ValueError, "only permits"):
            runner.build_routed_subqueries(row)

    def test_gold_fields_cannot_change_routes(self):
        first = question_row()
        second = question_row()

        for field in runner.PROHIBITED_RETRIEVAL_FIELDS:
            second[field] = f"malicious-{field}"

        self.assertEqual(
            runner.build_routed_subqueries(first),
            runner.build_routed_subqueries(second),
        )

    def test_multi_source_calls_are_independent_and_question_unchanged(self):
        row = question_row()
        routes = runner.build_routed_subqueries(row)
        retriever = FakeRetriever()
        adapter = ProductionRetrieverAdapter(
            retriever,
            runner.SourceSpec,
        )

        result = aggregate(
            routes,
            adapter,
            evidence_limit=runner.FINAL_EVIDENCE_COUNT,
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["evidence"]), 5)
        self.assertEqual(len(retriever.calls), 2)
        self.assertEqual(
            [call[0] for call in retriever.calls],
            [row["question"], row["question"]],
        )
        self.assertTrue(
            all(len(call[1]) == 1 for call in retriever.calls)
        )

    def test_strict_adapter_route_failure_propagates(self):
        route = runner.build_routed_subqueries(
            question_row()
        )[0]
        adapter = ProductionRetrieverAdapter(
            WrongRouteRetriever(),
            runner.SourceSpec,
        )

        with self.assertRaises(ContractError) as raised:
            adapter.retrieve(route)

        self.assertEqual(
            raised.exception.code,
            "ROUTING_INTEGRITY_FAILED",
        )

    def test_full_fake_run_enforces_counts_and_is_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            questions = Path(directory) / "questions.csv"
            write_frozen_shape(questions)

            first_rows, first_details = runner.run_benchmark(
                questions,
                FakeRetriever(),
            )
            second_rows, second_details = runner.run_benchmark(
                questions,
                FakeRetriever(),
            )

        self.assertEqual(first_rows, second_rows)
        self.assertEqual(first_details, second_details)
        self.assertEqual(len(first_rows), 120)
        self.assertEqual(
            set(first_rows[0]),
            set(runner.CSV_FIELDS),
        )
        self.assertEqual(
            {row["question_id"] for row in first_rows},
            {f"q{index:02d}" for index in range(1, 25)},
        )

    def test_question_and_refusal_counts_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            questions = Path(directory) / "questions.csv"
            write_frozen_shape(questions)

            rows = list(
                csv.DictReader(
                    questions.open(
                        encoding="utf-8",
                        newline="",
                    )
                )
            )
            fields = list(rows[0])
            rows.pop()

            with questions.open(
                "w",
                encoding="utf-8",
                newline="",
            ) as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=fields,
                )
                writer.writeheader()
                writer.writerows(rows)

            with self.assertRaisesRegex(
                RuntimeError,
                "expected 5 refusal questions",
            ):
                runner.run_benchmark(
                    questions,
                    FakeRetriever(),
                    expected_supported=24,
                    expected_refusals=5,
                )

    def test_overwrite_protection_checks_all_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            existing = Path(directory) / "details.json"
            existing.write_text("protected\n", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                runner._refuse_overwrite(
                    (
                        Path(directory) / "retrieval.csv",
                        existing,
                        Path(directory) / "manifest.json",
                    )
                )


if __name__ == "__main__":
    unittest.main()
