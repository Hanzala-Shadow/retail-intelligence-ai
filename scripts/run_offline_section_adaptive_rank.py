#!/usr/bin/env python3
"""Apply a fixed section-family rank policy to frozen multiview pools."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.run_offline_hybrid_rank_ablation import balanced_select, hybrid_rank

METHODS = (
    "cross_encoder_control",
    "rrf_control",
    "section_adaptive_v1",
)
SECTION_CE_WEIGHTS = {
    "Item_1": 0.0,
    "Item_1A": 0.0,
    "Item_7": 0.0,
    "Item_8": 1.0,
}
DEFAULT_CE_WEIGHT = 0.5
CSV_FIELDS = ("model_id", "question_id", "rank", "chunk_id", "score")
SELECTION_POLICY = {
    "control": "cross_encoder_control",
    "primary_metric": "mrr_at_5",
    "minimum_overall_primary_gain": 0.03,
    "minimum_groups_improved": 4,
    "maximum_allowed_group_primary_regression": 0.02,
    "all_hard_gates_must_pass": True,
    "tie_breakers": ["ndcg_at_5", "recall_at_5", "hit_at_5"],
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def section_weight(section_code: str) -> float:
    return SECTION_CE_WEIGHTS.get(section_code, DEFAULT_CE_WEIGHT)


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
        routes = question["strategies"]["all_views_requirement_aware"]["routes"]
        for method in METHODS:
            ranked_routes = []
            applied_weights = []
            for route in routes:
                section = route["subquery"]["section_code"]
                if method == "cross_encoder_control":
                    weight = 1.0
                elif method == "rrf_control":
                    weight = 0.0
                else:
                    weight = section_weight(section)
                ranked_routes.append(hybrid_rank(route["pool"], weight))
                applied_weights.append(weight)
            final = balanced_select(ranked_routes)
            for rank, item in enumerate(final, start=1):
                retrieval_rows.append({
                    "model_id": f"bge_base_fixed__{method}",
                    "question_id": question_id,
                    "rank": rank,
                    "chunk_id": int(item["chunk_id"]),
                    "score": float(item["hybrid_score"]),
                })

    expected = len(details["questions"]) * len(METHODS) * 5
    if len(retrieval_rows) != expected:
        raise RuntimeError(f"expected {expected} rows; found {len(retrieval_rows)}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(retrieval_rows)
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "experiment_mode": "offline_section_family_adaptive_rank_v1",
        "models_executed": False,
        "database_used": False,
        "gold_fields_used": False,
        "methods": list(METHODS),
        "section_cross_encoder_weights": SECTION_CE_WEIGHTS,
        "default_cross_encoder_weight": DEFAULT_CE_WEIGHT,
        "policy_interpretation": {
            "narrative_sections": "multiview semantic RRF ordering",
            "financial_statement_notes": "fixed cross-encoder ordering",
            "unknown_sections": "equal cross-encoder and RRF rank blend",
        },
        "selection_policy": SELECTION_POLICY,
        "source_details_path": str(args.details),
        "source_details_sha256": _sha256(args.details),
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
