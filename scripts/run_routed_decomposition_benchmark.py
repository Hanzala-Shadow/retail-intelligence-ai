#!/usr/bin/env python3
"""Run approved-metadata routed decomposition on the frozen supported questions."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import resource
import sys
import time
from dataclasses import asdict
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
    EMBEDDING_TABLE,
    FINAL_EVIDENCE_COUNT,
    LIVE_VIEW,
    ProductionRetriever,
    SourceSpec,
)
from src.query_decomposition import (  # noqa: E402
    CONTRACT_VERSION,
    ContractError,
    ProductionRetrieverAdapter,
    SubQuery,
    aggregate,
)

DEFAULT_QUESTIONS = Path("data/00_reference/rag_eval_questions.csv")
MODEL_ID = "bge_base_en_v1_5_routed_decomposition"
SUPPORTED_QUESTION_COUNT = 24
REFUSAL_QUESTION_COUNT = 5
EXPERIMENT_MODE = "approved_metadata_routed_decomposition"

ROUTING_FIELDS = (
    "question_id",
    "question_group",
    "question",
    "expected_tickers",
    "expected_years",
    "required_doc_type",
    "required_sections",
    "supporting_accession_numbers",
    "refusal_expected",
)

PROHIBITED_RETRIEVAL_FIELDS = (
    "expected_answer",
    "supporting_chunk_ids",
    "supporting_passages",
    "supporting_chunk_indexes",
    "supporting_source_files",
    "supporting_file_sha256",
    "supporting_token_counts",
)

CSV_FIELDS = ("model_id", "question_id", "rank", "chunk_id", "score")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _parts(value: str) -> list[str]:
    return [part.strip() for part in str(value or "").split("|") if part.strip()]


def _broadcast(
    values: list[str],
    count: int,
    field: str,
    question_id: str,
) -> list[str]:
    if len(values) == 1 and count > 1:
        values = values * count
    if len(values) != count:
        raise ValueError(f"{question_id}: positional {field} mismatch")
    return values


def build_routed_subqueries(row: dict[str, str]) -> tuple[SubQuery, ...]:
    """Build positional routes using approved metadata only.

    The original question is deliberately retained for every route. This
    experiment isolates independent hard routing and balanced aggregation;
    it does not introduce an additional query-rewriting policy.
    """
    question_id = str(row.get("question_id", "")).strip()
    question = str(row.get("question", "")).strip()
    if not question_id:
        raise ValueError("question_id is empty")
    if not question:
        raise ValueError(f"{question_id}: question is empty")

    tickers = _parts(row.get("expected_tickers", ""))
    if not tickers:
        raise ValueError(f"{question_id}: expected_tickers is empty")

    count = len(tickers)
    years = _broadcast(
        _parts(row.get("expected_years", "")),
        count,
        "expected_years",
        question_id,
    )
    doc_types = _broadcast(
        _parts(row.get("required_doc_type", "")),
        count,
        "required_doc_type",
        question_id,
    )
    sections = _broadcast(
        _parts(row.get("required_sections", "")),
        count,
        "required_sections",
        question_id,
    )
    accessions = _broadcast(
        _parts(row.get("supporting_accession_numbers", "")),
        count,
        "supporting_accession_numbers",
        question_id,
    )

    output: list[SubQuery] = []
    for index, values in enumerate(
        zip(tickers, years, doc_types, accessions, sections, strict=True),
        start=1,
    ):
        ticker, year, doc_type, accession, section = values
        source = SourceSpec.from_mapping(
            {
                "ticker": ticker,
                "filing_year": int(year),
                "accession_number": accession,
                "section_code": section,
                "doc_type": doc_type,
            }
        )
        route_id = f"{question_id}-route-{index:02d}"
        output.append(
            SubQuery(
                subquery_id=f"{question_id}-sq-{index:02d}",
                question=question,
                claim_key=route_id,
                comparison_side_id=route_id,
                ticker=source.ticker,
                filing_year=source.filing_year,
                doc_type=source.doc_type,
                accession_number=source.accession_number,
                section_code=source.section_code,
            )
        )
    return tuple(output)


def _evidence_details(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "aggregated_rank": int(item["aggregated_rank"]),
        "chunk_id": int(item["chunk_id"]),
        "semantic_rank": int(item["semantic_rank"]),
        "cross_encoder_rank": int(item["cross_encoder_rank"]),
        "cross_encoder_score": float(item["cross_encoder_score"]),
        "subquery_id": str(item["subquery_id"]),
        "claim_key": str(item["claim_key"]),
        "comparison_side_id": str(item["comparison_side_id"]),
        "ticker": str(item["ticker"]),
        "filing_year": int(item["filing_year"]),
        "doc_type": str(item["doc_type"]),
        "accession_number": str(item["accession_number"]),
        "section_code": str(item["section_code"]),
        "provenance": item["provenance"],
    }


def _single_source_result(
    subquery: SubQuery,
    adapter: ProductionRetrieverAdapter,
) -> dict[str, Any]:
    chunks = adapter.retrieve(subquery)
    if len(chunks) != FINAL_EVIDENCE_COUNT:
        raise ContractError(
            "INSUFFICIENT_EVIDENCE",
            f"{subquery.subquery_id}: expected {FINAL_EVIDENCE_COUNT} chunks; "
            f"found {len(chunks)}",
        )

    evidence: list[dict[str, Any]] = []
    for rank, chunk in enumerate(chunks, start=1):
        item = asdict(chunk)
        item.update(
            {
                "aggregated_rank": rank,
                "subquery_id": subquery.subquery_id,
                "claim_key": subquery.claim_key,
                "comparison_side_id": subquery.comparison_side_id,
                "provenance": [
                    {
                        "subquery_id": subquery.subquery_id,
                        "comparison_side_id": subquery.comparison_side_id,
                    }
                ],
            }
        )
        evidence.append(item)
    return {
        "status": "success",
        "evidence_completeness": "full",
        "required_sides": [subquery.comparison_side_id],
        "evidence": evidence,
    }


def run_benchmark(
    questions_path: Path,
    retriever: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    with questions_path.open(encoding="utf-8", newline="") as handle:
        question_rows = list(csv.DictReader(handle))

    supported = [
        row for row in question_rows
        if row.get("question_group") != "refusal"
    ]
    refusals = [
        row for row in question_rows
        if row.get("question_group") == "refusal"
    ]
    if len(supported) != SUPPORTED_QUESTION_COUNT:
        raise RuntimeError(
            f"expected {SUPPORTED_QUESTION_COUNT} supported questions; "
            f"found {len(supported)}"
        )
    if len(refusals) != REFUSAL_QUESTION_COUNT:
        raise RuntimeError(
            f"expected {REFUSAL_QUESTION_COUNT} refusal questions; "
            f"found {len(refusals)}"
        )

    adapter = ProductionRetrieverAdapter(retriever, SourceSpec)
    retrieval_rows: list[dict[str, Any]] = []
    question_details: list[dict[str, Any]] = []

    for index, row in enumerate(supported, start=1):
        question_id = row["question_id"]
        subqueries = build_routed_subqueries(row)

        if len(subqueries) == 1:
            result = _single_source_result(subqueries[0], adapter)
            method = "locked_single_source"
        else:
            result = aggregate(
                subqueries,
                adapter,
                evidence_limit=FINAL_EVIDENCE_COUNT,
            )
            method = "independent_routes_balanced_aggregation"

        if result.get("status") != "success":
            raise ContractError(
                str(result.get("error_code", "ROUTED_RETRIEVAL_FAILED")),
                f"{question_id}: {json.dumps(result, sort_keys=True)}",
            )

        evidence = result.get("evidence", [])
        if len(evidence) != FINAL_EVIDENCE_COUNT:
            raise RuntimeError(
                f"{question_id}: expected {FINAL_EVIDENCE_COUNT} final chunks; "
                f"found {len(evidence)}"
            )

        details_evidence = [_evidence_details(item) for item in evidence]
        for rank, item in enumerate(details_evidence, start=1):
            if item["aggregated_rank"] != rank:
                raise RuntimeError(
                    f"{question_id}: non-contiguous aggregated rank"
                )
            retrieval_rows.append(
                {
                    "model_id": MODEL_ID,
                    "question_id": question_id,
                    "rank": rank,
                    "chunk_id": item["chunk_id"],
                    "score": item["cross_encoder_score"],
                }
            )

        question_details.append(
            {
                "question_id": question_id,
                "question_group": row["question_group"],
                "method": method,
                "route_count": len(subqueries),
                "routes": [asdict(subquery) for subquery in subqueries],
                "status": "success",
                "evidence_completeness": result["evidence_completeness"],
                "required_sides": result["required_sides"],
                "evidence": details_evidence,
            }
        )
        print(
            f"[{index}/{len(supported)}] {question_id} "
            f"routes={len(subqueries)} method={method}",
            file=sys.stderr,
            flush=True,
        )

    expected_rows = SUPPORTED_QUESTION_COUNT * FINAL_EVIDENCE_COUNT
    if len(retrieval_rows) != expected_rows:
        raise RuntimeError(
            f"expected {expected_rows} retrieval rows; "
            f"found {len(retrieval_rows)}"
        )

    details = {
        "contract_version": CONTRACT_VERSION,
        "experiment_mode": EXPERIMENT_MODE,
        "in_sample": True,
        "model_id": MODEL_ID,
        "multi_source_subquery_text_policy": "original_question_unchanged",
        "supported_questions": SUPPORTED_QUESTION_COUNT,
        "refusals_excluded": REFUSAL_QUESTION_COUNT,
        "retrieval_rows": len(retrieval_rows),
        "questions": question_details,
    }
    return retrieval_rows, details


def _refuse_overwrite(paths: tuple[Path, ...]) -> None:
    existing = [str(path) for path in paths if path.exists()]
    if existing:
        raise FileExistsError(
            "refusing to overwrite: " + ", ".join(existing)
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--details", type=Path)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()

    details_path = args.details or args.output.with_suffix(".details.json")
    manifest_path = args.manifest or args.output.with_suffix(".manifest.json")
    _refuse_overwrite((args.output, details_path, manifest_path))

    started_at = _utcnow()
    started = time.monotonic()

    with ProductionRetriever() as retriever:
        retrieval_rows, details = run_benchmark(
            args.questions,
            retriever,
        )

    elapsed_seconds = time.monotonic() - started
    peak_rss_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    for path in (args.output, details_path, manifest_path):
        path.parent.mkdir(parents=True, exist_ok=True)

    with args.output.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(retrieval_rows)

    with details_path.open("x", encoding="utf-8") as handle:
        json.dump(details, handle, indent=2, sort_keys=True)
        handle.write("\n")

    manifest = {
        "started_at": started_at,
        "finished_at": _utcnow(),
        "elapsed_seconds": elapsed_seconds,
        "peak_rss_kib": peak_rss_kib,
        "in_sample": True,
        "experiment_mode": EXPERIMENT_MODE,
        "supported_questions": SUPPORTED_QUESTION_COUNT,
        "refusals_excluded": REFUSAL_QUESTION_COUNT,
        "retrieval_rows": len(retrieval_rows),
        "routing_fields_used": list(ROUTING_FIELDS),
        "supporting_accession_numbers_role": "approved_routing_metadata",
        "prohibited_retrieval_fields": list(PROHIBITED_RETRIEVAL_FIELDS),
        "gold_chunk_ids_used_for_retrieval": False,
        "gold_passages_used_for_retrieval": False,
        "expected_answers_used_for_retrieval": False,
        "questions_path": str(args.questions),
        "questions_sha256": _sha256(args.questions),
        "query_api_sha256": _sha256(REPO_ROOT / "src/query_api.py"),
        "query_decomposition_sha256": _sha256(
            REPO_ROOT / "src/query_decomposition.py"
        ),
        "decomposed_query_api_sha256": _sha256(
            REPO_ROOT / "src/decomposed_query_api.py"
        ),
        "runner_sha256": _sha256(Path(__file__)),
        "output_path": str(args.output),
        "output_sha256": _sha256(args.output),
        "details_path": str(details_path),
        "details_sha256": _sha256(details_path),
        "policy": {
            "embedding_table": EMBEDDING_TABLE,
            "live_view": LIVE_VIEW,
            "bi_encoder": BI_ENCODER_REPO,
            "bi_encoder_revision": BI_ENCODER_REVISION,
            "candidates_per_source": CANDIDATES_PER_SOURCE,
            "cross_encoder": CROSS_ENCODER_REPO,
            "cross_encoder_revision": CROSS_ENCODER_REVISION,
            "final_evidence_count": FINAL_EVIDENCE_COUNT,
            "section_routing": "required_hard_filter",
            "single_source_behavior": "locked_original_question",
            "multi_source_retrieval": "one_independent_call_per_route",
            "multi_source_subquery_text": "original_question_unchanged",
            "multi_source_merge": (
                "deterministic_requirement_balanced_round_robin"
            ),
        },
    }

    with manifest_path.open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")

    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
