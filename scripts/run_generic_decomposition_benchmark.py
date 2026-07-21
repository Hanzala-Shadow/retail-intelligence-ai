#!/usr/bin/env python3
"""Benchmark detector-driven generic decomposition without gold routing metadata."""

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

from src.decomposed_query_api import _aliases_from_connection  # noqa: E402
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
    SECTION_ADAPTIVE_POLICY_VERSION,
    SourceSpec,
)
from src.query_decomposition import (  # noqa: E402
    CONTRACT_VERSION,
    ContractError,
    ProductionRetrieverAdapter,
    SourceResolver,
    aggregate,
    build_subqueries,
)

MODEL_ID = "bge_base_en_v1_5_generic_decomposition_v1_1"
EXPERIMENT_MODE = "detector_driven_generic_focused_decomposition"
CSV_FIELDS = ("model_id", "question_id", "rank", "chunk_id", "score")
PROHIBITED_RETRIEVAL_FIELDS = (
    "expected_tickers", "expected_years", "required_doc_type",
    "required_sections", "supporting_accession_numbers", "expected_answer",
    "supporting_chunk_ids", "supporting_passages", "supporting_chunk_indexes",
    "supporting_source_files", "supporting_file_sha256", "supporting_token_counts",
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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


def _single_source_result(subquery: Any, adapter: ProductionRetrieverAdapter) -> dict[str, Any]:
    chunks = adapter.retrieve(subquery)
    if len(chunks) != FINAL_EVIDENCE_COUNT:
        raise ContractError(
            "INSUFFICIENT_EVIDENCE",
            f"{subquery.subquery_id}: expected {FINAL_EVIDENCE_COUNT} chunks; found {len(chunks)}",
        )
    evidence = []
    for rank, chunk in enumerate(chunks, start=1):
        item = asdict(chunk)
        item.update({
            "aggregated_rank": rank,
            "subquery_id": subquery.subquery_id,
            "claim_key": subquery.claim_key,
            "comparison_side_id": subquery.comparison_side_id,
            "provenance": [{
                "subquery_id": subquery.subquery_id,
                "comparison_side_id": subquery.comparison_side_id,
            }],
        })
        evidence.append(item)
    return {
        "status": "success", "evidence_completeness": "full",
        "required_sides": [subquery.comparison_side_id], "evidence": evidence,
    }


def run_benchmark(
    questions_path: Path,
    retriever: Any,
    *,
    expected_supported: int | None = None,
    expected_refusals: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    with questions_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    supported = [row for row in rows if row.get("question_group") != "refusal"]
    refusals = [row for row in rows if row.get("question_group") == "refusal"]
    if not supported:
        raise RuntimeError("question set contains no supported questions")
    if expected_supported is not None and len(supported) != expected_supported:
        raise RuntimeError(f"expected {expected_supported} supported questions; found {len(supported)}")
    if expected_refusals is not None and len(refusals) != expected_refusals:
        raise RuntimeError(f"expected {expected_refusals} refusal questions; found {len(refusals)}")

    resolver = SourceResolver.from_connection(retriever.conn)
    tickers, aliases = _aliases_from_connection(retriever.conn)
    retrieval_rows: list[dict[str, Any]] = []
    question_details: list[dict[str, Any]] = []

    for index, row in enumerate(supported, start=1):
        question_id = str(row.get("question_id", "")).strip()
        question = str(row.get("question", "")).strip()
        if not question_id or not question:
            raise ValueError("supported question_id and question must be nonempty")
        query_type, subqueries = build_subqueries(
            question_id, question, tickers, aliases, resolver,
        )
        adapter = ProductionRetrieverAdapter(
            retriever, SourceSpec, original_question=question,
        )
        if len(subqueries) == 1:
            result = _single_source_result(subqueries[0], adapter)
            method = "generic_focused_single_source"
        else:
            result = aggregate(subqueries, adapter, evidence_limit=FINAL_EVIDENCE_COUNT)
            method = "generic_focused_balanced_aggregation"
        if result.get("status") != "success":
            raise ContractError(
                str(result.get("error_code", "GENERIC_RETRIEVAL_FAILED")),
                f"{question_id}: {json.dumps(result, sort_keys=True)}",
            )
        evidence = result.get("evidence", [])
        if len(evidence) != FINAL_EVIDENCE_COUNT:
            raise RuntimeError(
                f"{question_id}: expected {FINAL_EVIDENCE_COUNT} final chunks; found {len(evidence)}"
            )
        details_evidence = [_evidence_details(item) for item in evidence]
        for rank, item in enumerate(details_evidence, start=1):
            if item["aggregated_rank"] != rank:
                raise RuntimeError(f"{question_id}: non-contiguous aggregated rank")
            retrieval_rows.append({
                "model_id": MODEL_ID, "question_id": question_id, "rank": rank,
                "chunk_id": item["chunk_id"], "score": item["cross_encoder_score"],
            })
        question_details.append({
            "question_id": question_id,
            "question_group": row.get("question_group", ""),
            "query_type": query_type.value,
            "method": method,
            "route_count": len(subqueries),
            "routes": [asdict(subquery) for subquery in subqueries],
            "status": "success",
            "evidence_completeness": result["evidence_completeness"],
            "required_sides": result["required_sides"],
            "evidence": details_evidence,
        })
        print(
            f"[{index}/{len(supported)}] {question_id} routes={len(subqueries)} "
            f"type={query_type.value} method={method}",
            file=sys.stderr, flush=True,
        )

    expected_rows = len(supported) * FINAL_EVIDENCE_COUNT
    if len(retrieval_rows) != expected_rows:
        raise RuntimeError(f"expected {expected_rows} retrieval rows; found {len(retrieval_rows)}")
    return retrieval_rows, {
        "contract_version": CONTRACT_VERSION,
        "experiment_mode": EXPERIMENT_MODE,
        "model_id": MODEL_ID,
        "supported_questions": len(supported),
        "refusals_excluded": len(refusals),
        "retrieval_rows": len(retrieval_rows),
        "questions": question_details,
    }


def _refuse_overwrite(paths: tuple[Path, ...]) -> None:
    existing = [str(path) for path in paths if path.exists()]
    if existing:
        raise FileExistsError("refusing to overwrite: " + ", ".join(existing))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--details", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--expected-supported", type=int)
    parser.add_argument("--expected-refusals", type=int)
    args = parser.parse_args()
    details_path = args.details or args.output.with_suffix(".details.json")
    manifest_path = args.manifest or args.output.with_suffix(".manifest.json")
    _refuse_overwrite((args.output, details_path, manifest_path))

    started_at = _utcnow()
    started = time.monotonic()
    with ProductionRetriever() as retriever:
        retrieval_rows, details = run_benchmark(
            args.questions, retriever,
            expected_supported=args.expected_supported,
            expected_refusals=args.expected_refusals,
        )
    elapsed_seconds = time.monotonic() - started
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
        "started_at": started_at, "finished_at": _utcnow(),
        "elapsed_seconds": time.monotonic() - started,
        "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "experiment_mode": EXPERIMENT_MODE,
        "supported_questions": details["supported_questions"],
        "refusals_excluded": details["refusals_excluded"],
        "retrieval_rows": len(retrieval_rows),
        "retrieval_inputs": ["question_id", "question"],
        "question_id_role": "output correlation only; excluded from decomposition identities",
        "question_group_role": "report stratification only; excluded from retrieval",
        "prohibited_retrieval_fields": list(PROHIBITED_RETRIEVAL_FIELDS),
        "approved_gold_routing_metadata_used": False,
        "gold_chunk_ids_used_for_retrieval": False,
        "gold_passages_used_for_retrieval": False,
        "expected_answers_used_for_retrieval": False,
        "questions_path": str(args.questions),
        "questions_sha256": _sha256(args.questions),
        "output_path": str(args.output), "output_sha256": _sha256(args.output),
        "details_path": str(details_path), "details_sha256": _sha256(details_path),
        "runner_sha256": _sha256(Path(__file__)),
        "query_decomposition_sha256": _sha256(REPO_ROOT / "src/query_decomposition.py"),
        "decomposed_query_api_sha256": _sha256(REPO_ROOT / "src/decomposed_query_api.py"),
        "policy": {
            "embedding_table": EMBEDDING_TABLE, "live_view": LIVE_VIEW,
            "bi_encoder": BI_ENCODER_REPO, "bi_encoder_revision": BI_ENCODER_REVISION,
            "candidates_per_source": CANDIDATES_PER_SOURCE,
            "cross_encoder": CROSS_ENCODER_REPO,
            "cross_encoder_revision": CROSS_ENCODER_REVISION,
            "final_evidence_count": FINAL_EVIDENCE_COUNT,
            "section_routing": "detected_hard_filter",
            "query_views": ["original", "focused", "section_profile"],
            "candidate_fusion": "multiview_rrf_k_60",
            "section_adaptive_policy_version": SECTION_ADAPTIVE_POLICY_VERSION,
            "narrative_section_order": "multiview_rrf",
            "item8_order": "fixed_cross_encoder",
            "unknown_section_order": "equal_rank_blend",
            "multi_source_merge": "deterministic_requirement_balanced_round_robin",
        },
    }
    with manifest_path.open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
