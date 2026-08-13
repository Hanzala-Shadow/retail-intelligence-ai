#!/usr/bin/env python3
"""Replay frozen selection audits through the production pure selector."""
from __future__ import annotations

import argparse
from collections import OrderedDict
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.anchored_reranking import (  # noqa: E402
    AnchoredRerankingConfig,
    select_anchored_evidence,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection-audit", type=Path, required=True)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/retrieval_anchored_k16_v1.json"),
    )
    parser.add_argument("--expected-questions", type=int)
    args = parser.parse_args()
    config = AnchoredRerankingConfig.load(args.config)
    questions = 0
    exact = 0
    failures: list[dict[str, object]] = []
    with args.selection_audit.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            questions += 1
            groups: OrderedDict[str, dict[str, object]] = OrderedDict()
            for item in record["evidence"]:
                requirement_id = str(item["selected_for_subquery_id"])
                group = groups.setdefault(requirement_id, {
                    "requirement_id": requirement_id,
                    "hard": [],
                    "soft": [],
                })
                destination = (
                    "soft"
                    if item["selection_reason"] == "top_soft_per_requirement"
                    else "hard"
                )
                group[destination].append(dict(item))
            try:
                replayed = select_anchored_evidence(list(groups.values()), config)
                expected = [int(item["chunk_id"]) for item in record["evidence"]]
                actual = [int(item["chunk_id"]) for item in replayed]
                if actual == expected:
                    exact += 1
                else:
                    failures.append({
                        "question_id": record["question_id"],
                        "error": "selection_mismatch",
                        "expected": expected,
                        "actual": actual,
                    })
            except Exception as exc:  # validation report must retain the ID
                failures.append({
                    "question_id": record.get("question_id"),
                    "error": type(exc).__name__,
                    "message": str(exc),
                })
    if args.expected_questions is not None and questions != args.expected_questions:
        failures.append({
            "error": "question_count_mismatch",
            "expected": args.expected_questions,
            "actual": questions,
        })
    report = {
        "questions": questions,
        "exact_reproductions": exact,
        "failures": failures,
        "structural_pass": not failures and exact == questions,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    print(
        "PASS: selector exactly reproduces frozen audit"
        if report["structural_pass"]
        else "FAIL: selector replay differs from frozen audit"
    )
    return 0 if report["structural_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
