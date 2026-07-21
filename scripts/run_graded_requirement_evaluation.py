#!/usr/bin/env python3
"""Evaluate ranked retrieval against graded requirement-level qrels."""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def _read(path: Path):
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _mean(values):
    return sum(values) / len(values) if values else None


def _metrics(retrieved, requirement_qrels):
    grade_by_chunk = {}
    direct_by_requirement = {}
    for requirement, judgments in requirement_qrels.items():
        direct_by_requirement[requirement] = {
            chunk_id for chunk_id, grade in judgments.items() if grade == 2
        }
        for chunk_id, grade in judgments.items():
            grade_by_chunk[chunk_id] = max(grade_by_chunk.get(chunk_id, 0), grade)
    grades = [grade_by_chunk.get(chunk_id) for chunk_id in retrieved]
    judged = [grade is not None for grade in grades]
    numeric_grades = [grade if grade is not None else 0 for grade in grades]
    direct_positions = [index for index, grade in enumerate(numeric_grades, 1) if grade == 2]
    covered = sum(
        bool(set(retrieved) & direct_chunks)
        for direct_chunks in direct_by_requirement.values()
    )
    gains = [(2**grade - 1) / math.log2(index + 1) for index, grade in enumerate(numeric_grades, 1)]
    ideal_grades = sorted(grade_by_chunk.values(), reverse=True)[:5]
    ideal = sum((2**grade - 1) / math.log2(index + 1) for index, grade in enumerate(ideal_grades, 1))
    return {
        "direct_hit_at_5": 1.0 if direct_positions else 0.0,
        "mrr_direct_at_5": 1.0 / direct_positions[0] if direct_positions else 0.0,
        "graded_ndcg_at_5": sum(gains) / ideal if ideal else 0.0,
        "requirement_coverage_at_5": covered / len(direct_by_requirement),
        "complete_requirement_coverage_at_5": 1.0 if covered == len(direct_by_requirement) else 0.0,
        "judged_at_5_rate": sum(judged) / len(judged),
        "direct_chunks_at_5": sum(grade == 2 for grade in numeric_grades),
        "partial_chunks_at_5": sum(grade == 1 for grade in numeric_grades),
    }


METRICS = (
    "direct_hit_at_5", "mrr_direct_at_5", "graded_ndcg_at_5",
    "requirement_coverage_at_5", "complete_requirement_coverage_at_5",
    "judged_at_5_rate",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--retrieval", type=Path, required=True)
    parser.add_argument("--qrels", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    outputs = {
        "json": args.output_dir / "graded_requirement_eval.json",
        "markdown": args.output_dir / "graded_requirement_eval.md",
        "per_question": args.output_dir / "graded_requirement_per_question.csv",
    }
    existing = [str(path) for path in outputs.values() if path.exists()]
    if existing:
        raise FileExistsError("refusing to overwrite: " + ", ".join(existing))

    questions = {
        row["question_id"]: row for row in _read(args.questions)
        if row.get("question_group") != "refusal"
    }
    retrieval_rows = _read(args.retrieval)
    qrel_rows = _read(args.qrels)
    retrieval = defaultdict(lambda: defaultdict(list))
    for row in retrieval_rows:
        retrieval[row["model_id"]][row["question_id"]].append(
            (int(row["rank"]), int(row["chunk_id"]))
        )
    qrels = defaultdict(lambda: defaultdict(dict))
    for row in qrel_rows:
        qrels[row["question_id"]][int(row["requirement_index"])][int(row["chunk_id"])] = int(row["relevance_grade"])
    if set(qrels) != set(questions):
        raise ValueError("qrels do not exactly cover supported questions")

    report_models = {}
    all_per_question = []
    for model_id, by_question in sorted(retrieval.items()):
        if set(by_question) != set(questions):
            raise ValueError(f"{model_id}: retrieval does not exactly cover questions")
        per_question = []
        for question_id, question in questions.items():
            ranked = sorted(by_question[question_id])
            if [rank for rank, _ in ranked] != [1, 2, 3, 4, 5]:
                raise ValueError(f"{model_id}/{question_id}: expected ranks 1 through 5")
            chunk_ids = [chunk_id for _, chunk_id in ranked]
            values = _metrics(chunk_ids, qrels[question_id])
            record = {
                "model_id": model_id, "question_id": question_id,
                "question_group": question["question_group"], **values,
            }
            per_question.append(record)
            all_per_question.append(record)
        overall = {name: _mean([row[name] for row in per_question]) for name in METRICS}
        grouped = defaultdict(list)
        for row in per_question:
            grouped[row["question_group"]].append(row)
        by_group = {
            group: {name: _mean([row[name] for row in rows]) for name in METRICS}
            for group, rows in sorted(grouped.items())
        }
        report_models[model_id] = {
            "question_count": len(per_question), "overall": overall,
            "by_group": by_group,
        }

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "questions": len(questions), "judgments": len(qrel_rows),
        "requirements": sum(len(value) for value in qrels.values()),
        "unjudged_policy": "reported separately; unjudged chunks receive zero gain but are not certified irrelevant",
        "models": report_models,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    outputs["json"].write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    fields = list(all_per_question[0])
    with outputs["per_question"].open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(all_per_question)
    lines = [
        "# Graded Requirement Retrieval Evaluation", "",
        f"- Questions: {report['questions']}",
        f"- Requirements: {report['requirements']}",
        f"- Judgments: {report['judgments']}", "",
        "## Overall", "",
        "| Model | Direct Hit@5 | MRR@5 | Graded nDCG@5 | Requirement coverage@5 | Complete coverage@5 | Judged@5 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for model_id, model in report_models.items():
        value = model["overall"]
        lines.append(
            f"| {model_id} | {value['direct_hit_at_5']:.4f} | {value['mrr_direct_at_5']:.4f} | "
            f"{value['graded_ndcg_at_5']:.4f} | {value['requirement_coverage_at_5']:.4f} | "
            f"{value['complete_requirement_coverage_at_5']:.4f} | {value['judged_at_5_rate']:.4f} |"
        )
        lines += ["", f"### {model_id} by group", "",
                  "| Group | Direct Hit@5 | MRR@5 | nDCG@5 | Requirement coverage@5 | Complete coverage@5 | Judged@5 |",
                  "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
        for group, values in model["by_group"].items():
            lines.append(
                f"| {group} | {values['direct_hit_at_5']:.4f} | {values['mrr_direct_at_5']:.4f} | "
                f"{values['graded_ndcg_at_5']:.4f} | {values['requirement_coverage_at_5']:.4f} | "
                f"{values['complete_requirement_coverage_at_5']:.4f} | {values['judged_at_5_rate']:.4f} |"
            )
    outputs["markdown"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
