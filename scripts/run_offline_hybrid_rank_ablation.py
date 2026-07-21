#!/usr/bin/env python3
"""Re-rank frozen generic candidate pools without models or gold fields."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

METHODS = {
    "rrf_only": 0.00,
    "hybrid_ce25_rrf75": 0.25,
    "hybrid_ce50_rrf50": 0.50,
    "hybrid_ce75_rrf25": 0.75,
    "cross_encoder_only": 1.00,
}
RANK_OFFSET = 0
FINAL_EVIDENCE_COUNT = 5
CSV_FIELDS = ("model_id", "question_id", "rank", "chunk_id", "score")
SELECTION_POLICY = {
    "control": "cross_encoder_only",
    "primary_metric": "mrr_at_5",
    "minimum_overall_primary_gain": 0.03,
    "minimum_groups_improved": 4,
    "maximum_allowed_group_primary_regression": 0.02,
    "tie_breakers": ["ndcg_at_5", "recall_at_5", "hit_at_5"],
    "all_hard_gates_must_pass": True,
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def hybrid_rank(pool: list[dict[str, Any]], ce_weight: float) -> list[dict[str, Any]]:
    if not 0.0 <= ce_weight <= 1.0:
        raise ValueError("ce_weight must be between zero and one")
    rrf_order = sorted(
        pool,
        key=lambda row: (-float(row["rrf_score"]), int(row["chunk_id"])),
    )
    rrf_rank = {int(row["chunk_id"]): rank for rank, row in enumerate(rrf_order, 1)}
    output = []
    for row in pool:
        copied = dict(row)
        chunk_id = int(row["chunk_id"])
        ce_rank = int(row["cross_encoder_rank"])
        semantic_fusion_rank = rrf_rank[chunk_id]
        score = (
            ce_weight / (RANK_OFFSET + ce_rank)
            + (1.0 - ce_weight) / (RANK_OFFSET + semantic_fusion_rank)
        )
        copied["rrf_rank"] = semantic_fusion_rank
        copied["hybrid_score"] = score
        output.append(copied)
    output.sort(key=lambda row: (
        -float(row["hybrid_score"]),
        int(row["cross_encoder_rank"]),
        int(row["rrf_rank"]),
        int(row["chunk_id"]),
    ))
    return output


def balanced_select(per_requirement: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    output = []
    seen = set()
    depth = 0
    while len(output) < FINAL_EVIDENCE_COUNT and any(
        depth < len(rows) for rows in per_requirement
    ):
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
            if len(output) == FINAL_EVIDENCE_COUNT:
                break
        depth += 1
    if len(output) != FINAL_EVIDENCE_COUNT:
        raise RuntimeError("candidate pools cannot fill the final evidence budget")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--details", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.manifest.exists():
        raise FileExistsError("refusing to overwrite output or manifest")

    details = json.loads(args.details.read_text(encoding="utf-8"))
    retrieval_rows = []
    for question in details["questions"]:
        question_id = question["question_id"]
        base = question["strategies"]["all_views_requirement_aware"]
        routes = base["routes"]
        for method_id, ce_weight in METHODS.items():
            ranked_routes = [
                hybrid_rank(route["pool"], ce_weight)
                for route in routes
            ]
            final = balanced_select(ranked_routes)
            for rank, item in enumerate(final, start=1):
                retrieval_rows.append({
                    "model_id": f"bge_base_fixed__{method_id}",
                    "question_id": question_id,
                    "rank": rank,
                    "chunk_id": int(item["chunk_id"]),
                    "score": float(item["hybrid_score"]),
                })

    expected = len(details["questions"]) * len(METHODS) * FINAL_EVIDENCE_COUNT
    if len(retrieval_rows) != expected:
        raise RuntimeError(f"expected {expected} retrieval rows; found {len(retrieval_rows)}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(retrieval_rows)
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "experiment_mode": "offline_frozen_pool_hybrid_rank_ablation",
        "models_executed": False,
        "database_used": False,
        "gold_fields_used": False,
        "source_details_path": str(args.details),
        "source_details_sha256": _sha256(args.details),
        "methods": METHODS,
        "rank_offset": RANK_OFFSET,
        "rank_formula": "ce_weight/(ce_rank) + (1-ce_weight)/(multiview_rrf_rank)",
        "final_selection": "deterministic_requirement_balanced_round_robin",
        "final_evidence_count": FINAL_EVIDENCE_COUNT,
        "selection_policy": SELECTION_POLICY,
        "output_path": str(args.output),
        "output_sha256": _sha256(args.output),
        "runner_sha256": _sha256(Path(__file__)),
    }
    with args.manifest.open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
