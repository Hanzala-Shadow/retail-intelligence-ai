#!/usr/bin/env python3
"""Build blind evidence and gold-coverage semantic review CSVs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

DEFAULT_MODEL_ID = "bge_base_fixed__section_adaptive_v1"
BLIND_FIELDS = (
    "question_id", "question_group", "question", "retrieval_rank", "chunk_id",
    "ticker", "filing_year", "accession_number", "doc_type", "section_code",
    "chunk_index", "retrieved_chunk_text", "relevance_grade",
    "directly_supports_question", "evidence_sufficiency", "reviewer_notes",
)
GOLD_FIELDS = (
    "question_id", "question_group", "question", "expected_answer",
    "original_gold_chunk_ids", "original_gold_passages", "retrieved_chunk_ids",
    "blind_review_completed", "original_gold_valid", "gold_label_complete",
    "valid_alternative_chunk_ids", "annotation_correction_required",
    "adjudicator_notes", "final_review_status",
)
SUMMARY_FIELDS = (
    "question_id", "question_group", "blind_rows_expected", "blind_rows_completed",
    "direct_count", "partial_count", "irrelevant_count", "answer_supported_at_5",
    "gold_review_completed", "final_review_status",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, object]]) -> None:
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def load_selected_retrieval(
    path: Path, model_id: str,
) -> dict[str, list[dict[str, str]]]:
    rows = [row for row in _read_csv(path) if row.get("model_id") == model_id]
    by_question: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        by_question.setdefault(row["question_id"], []).append(row)
    for question_id, values in by_question.items():
        values.sort(key=lambda row: int(row["rank"]))
        ranks = [int(row["rank"]) for row in values]
        if ranks != [1, 2, 3, 4, 5]:
            raise ValueError(f"{question_id}: expected retrieval ranks 1 through 5; got {ranks}")
    return by_question


def load_needed_chunks(path: Path, needed: set[str]) -> dict[str, dict[str, str]]:
    csv.field_size_limit(sys.maxsize)
    found: dict[str, dict[str, str]] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            chunk_id = str(row.get("chunk_id", "")).strip()
            if chunk_id in needed:
                found[chunk_id] = row
                if len(found) == len(needed):
                    break
    missing = sorted(needed - set(found))
    if missing:
        raise ValueError(f"chunk metadata missing {len(missing)} selected chunks: {missing[:10]}")
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--retrieval", type=Path, required=True)
    parser.add_argument("--chunk-metadata", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--expected-questions", type=int, default=50)
    args = parser.parse_args()

    output_paths = {
        "blind": args.output_dir / "01_blind_evidence_review.csv",
        "gold": args.output_dir / "02_gold_coverage_adjudication.csv",
        "summary": args.output_dir / "03_review_completion_summary.csv",
        "instructions": args.output_dir / "SEMANTIC_ADJUDICATION_INSTRUCTIONS.md",
        "manifest": args.output_dir / "semantic_adjudication_manifest.json",
    }
    existing = [str(path) for path in output_paths.values() if path.exists()]
    if existing:
        raise FileExistsError("refusing to overwrite: " + ", ".join(existing))

    questions = [
        row for row in _read_csv(args.questions)
        if row.get("question_group") != "refusal"
    ]
    if len(questions) != args.expected_questions:
        raise ValueError(f"expected {args.expected_questions} questions; found {len(questions)}")
    retrieval = load_selected_retrieval(args.retrieval, args.model_id)
    question_ids = {row["question_id"] for row in questions}
    if set(retrieval) != question_ids:
        raise ValueError("retrieval question IDs do not exactly match supported questions")
    needed = {
        str(item["chunk_id"])
        for values in retrieval.values()
        for item in values
    }
    chunks = load_needed_chunks(args.chunk_metadata, needed)

    blind_rows = []
    gold_rows = []
    summary_rows = []
    for question in questions:
        question_id = question["question_id"]
        selected = retrieval[question_id]
        retrieved_ids = [str(item["chunk_id"]) for item in selected]
        for item in selected:
            chunk = chunks[str(item["chunk_id"])]
            blind_rows.append({
                "question_id": question_id,
                "question_group": question["question_group"],
                "question": question["question"],
                "retrieval_rank": int(item["rank"]),
                "chunk_id": chunk["chunk_id"],
                "ticker": chunk.get("ticker", ""),
                "filing_year": chunk.get("filing_year", ""),
                "accession_number": chunk.get("accession_number", ""),
                "doc_type": chunk.get("doc_type", ""),
                "section_code": chunk.get("section_code", ""),
                "chunk_index": chunk.get("chunk_index", ""),
                "retrieved_chunk_text": chunk.get("chunk_text", ""),
                "relevance_grade": "",
                "directly_supports_question": "",
                "evidence_sufficiency": "",
                "reviewer_notes": "",
            })
        gold_rows.append({
            "question_id": question_id,
            "question_group": question["question_group"],
            "question": question["question"],
            "expected_answer": question.get("expected_answer", ""),
            "original_gold_chunk_ids": question.get("supporting_chunk_ids", ""),
            "original_gold_passages": question.get("supporting_passages", ""),
            "retrieved_chunk_ids": "|".join(retrieved_ids),
            "blind_review_completed": "",
            "original_gold_valid": "",
            "gold_label_complete": "",
            "valid_alternative_chunk_ids": "",
            "annotation_correction_required": "",
            "adjudicator_notes": "",
            "final_review_status": "",
        })
        summary_rows.append({
            "question_id": question_id,
            "question_group": question["question_group"],
            "blind_rows_expected": 5,
            "blind_rows_completed": "",
            "direct_count": "",
            "partial_count": "",
            "irrelevant_count": "",
            "answer_supported_at_5": "",
            "gold_review_completed": "",
            "final_review_status": "",
        })

    args.output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_paths["blind"], BLIND_FIELDS, blind_rows)
    _write_csv(output_paths["gold"], GOLD_FIELDS, gold_rows)
    _write_csv(output_paths["summary"], SUMMARY_FIELDS, summary_rows)
    instructions = """# Semantic Adjudication Instructions

## Phase 1 — blind evidence review

Review `01_blind_evidence_review.csv` using only the question and retrieved chunk.
Do not open the gold sheet until all five rows for a question are graded.

- `relevance_grade`: `DIRECT`, `PARTIAL`, or `IRRELEVANT`.
- `directly_supports_question`: `YES` or `NO`.
- `evidence_sufficiency`: `SUFFICIENT`, `SUPPORTING`, or `INSUFFICIENT`.
- Do not reward a chunk merely because it shares keywords with the question.

## Phase 2 — gold coverage adjudication

After completing Phase 1, open `02_gold_coverage_adjudication.csv`.

- Confirm whether the original gold passage genuinely answers the question.
- Mark whether the gold labels are complete.
- Record retrieved chunks judged `DIRECT` but absent from the original gold as valid alternatives.
- Never add a chunk solely because the retrieval system returned it.

## Completion

Use `03_review_completion_summary.csv` to record counts and final status.
Allowed final status values: `PASS`, `ANNOTATION_FIX_REQUIRED`, `QUESTION_FIX_REQUIRED`, `RETRIEVAL_FAILURE`, `ESCALATE`.
Two reviewers should independently adjudicate disputed or annotation-changing cases.
"""
    output_paths["instructions"].write_text(instructions, encoding="utf-8")
    manifest = {
        "model_id": args.model_id,
        "questions": len(questions),
        "blind_review_rows": len(blind_rows),
        "gold_review_rows": len(gold_rows),
        "unique_retrieved_chunks": len(needed),
        "blind_review_excludes_gold": True,
        "questions_sha256": _sha256(args.questions),
        "retrieval_sha256": _sha256(args.retrieval),
        "chunk_metadata_sha256": _sha256(args.chunk_metadata),
        "outputs": {},
    }
    for name in ("blind", "gold", "summary", "instructions"):
        path = output_paths[name]
        manifest["outputs"][name] = {"path": str(path), "sha256": _sha256(path)}
    output_paths["manifest"].write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
