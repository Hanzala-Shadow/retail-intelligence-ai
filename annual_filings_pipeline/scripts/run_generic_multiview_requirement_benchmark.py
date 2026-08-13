#!/usr/bin/env python3
"""Run leakage-free generic multi-view, requirement-aware retrieval ablations."""

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

from scripts.run_configurable_candidate_strategy_benchmark import CandidateEngine  # noqa: E402
from src.decomposed_query_api import _aliases_from_connection  # noqa: E402
from src.query_api import (  # noqa: E402
    BI_ENCODER_REPO, BI_ENCODER_REVISION, CANDIDATES_PER_SOURCE,
    CROSS_ENCODER_BATCH_SIZE, CROSS_ENCODER_REPO, CROSS_ENCODER_REVISION,
    EMBEDDING_TABLE, FINAL_EVIDENCE_COUNT, LIVE_VIEW, ProductionRetriever,
    SourceSpec,
)
from src.query_decomposition import SourceResolver, build_subqueries  # noqa: E402

EXPERIMENT_MODE = "generic_multiview_requirement_aware_ablation"
SEMANTIC_DEPTH = CANDIDATES_PER_SOURCE
RRF_K = 60
POOL_LIMIT = 60
CSV_FIELDS = ("model_id", "question_id", "rank", "chunk_id", "score")

SECTION_PROFILES = {
    "Item_1": "business operations products services customers channels stores distribution sourcing suppliers competition",
    "Item_1A": "risk factors exposure uncertainty adverse impact mitigation regulation cybersecurity supply chain macroeconomic",
    "Item_7": "management discussion analysis results operations trends drivers changes year over year revenue margin expenses inventory liquidity cash flows",
    "Item_8": "financial statements notes accounting policy recognition measurement estimates commitments contingencies impairment taxes leases",
}
STRATEGIES = {
    "focused_control": ("focused",),
    "original_plus_focused": ("original", "focused"),
    "focused_plus_section_profile": ("focused", "profile"),
    "all_views_requirement_aware": ("original", "focused", "profile"),
    "section_adaptive_semantic_control": ("original", "focused", "profile"),
    "section_adaptive_plus_claim_lexical": (
        "original", "focused", "profile", "claim_lexical",
    ),
}
SECTION_ADAPTIVE_STRATEGIES = {
    "section_adaptive_semantic_control",
    "section_adaptive_plus_claim_lexical",
}
SELECTION_POLICY = {
    "control": "section_adaptive_semantic_control",
    "candidate": "section_adaptive_plus_claim_lexical",
    "primary_metric": "complete_requirement_coverage_at_5",
    "minimum_overall_primary_gain": 0.05,
    "minimum_cross_company_complete_coverage": 0.70,
    "maximum_overall_mrr_regression": 0.02,
    "maximum_overall_ndcg_regression": 0.02,
    "maximum_group_mrr_regression": 0.02,
    "required_direct_hit_at_5": 1.0,
    "required_judged_at_5": 1.0,
    "all_hard_gates_must_pass": True,
    "supplemental_blind_adjudication_required_for_unjudged_top5": True,
    "production_change_allowed_from_this_run": False,
    "purpose": (
        "development ablation only; passing requires human-confirmed supplemental "
        "judgments and independent sealed-100 certification"
    ),
}
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


def query_views(original_question: str, subquery: Any) -> dict[str, str]:
    profile = SECTION_PROFILES.get(
        subquery.section_code,
        "annual report disclosure description factors changes impacts",
    )
    return {
        "original": original_question,
        "focused": subquery.question,
        "profile": f"{subquery.claim_key}. {profile}",
    }


def fuse_candidates(
    per_view: dict[str, list[dict[str, Any]]],
    enabled_views: tuple[str, ...],
    *,
    rrf_k: int = RRF_K,
    pool_limit: int = POOL_LIMIT,
) -> list[dict[str, Any]]:
    by_chunk: dict[int, dict[str, Any]] = {}
    for view in enabled_views:
        for rank, row in enumerate(per_view[view], start=1):
            chunk_id = int(row["chunk_id"])
            item = by_chunk.setdefault(chunk_id, {
                "chunk_id": chunk_id,
                "embedding_text": row.get("embedding_text") or "",
                "view_ranks": {},
                "rrf_score": 0.0,
            })
            item["view_ranks"][view] = rank
            item["rrf_score"] += 1.0 / (rrf_k + rank)
    ranked = sorted(
        by_chunk.values(),
        key=lambda row: (-float(row["rrf_score"]), int(row["chunk_id"])),
    )
    return ranked[:pool_limit]


def rerank_pool(
    focused_query: str,
    pool: list[dict[str, Any]],
    cross_encoder: Any,
) -> list[dict[str, Any]]:
    pairs = [(focused_query, row["embedding_text"]) for row in pool]
    scores = cross_encoder.predict(
        pairs,
        batch_size=CROSS_ENCODER_BATCH_SIZE,
        show_progress_bar=False,
        convert_to_numpy=True,
    )
    output = []
    for row, score in zip(pool, scores, strict=True):
        copied = dict(row)
        copied["cross_encoder_score"] = float(score)
        output.append(copied)
    output.sort(key=lambda row: (
        -float(row["cross_encoder_score"]),
        -float(row["rrf_score"]),
        int(row["chunk_id"]),
    ))
    for rank, row in enumerate(output, start=1):
        row["cross_encoder_rank"] = rank
    return output


def rank_pool_with_scores(
    pool: list[dict[str, Any]],
    score_by_chunk: dict[int, float],
) -> list[dict[str, Any]]:
    output = []
    for row in pool:
        copied = dict(row)
        copied["cross_encoder_score"] = score_by_chunk[int(row["chunk_id"])]
        output.append(copied)
    output.sort(key=lambda row: (
        -float(row["cross_encoder_score"]),
        -float(row["rrf_score"]),
        int(row["chunk_id"]),
    ))
    for rank, row in enumerate(output, start=1):
        row["cross_encoder_rank"] = rank
    return output


def adaptive_rank_pool(
    pool: list[dict[str, Any]],
    score_by_chunk: dict[int, float],
    section_code: str,
) -> list[dict[str, Any]]:
    ranked = rank_pool_with_scores(pool, score_by_chunk)
    if section_code == "Item_8":
        return ranked
    if section_code in {"Item_1", "Item_1A", "Item_7"}:
        ranked.sort(key=lambda row: (
            -float(row["rrf_score"]),
            int(row["cross_encoder_rank"]),
            int(row["chunk_id"]),
        ))
        for rank, row in enumerate(ranked, start=1):
            row["adaptive_rank"] = rank
        return ranked
    rrf_order = sorted(
        ranked,
        key=lambda row: (-float(row["rrf_score"]), int(row["chunk_id"])),
    )
    rrf_rank = {int(row["chunk_id"]): rank for rank, row in enumerate(rrf_order, 1)}
    for row in ranked:
        row["adaptive_score"] = (
            0.5 / int(row["cross_encoder_rank"])
            + 0.5 / rrf_rank[int(row["chunk_id"])]
        )
    ranked.sort(key=lambda row: (
        -float(row["adaptive_score"]), int(row["chunk_id"]),
    ))
    for rank, row in enumerate(ranked, start=1):
        row["adaptive_rank"] = rank
    return ranked


def balanced_select(
    per_requirement: list[list[dict[str, Any]]],
    limit: int = FINAL_EVIDENCE_COUNT,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[int] = set()
    depth = 0
    while len(output) < limit and any(depth < len(rows) for rows in per_requirement):
        for requirement_index, rows in enumerate(per_requirement, start=1):
            if depth >= len(rows):
                continue
            row = rows[depth]
            chunk_id = int(row["chunk_id"])
            if chunk_id in seen:
                continue
            copied = dict(row)
            copied["requirement_index"] = requirement_index
            output.append(copied)
            seen.add(chunk_id)
            if len(output) == limit:
                break
        depth += 1
    if len(output) != limit:
        raise RuntimeError(f"requirement-aware selection returned {len(output)} rows; expected {limit}")
    for rank, row in enumerate(output, start=1):
        row["final_rank"] = rank
    return output


def run_benchmark(
    questions_path: Path,
    retriever: ProductionRetriever,
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

    retriever._load_models()
    engine = CandidateEngine(retriever)
    resolver = SourceResolver.from_connection(retriever.conn)
    tickers, aliases = _aliases_from_connection(retriever.conn)
    retrieval_rows: list[dict[str, Any]] = []
    question_details: list[dict[str, Any]] = []

    for question_index, row in enumerate(supported, start=1):
        question_id = str(row.get("question_id", "")).strip()
        question = str(row.get("question", "")).strip()
        query_type, subqueries = build_subqueries(
            question_id, question, tickers, aliases, resolver,
        )
        route_data = []
        for subquery in subqueries:
            source = SourceSpec(
                subquery.ticker, subquery.filing_year, subquery.accession_number,
                subquery.section_code, subquery.doc_type,
            )
            views = query_views(question, subquery)
            per_view = {}
            for view_name, view_query in views.items():
                vector = engine.encode(view_query)
                per_view[view_name] = engine.fetch_semantic(
                    source, vector, SEMANTIC_DEPTH,
                )
            views["claim_lexical"] = subquery.claim_key
            per_view["claim_lexical"] = engine.fetch_lexical(
                subquery.claim_key, source, SEMANTIC_DEPTH,
            )
            all_pool = fuse_candidates(per_view, tuple(per_view))
            all_ranked = rerank_pool(
                views["focused"], all_pool, retriever.cross_encoder,
            )
            route_data.append({
                "subquery": subquery, "source": source,
                "views": views, "per_view": per_view,
                "cross_scores": {
                    int(item["chunk_id"]): float(item["cross_encoder_score"])
                    for item in all_ranked
                },
            })

        strategy_details = {}
        for strategy_id, enabled_views in STRATEGIES.items():
            ranked_routes = []
            route_details = []
            for route in route_data:
                pool = fuse_candidates(route["per_view"], enabled_views)
                if strategy_id in SECTION_ADAPTIVE_STRATEGIES:
                    ranked = adaptive_rank_pool(
                        pool, route["cross_scores"],
                        route["subquery"].section_code,
                    )
                else:
                    ranked = rank_pool_with_scores(pool, route["cross_scores"])
                ranked_routes.append(ranked)
                route_details.append({
                    "subquery": asdict(route["subquery"]),
                    "enabled_views": list(enabled_views),
                    "view_queries": {name: route["views"][name] for name in enabled_views},
                    "view_candidate_ids": {
                        name: [int(item["chunk_id"]) for item in route["per_view"][name]]
                        for name in enabled_views
                    },
                    "pool": [{
                        "chunk_id": int(item["chunk_id"]),
                        "rrf_score": float(item["rrf_score"]),
                        "view_ranks": item["view_ranks"],
                        "cross_encoder_score": float(item["cross_encoder_score"]),
                        "cross_encoder_rank": int(item["cross_encoder_rank"]),
                    } for item in ranked],
                })
            final = balanced_select(ranked_routes)
            model_id = f"bge_base_fixed__{strategy_id}"
            for item in final:
                retrieval_rows.append({
                    "model_id": model_id, "question_id": question_id,
                    "rank": item["final_rank"], "chunk_id": item["chunk_id"],
                    "score": item["cross_encoder_score"],
                })
            strategy_details[strategy_id] = {
                "enabled_views": list(enabled_views),
                "routes": route_details,
                "final_evidence": [{
                    "rank": int(item["final_rank"]),
                    "chunk_id": int(item["chunk_id"]),
                    "requirement_index": int(item["requirement_index"]),
                    "cross_encoder_score": float(item["cross_encoder_score"]),
                } for item in final],
            }
        question_details.append({
            "question_id": question_id,
            "question_group": row.get("question_group", ""),
            "query_type": query_type.value,
            "route_count": len(subqueries),
            "strategies": strategy_details,
        })
        print(
            f"[{question_index}/{len(supported)}] {question_id} "
            f"routes={len(subqueries)} strategies={len(STRATEGIES)}",
            file=sys.stderr, flush=True,
        )

    expected_rows = len(supported) * len(STRATEGIES) * FINAL_EVIDENCE_COUNT
    if len(retrieval_rows) != expected_rows:
        raise RuntimeError(f"expected {expected_rows} retrieval rows; found {len(retrieval_rows)}")
    return retrieval_rows, {
        "experiment_mode": EXPERIMENT_MODE,
        "supported_questions": len(supported),
        "refusals_excluded": len(refusals),
        "strategy_count": len(STRATEGIES),
        "retrieval_rows": len(retrieval_rows),
        "questions": question_details,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--details", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--expected-supported", type=int)
    parser.add_argument("--expected-refusals", type=int)
    args = parser.parse_args()
    paths = (args.output, args.details, args.manifest)
    existing = [str(path) for path in paths if path.exists()]
    if existing:
        raise FileExistsError("refusing to overwrite: " + ", ".join(existing))

    started_at = _utcnow()
    started = time.monotonic()
    with ProductionRetriever() as retriever:
        retrieval_rows, details = run_benchmark(
            args.questions, retriever,
            expected_supported=args.expected_supported,
            expected_refusals=args.expected_refusals,
        )
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(retrieval_rows)
    with args.details.open("x", encoding="utf-8") as handle:
        json.dump(details, handle, indent=2, sort_keys=True)
        handle.write("\n")
    manifest = {
        "started_at": started_at, "finished_at": _utcnow(),
        "elapsed_seconds": time.monotonic() - started,
        "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "experiment_mode": EXPERIMENT_MODE,
        "retrieval_inputs": ["question_id", "question"],
        "question_id_role": "output correlation only",
        "question_group_role": "evaluation stratification only",
        "prohibited_retrieval_fields": list(PROHIBITED_RETRIEVAL_FIELDS),
        "approved_gold_routing_metadata_used": False,
        "gold_chunk_ids_used_for_retrieval": False,
        "gold_passages_used_for_retrieval": False,
        "expected_answers_used_for_retrieval": False,
        "models_fixed": True,
        "lexical_candidate_policy": "postgres_english_websearch_to_tsquery_on_claim_key",
        "strategies": {key: list(value) for key, value in STRATEGIES.items()},
        "selection_policy": SELECTION_POLICY,
        "section_profiles": SECTION_PROFILES,
        "semantic_depth_per_view": SEMANTIC_DEPTH,
        "rrf_k": RRF_K, "pool_limit": POOL_LIMIT,
        "final_evidence_count": FINAL_EVIDENCE_COUNT,
        "bi_encoder": BI_ENCODER_REPO, "bi_encoder_revision": BI_ENCODER_REVISION,
        "cross_encoder": CROSS_ENCODER_REPO,
        "cross_encoder_revision": CROSS_ENCODER_REVISION,
        "embedding_table": EMBEDDING_TABLE, "live_view": LIVE_VIEW,
        "questions_path": str(args.questions),
        "questions_sha256": _sha256(args.questions),
        "output_path": str(args.output), "output_sha256": _sha256(args.output),
        "details_path": str(args.details), "details_sha256": _sha256(args.details),
        "runner_sha256": _sha256(Path(__file__)),
        "query_decomposition_sha256": _sha256(REPO_ROOT / "src/query_decomposition.py"),
        "decomposed_query_api_sha256": _sha256(REPO_ROOT / "src/decomposed_query_api.py"),
    }
    with args.manifest.open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
