#!/usr/bin/env python3
"""Validate one anchored benchmark run against the frozen evidence identity."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


POLICY_ID = "balanced_anchored_round_robin_k16"


def validate(directory: Path, expected: str) -> dict:
    rows = []
    with (directory / "responses.jsonl").open(encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    failures = []
    for row in rows:
        reasons = []
        if row.get("status") != "success":
            reasons.append("status")
        if row.get("policy_id") != POLICY_ID:
            reasons.append("policy_id")
        if row.get("evidence_count") != 16:
            reasons.append("evidence_count")
        if row.get("evidence_identity_sha256") != expected:
            reasons.append("evidence_identity_sha256")
        identity = row.get("evidence_identity") or []
        if [item.get("final_rank") for item in identity] != list(range(1, 17)):
            reasons.append("final_ranks")
        if sum(item.get("selection_reason") == "l12_anchor" for item in identity) != 6:
            reasons.append("anchor_count")
        if reasons:
            failures.append({"request_id": row.get("request_id"), "reasons": reasons})
    return {
        "requests": len(rows),
        "expected_identity_sha256": expected,
        "failures": failures,
        "structural_pass": bool(rows) and not failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--expected-identity-sha256", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    expected = args.expected_identity_sha256.strip().lower()
    if len(expected) != 64:
        raise ValueError("expected identity SHA-256 must contain 64 hex characters")
    report = validate(args.run_dir, expected)
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    print(payload, end="")
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    print("PASS: frozen evidence identity" if report["structural_pass"] else "FAIL: frozen evidence identity")
    return 0 if report["structural_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
