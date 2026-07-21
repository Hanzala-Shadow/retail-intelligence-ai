#!/usr/bin/env python3
"""Join human-confirmed blind grades to private chunk provenance."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

FIELDS = (
    "question_id", "requirement_index", "chunk_id", "relevance_grade",
    "direct_evidence", "candidate_code", "blind_reviewer_id",
    "human_confirmer_id", "human_confirmation_note",
)


def _read(path: Path):
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _sha(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reviewed-blind", type=Path, required=True)
    parser.add_argument("--private-provenance", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--human-confirmer-id", required=True)
    parser.add_argument("--confirmation-note", required=True)
    parser.add_argument("--human-confirmed", action="store_true")
    args = parser.parse_args()
    if not args.human_confirmed:
        raise ValueError("--human-confirmed is required; AI preliminary grades cannot become qrels automatically")
    if args.output.exists() or args.manifest.exists():
        raise FileExistsError("refusing to overwrite output or manifest")

    reviewed = _read(args.reviewed_blind)
    provenance = _read(args.private_provenance)
    by_code = {row["candidate_code"]: row for row in provenance}
    if len(by_code) != len(provenance):
        raise ValueError("private provenance contains duplicate candidate codes")
    if {row["candidate_code"] for row in reviewed} != set(by_code):
        raise ValueError("reviewed and provenance candidate-code sets differ")
    output = []
    for row in reviewed:
        grade = row["relevance_grade"].strip().upper()
        if grade not in {"0", "1", "2"}:
            raise ValueError(f"candidate {row['candidate_code']} is not finally adjudicated: {grade}")
        direct = row["direct_evidence"].strip().upper()
        if (grade == "2") != (direct == "YES"):
            raise ValueError(f"grade/direct mismatch for {row['candidate_code']}")
        private = by_code[row["candidate_code"]]
        if (
            private["question_id"] != row["question_id"]
            or int(private["requirement_index"]) != int(row["requirement_index"])
        ):
            raise ValueError(f"identity mismatch for {row['candidate_code']}")
        output.append({
            "question_id": row["question_id"],
            "requirement_index": int(row["requirement_index"]),
            "chunk_id": int(private["chunk_id"]),
            "relevance_grade": int(grade),
            "direct_evidence": direct,
            "candidate_code": row["candidate_code"],
            "blind_reviewer_id": row["reviewer_id"],
            "human_confirmer_id": args.human_confirmer_id,
            "human_confirmation_note": args.confirmation_note,
        })
    requirements = {(row["question_id"], row["requirement_index"]) for row in output}
    direct_requirements = {
        (row["question_id"], row["requirement_index"])
        for row in output if row["relevance_grade"] == 2
    }
    if direct_requirements != requirements:
        missing = sorted(requirements - direct_requirements)
        raise ValueError(f"requirements without direct qrels: {missing}")
    output.sort(key=lambda row: (
        row["question_id"], row["requirement_index"],
        -row["relevance_grade"], row["chunk_id"],
    ))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(output)
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "HUMAN_CONFIRMED_GRADED_QRELS",
        "human_confirmer_id": args.human_confirmer_id,
        "confirmation_note": args.confirmation_note,
        "judgments": len(output),
        "questions": len({row["question_id"] for row in output}),
        "requirements": len(requirements),
        "grade_counts": {
            str(grade): sum(row["relevance_grade"] == grade for row in output)
            for grade in (0, 1, 2)
        },
        "reviewed_blind_sha256": _sha(args.reviewed_blind),
        "private_provenance_sha256": _sha(args.private_provenance),
        "output_path": str(args.output),
        "output_sha256": _sha(args.output),
        "runner_sha256": _sha(Path(__file__)),
    }
    args.manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
