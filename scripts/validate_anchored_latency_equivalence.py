#!/usr/bin/env python3
"""Validate exact K16 identity between two latency benchmark runs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
from typing import Any


def load_rows(directory: Path) -> dict[str, dict[str, Any]]:
    path = directory / "responses.jsonl"
    rows: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            request_id = str(row["request_id"])
            if request_id in rows:
                raise ValueError(f"duplicate request_id: {request_id}")
            rows[request_id] = row
    return rows


def compare_runs(
    baseline: dict[str, dict[str, Any]],
    challenger: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    identifiers = sorted(set(baseline) | set(challenger))
    for request_id in identifiers:
        left = baseline.get(request_id)
        right = challenger.get(request_id)
        reasons = []
        if left is None or right is None:
            reasons.append("missing_request")
        else:
            if left.get("status") != "success" or right.get("status") != "success":
                reasons.append("non_success_status")
            if left.get("policy_id") != right.get("policy_id"):
                reasons.append("policy_mismatch")
            if left.get("evidence_count") != 16 or right.get("evidence_count") != 16:
                reasons.append("evidence_count")
            if left.get("evidence_identity") != right.get("evidence_identity"):
                reasons.append("evidence_identity")
            if left.get("evidence_identity_sha256") != right.get(
                "evidence_identity_sha256"
            ):
                reasons.append("identity_hash")
        if reasons:
            failures.append({"request_id": request_id, "reasons": reasons})

    def totals(rows: dict[str, dict[str, Any]]) -> list[float]:
        return [
            float(row["runtime_profile"]["timings_ms"]["total"])
            for row in rows.values()
            if row.get("status") == "success"
        ]

    left_times = totals(baseline)
    right_times = totals(challenger)
    speedup = None
    if left_times and right_times and statistics.mean(right_times) > 0:
        speedup = statistics.mean(left_times) / statistics.mean(right_times)
    return {
        "baseline_requests": len(baseline),
        "challenger_requests": len(challenger),
        "exact_matches": len(identifiers) - len(failures),
        "failures": failures,
        "baseline_mean_ms": round(statistics.mean(left_times), 3)
        if left_times else None,
        "challenger_mean_ms": round(statistics.mean(right_times), 3)
        if right_times else None,
        "mean_speedup": round(speedup, 3) if speedup is not None else None,
        "structural_pass": not failures and bool(identifiers),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-dir", type=Path, required=True)
    parser.add_argument("--challenger-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = compare_runs(
        load_rows(args.baseline_dir),
        load_rows(args.challenger_dir),
    )
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    print(payload, end="")
    if args.output:
        if args.output.exists():
            raise FileExistsError(args.output)
        args.output.write_text(payload, encoding="utf-8")
    print(
        "PASS: exact anchored latency equivalence"
        if report["structural_pass"]
        else "FAIL: anchored latency equivalence"
    )
    return 0 if report["structural_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
