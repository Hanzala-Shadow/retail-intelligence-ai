#!/usr/bin/env python3
"""Evaluate source/claim rewrites and requirement-aware aggregation on frozen-24.

Only the question and approved routing metadata are projected before retrieval.
The registered rewrite configuration contains human-authored claim descriptions,
never benchmark answers or gold chunk/passage metadata. All results are in-sample.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import resource
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.run_configurable_candidate_strategy_benchmark import (  # noqa: E402
    CandidateEngine,
    PROHIBITED_FIELDS,
    ROUTING_FIELDS,
    load_questions,
)
from scripts.run_production_retrieval_benchmark import build_sources  # noqa: E402
from src.query_api import (  # noqa: E402
    BI_ENCODER_REPO,
    BI_ENCODER_REVISION,
    CROSS_ENCODER_REPO,
    CROSS_ENCODER_REVISION,
    ProductionRetriever,
    SourceSpec,
    _round_robin,
)

DEFAULT_QUESTIONS = Path("data/00_reference/rag_eval_questions.csv")
DEFAULT_CONFIG = Path("config/retrieval_rewrite_requirements_v1.json")
SUPPORTED_QUESTIONS = 24
REFUSAL_QUESTIONS = 5
ALLOWED_METHODS = (
    "original_depth20_control",
    "source_only_original_control",
    "source_specific_rewrite",
    "claim_specific_rewrite",
    "claim_specific_requirement_aware",
    "source_claim_specific_rewrite",
    "source_claim_requirement_aware",
)


@dataclass(frozen=True)
class Requirement:
    requirement_id: str
    claim: str


@dataclass(frozen=True)
class RewriteConfig:
    experiment_id: str
    in_sample: bool
    semantic_depth: int
    final_evidence_count: int
    methods: tuple[str, ...]
    questions: dict[str, tuple[tuple[Requirement, ...], ...]]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git_head() -> str | None:
    try:
        return subprocess.check_output(
            [
                "git",
                "-c",
                f"safe.directory={REPO_ROOT}",
                "rev-parse",
                "HEAD",
            ],
            cwd=REPO_ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def load_config(path: Path) -> RewriteConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("schema_version") != 1:
        raise ValueError("configuration schema_version must be 1")
    if raw.get("in_sample") is not True:
        raise ValueError("frozen-24 rewrite experiments must declare in_sample=true")
    methods = tuple(raw.get("methods", []))
    if methods != ALLOWED_METHODS:
        raise ValueError("methods must exactly match the registered method order")
    depth = int(raw.get("semantic_depth", 0))
    final_count = int(raw.get("final_evidence_count", 0))
    if depth != 20 or final_count != 5:
        raise ValueError("registered control requires semantic_depth=20 and final_evidence_count=5")
    experiment_id = str(raw.get("experiment_id", "")).strip()
    if not experiment_id:
        raise ValueError("experiment_id must be non-empty")

    questions: dict[str, tuple[tuple[Requirement, ...], ...]] = {}
    seen_ids: set[str] = set()
    for question_id, item in raw.get("questions", {}).items():
        routes = []
        for route in item.get("routes", []):
            requirements = []
            for value in route:
                if not isinstance(value, list) or len(value) != 2:
                    raise ValueError(f"{question_id}: requirement must be [id, claim]")
                requirement_id, claim = (str(part).strip() for part in value)
                if not requirement_id or not claim or requirement_id in seen_ids:
                    raise ValueError("requirement IDs must be non-empty and globally unique")
                lowered = claim.casefold()
                if any(token in lowered for token in ("chunk_id", "supporting passage", "sha256", "token count")):
                    raise ValueError(f"{requirement_id}: prohibited gold-like configuration text")
                seen_ids.add(requirement_id)
                requirements.append(Requirement(requirement_id, claim))
            if not requirements:
                raise ValueError(f"{question_id}: routes cannot be empty")
            routes.append(tuple(requirements))
        if not routes:
            raise ValueError(f"{question_id}: routes cannot be empty")
        questions[str(question_id)] = tuple(routes)
    return RewriteConfig(experiment_id, True, depth, final_count, methods, questions)


def requirements_for(
    config: RewriteConfig, question_id: str, route_count: int
) -> tuple[tuple[Requirement, ...], ...]:
    registered = config.questions.get(question_id)
    if registered is None:
        return tuple(
            (Requirement(f"{question_id}-route-{index:02d}", "the question's requested disclosure"),)
            for index in range(1, route_count + 1)
        )
    if len(registered) != route_count:
        raise ValueError(f"{question_id}: configured route count does not match approved routing")
    return registered


def build_query(
    method: str,
    original_question: str,
    source: SourceSpec,
    requirement: Requirement,
    *,
    rewrite_enabled: bool = True,
) -> str:
    if not rewrite_enabled:
        return original_question
    source_text = (
        f"In {source.ticker}'s {source.filing_year} {source.doc_type}, "
        f"Section {source.section_code}"
    )
    if method in {"original_depth20_control", "source_only_original_control"}:
        return original_question
    if method == "source_specific_rewrite":
        return f"{source_text}, answer only this source's part of the question: {original_question}"
    if method in {"claim_specific_rewrite", "claim_specific_requirement_aware"}:
        return requirement.claim
    if method in {"source_claim_specific_rewrite", "source_claim_requirement_aware"}:
        return f"{source_text}, retrieve disclosures about {requirement.claim}."
    raise ValueError(f"unsupported method: {method}")


def _deduplicate_ranked(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[int, dict[str, Any]] = {}
    for row in rows:
        chunk_id = int(row["chunk_id"])
        previous = best.get(chunk_id)
        key = (-float(row["cross_encoder_score"]), row["requirement_id"], chunk_id)
        if previous is None:
            best[chunk_id] = row
        else:
            previous_key = (-float(previous["cross_encoder_score"]), previous["requirement_id"], chunk_id)
            if key < previous_key:
                best[chunk_id] = row
    return sorted(best.values(), key=lambda row: (-float(row["cross_encoder_score"]), int(row["chunk_id"])))


def aggregate_requirement_aware(
    ranked_by_requirement: list[list[dict[str, Any]]], limit: int
) -> tuple[list[dict[str, Any]], list[str]]:
    selected: list[dict[str, Any]] = []
    selected_ids: set[int] = set()
    uncovered: list[str] = []
    for ranked in ranked_by_requirement:
        requirement_id = ranked[0]["requirement_id"] if ranked else "unknown"
        candidate = next((row for row in ranked if int(row["chunk_id"]) not in selected_ids), None)
        if candidate is None or len(selected) >= limit:
            uncovered.append(requirement_id)
            continue
        selected.append(candidate)
        selected_ids.add(int(candidate["chunk_id"]))
    remaining = _deduplicate_ranked([row for ranked in ranked_by_requirement for row in ranked])
    for row in remaining:
        if len(selected) >= limit:
            break
        if int(row["chunk_id"]) not in selected_ids:
            selected.append(row)
            selected_ids.add(int(row["chunk_id"]))
    return selected, uncovered


def aggregate_score_first(
    ranked_by_requirement: list[list[dict[str, Any]]], limit: int
) -> list[dict[str, Any]]:
    return _deduplicate_ranked([row for ranked in ranked_by_requirement for row in ranked])[:limit]


def _retrieve_requirement(
    engine: CandidateEngine,
    query: str,
    source: SourceSpec,
    requirement: Requirement,
    depth: int,
) -> list[dict[str, Any]]:
    vector = engine.encode(query)
    semantic = engine.fetch_semantic(source, vector, depth)
    if not semantic:
        return []
    scores = engine.score_unique(query, [semantic])
    ranked = []
    for item in semantic:
        copied = dict(item)
        copied["cross_encoder_score"] = scores[int(item["chunk_id"])]
        copied["requirement_id"] = requirement.requirement_id
        copied["query"] = query
        copied["source"] = asdict(source)
        ranked.append(copied)
    ranked.sort(key=lambda row: (-row["cross_encoder_score"], int(row["chunk_id"])))
    for rank, row in enumerate(ranked, 1):
        row["cross_encoder_rank_within_requirement"] = rank
    return ranked


def run_benchmark(
    questions_path: Path, config: RewriteConfig, engine: CandidateEngine
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    questions = load_questions(questions_path)
    rows_by_method = {method: [] for method in config.methods}
    details: dict[str, Any] = {
        "experiment_id": config.experiment_id,
        "in_sample": True,
        "gold_fields_used": False,
        "routing_fields": list(ROUTING_FIELDS),
        "prohibited_fields": list(PROHIBITED_FIELDS),
        "semantic_depth": config.semantic_depth,
        "final_evidence_count": config.final_evidence_count,
        "questions": [],
    }
    for question_number, row in enumerate(questions, 1):
        sources = build_sources(row)
        rewrite_enabled = row["question_id"] in config.questions
        route_requirements = requirements_for(config, row["question_id"], len(sources))
        question_detail = {"question_id": row["question_id"], "question_group": row["question_group"], "methods": {}}
        retrieval_cache: dict[tuple[str, str, int, str, str, str], list[dict[str, Any]]] = {}
        for method in config.methods:
            ranked_groups = []
            for source, requirements in zip(sources, route_requirements, strict=True):
                method_requirements = requirements if method in {
                    "claim_specific_rewrite", "claim_specific_requirement_aware",
                    "source_claim_specific_rewrite", "source_claim_requirement_aware"
                } else (requirements[0],)
                for requirement in method_requirements:
                    query = build_query(
                        method,
                        row["question"],
                        source,
                        requirement,
                        rewrite_enabled=rewrite_enabled,
                    )
                    cache_key = (
                        query, source.ticker, source.filing_year, source.doc_type,
                        source.accession_number, source.section_code,
                    )
                    cached = retrieval_cache.get(cache_key)
                    if cached is None:
                        cached = _retrieve_requirement(
                            engine, query, source, requirement, config.semantic_depth
                        )
                        retrieval_cache[cache_key] = cached
                    ranked = [dict(item, requirement_id=requirement.requirement_id) for item in cached]
                    if not ranked:
                        raise RuntimeError(f"{row['question_id']} {method} {requirement.requirement_id}: empty route")
                    ranked_groups.append(ranked)
            if not rewrite_enabled:
                evidence = _round_robin(ranked_groups, config.final_evidence_count)
                uncovered = []
            elif method in {
                "claim_specific_requirement_aware",
                "source_claim_requirement_aware",
            }:
                evidence, uncovered = aggregate_requirement_aware(ranked_groups, config.final_evidence_count)
            elif method in {
                "original_depth20_control",
                "source_only_original_control",
                "source_specific_rewrite",
            }:
                evidence = _round_robin(ranked_groups, config.final_evidence_count)
                uncovered = []
            else:
                evidence = aggregate_score_first(ranked_groups, config.final_evidence_count)
                uncovered = []
            if len(evidence) != config.final_evidence_count:
                raise RuntimeError(f"{row['question_id']} {method}: expected five evidence rows")
            compact = []
            for final_rank, item in enumerate(evidence, 1):
                rows_by_method[method].append({
                    "model_id": method, "question_id": row["question_id"], "rank": final_rank,
                    "chunk_id": int(item["chunk_id"]), "score": item["cross_encoder_score"],
                })
                compact.append({
                    "final_rank": final_rank, "chunk_id": int(item["chunk_id"]),
                    "chunk_index": int(item["chunk_index"]), "semantic_rank": item.get("semantic_rank"),
                    "cross_encoder_score": item["cross_encoder_score"],
                    "cross_encoder_rank_within_requirement": item["cross_encoder_rank_within_requirement"],
                    "requirement_id": item["requirement_id"], "query": item["query"], "source": item["source"],
                })
            question_detail["methods"][method] = {
                "status": "insufficient_evidence" if uncovered else "ok",
                "uncovered_requirements": uncovered,
                "evidence": compact,
            }
        details["questions"].append(question_detail)
        print(f"[{question_number}/{len(questions)}] {row['question_id']} routes={len(sources)}", file=sys.stderr, flush=True)
    expected = SUPPORTED_QUESTIONS * config.final_evidence_count
    if any(len(rows) != expected for rows in rows_by_method.values()):
        raise RuntimeError("each method must produce exactly 120 rows")
    return rows_by_method, details


def _write_outputs(output_dir: Path, rows_by_method: dict[str, list[dict[str, Any]]], details: dict[str, Any]) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for method, rows in rows_by_method.items():
        path = output_dir / f"{method}.csv"
        with path.open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=("model_id", "question_id", "rank", "chunk_id", "score"))
            writer.writeheader()
            writer.writerows(rows)
        hashes[str(path)] = _sha256(path)
    details_path = output_dir / "rewrite_details.json"
    details_path.write_text(json.dumps(details, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    hashes[str(details_path)] = _sha256(details_path)
    return hashes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.output_dir.exists():
        raise FileExistsError(f"refusing existing output directory: {args.output_dir}")
    config = load_config(args.config)
    started_at, started = _utcnow(), time.monotonic()
    with ProductionRetriever() as retriever:
        retriever._load_models()
        rows, details = run_benchmark(args.questions, config, CandidateEngine(retriever))
    hashes = _write_outputs(args.output_dir, rows, details)
    manifest = {
        "started_at": started_at, "finished_at": _utcnow(), "elapsed_seconds": time.monotonic() - started,
        "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, "git_head": _git_head(),
        "questions_path": str(args.questions), "questions_sha256": _sha256(args.questions),
        "config_path": str(args.config), "config_sha256": _sha256(args.config),
        "script_sha256": _sha256(Path(__file__)), "experiment_id": config.experiment_id,
        "in_sample": True, "database_writes": False, "question_count": SUPPORTED_QUESTIONS,
        "refusals_excluded": REFUSAL_QUESTIONS, "method_count": len(config.methods),
        "bi_encoder": BI_ENCODER_REPO, "bi_encoder_revision": BI_ENCODER_REVISION,
        "cross_encoder": CROSS_ENCODER_REPO, "cross_encoder_revision": CROSS_ENCODER_REVISION,
        "output_hashes": hashes,
    }
    manifest_path = args.output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
