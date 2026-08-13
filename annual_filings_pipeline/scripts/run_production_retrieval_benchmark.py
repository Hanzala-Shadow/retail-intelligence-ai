#!/usr/bin/env python3
"""Run the frozen supported questions through the production query API."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.query_api import (  # noqa: E402
    BI_ENCODER_REPO,
    BI_ENCODER_REVISION,
    CANDIDATES_PER_SOURCE,
    CROSS_ENCODER_REPO,
    CROSS_ENCODER_REVISION,
    FINAL_EVIDENCE_COUNT,
    ProductionRetriever,
    SourceSpec,
)

DEFAULT_QUESTIONS = Path("data/00_reference/rag_eval_questions.csv")
MODEL_ID = "bge_base_en_v1_5"
DEFAULT_EXPECTED_SUPPORTED = 24
DEFAULT_EXPECTED_REFUSALS = 5
ROUTING_FIELDS = (
    "expected_tickers",
    "expected_years",
    "required_doc_type",
    "supporting_accession_numbers",
    "required_sections",
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _parts(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split("|") if item.strip()]


def _broadcast(values: list[str], count: int, field: str, question_id: str) -> list[str]:
    if len(values) == 1 and count > 1:
        values = values * count
    if len(values) != count:
        raise ValueError(f"{question_id}: positional {field} mismatch")
    return values


def build_sources(row: dict[str, str]) -> list[SourceSpec]:
    question_id = row["question_id"]
    tickers = _parts(row["expected_tickers"])
    if not tickers:
        raise ValueError(f"{question_id}: expected_tickers is empty")
    count = len(tickers)
    years = _broadcast(_parts(row["expected_years"]), count, "expected_years", question_id)
    doc_types = _broadcast(
        _parts(row["required_doc_type"]), count, "required_doc_type", question_id
    )
    accessions = _broadcast(
        _parts(row["supporting_accession_numbers"]),
        count,
        "supporting_accession_numbers",
        question_id,
    )
    sections = _broadcast(
        _parts(row["required_sections"]), count, "required_sections", question_id
    )
    return [
        SourceSpec(
            ticker=ticker,
            filing_year=int(year),
            doc_type=doc_type,
            accession_number=accession,
            section_code=section,
        )
        for ticker, year, doc_type, accession, section in zip(
            tickers, years, doc_types, accessions, sections, strict=True
        )
    ]


def load_question_rows(
    questions_path: Path,
    *,
    expected_supported: int | None = None,
    expected_refusals: int | None = None,
) -> tuple[list[dict[str, str]], int]:
    with questions_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    supported = [row for row in rows if row["question_group"] != "refusal"]
    refusals = [row for row in rows if row["question_group"] == "refusal"]
    if not supported:
        raise RuntimeError("question set contains no supported questions")
    if expected_supported is not None and len(supported) != expected_supported:
        raise RuntimeError(
            f"expected {expected_supported} supported questions, found {len(supported)}"
        )
    if expected_refusals is not None and len(refusals) != expected_refusals:
        raise RuntimeError(
            f"expected {expected_refusals} refusal questions, found {len(refusals)}"
        )
    return supported, len(refusals)


def run_benchmark(
    questions_path: Path,
    retriever: ProductionRetriever,
    *,
    expected_supported: int | None = None,
    expected_refusals: int | None = None,
) -> list[dict[str, Any]]:
    supported, _ = load_question_rows(
        questions_path,
        expected_supported=expected_supported,
        expected_refusals=expected_refusals,
    )

    output: list[dict[str, Any]] = []
    for index, row in enumerate(supported, 1):
        sources = build_sources(row)
        result = retriever.retrieve(row["question"], sources)
        evidence = result["evidence"]
        if len(evidence) != FINAL_EVIDENCE_COUNT:
            raise RuntimeError(
                f"{row['question_id']}: expected {FINAL_EVIDENCE_COUNT} results"
            )
        for evidence_row in evidence:
            output.append(
                {
                    "model_id": MODEL_ID,
                    "question_id": row["question_id"],
                    "rank": int(evidence_row["final_rank"]),
                    "chunk_id": int(evidence_row["chunk_id"]),
                    "score": float(evidence_row["cross_encoder_score"]),
                }
            )
        print(
            f"[{index}/{len(supported)}] {row['question_id']} "
            f"candidates={result['candidate_counts_by_source']}",
            file=sys.stderr,
            flush=True,
        )

    expected_rows = len(supported) * FINAL_EVIDENCE_COUNT
    if len(output) != expected_rows:
        raise RuntimeError(f"expected {expected_rows} retrieval rows, found {len(output)}")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument(
        "--expected-supported", type=int,
        help="optional fail-closed supported-question count; omitted means auto-detect",
    )
    parser.add_argument(
        "--expected-refusals", type=int,
        help="optional fail-closed refusal count; omitted means auto-detect",
    )
    args = parser.parse_args()
    manifest_path = args.manifest or args.output.with_suffix(".manifest.json")
    for path in (args.output, manifest_path):
        if path.exists():
            raise FileExistsError(f"refusing to overwrite: {path}")

    started_at = _utcnow()
    started = time.monotonic()
    with ProductionRetriever() as retriever:
        rows = run_benchmark(
            args.questions,
            retriever,
            expected_supported=args.expected_supported,
            expected_refusals=args.expected_refusals,
        )

    supported_count = len({row["question_id"] for row in rows})
    _, refusal_count = load_question_rows(
        args.questions,
        expected_supported=args.expected_supported,
        expected_refusals=args.expected_refusals,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=("model_id", "question_id", "rank", "chunk_id", "score"),
        )
        writer.writeheader()
        writer.writerows(rows)

    manifest = {
        "started_at": started_at,
        "finished_at": _utcnow(),
        "elapsed_seconds": time.monotonic() - started,
        "questions_path": str(args.questions),
        "questions_sha256": _sha256(args.questions),
        "query_api_sha256": _sha256(REPO_ROOT / "src/query_api.py"),
        "output_path": str(args.output),
        "output_sha256": _sha256(args.output),
        "supported_questions": supported_count,
        "retrieval_rows": len(rows),
        "refusals_excluded": refusal_count,
        "routing_fields_used": list(ROUTING_FIELDS),
        "gold_chunk_ids_used_for_retrieval": False,
        "policy": {
            "bi_encoder": BI_ENCODER_REPO,
            "bi_encoder_revision": BI_ENCODER_REVISION,
            "candidates_per_source": CANDIDATES_PER_SOURCE,
            "cross_encoder": CROSS_ENCODER_REPO,
            "cross_encoder_revision": CROSS_ENCODER_REVISION,
            "final_evidence_count": FINAL_EVIDENCE_COUNT,
            "section_routing": "required_hard_filter",
            "multi_source_merge": "deterministic_round_robin",
        },
    }
    with manifest_path.open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
