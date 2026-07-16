"""Retrieval evaluation harness for the 10-K RAG model bake-off.

Consumes the approved benchmark contract plus one ranked-retrieval result set
per candidate embedding model, and produces the gated retrieval metrics the
project requires before a production model may be selected:

    Recall@5, Hit Rate@5, MRR@10, nDCG@10 - overall and per question group.

`src/embedding_model_benchmark.py` measures throughput and resource use only.
It states that the production model "must not be selected from speed or
supplied MTEB scores alone" and that final selection requires the approved
query set, relevance judgments, and these retrieval metrics. This module is
that missing step.

Hard gates are evaluated before scores. A gate that cannot be evaluated -
because chunk metadata is absent or a retrieved chunk is unknown - reports
NOT_EVALUATED and blocks a PASS verdict. It never silently passes.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

QUESTIONS_PATH = Path("data/00_reference/rag_eval_questions.csv")
MULTI_VALUE_SEPARATOR = "|"

# Model-selection rule from Ayse Cetinel's 2026-07-16 email: BGE Base is the
# default winner; BGE Large replaces it only if MRR improves by at least 0.03
# absolute points AND that improvement holds across at least 4 of the 6
# question groups.
DEFAULT_MODEL_ID = "bge_base_en_v1_5"
CHALLENGER_MODEL_ID = "bge_large_en_v1_5"
MRR_IMPROVEMENT_THRESHOLD = 0.03
MINIMUM_IMPROVED_GROUPS = 4

SCORED_GROUPS = (
    "Item_1",
    "Item_1A",
    "Item_7",
    "Item_8",
    "cross_company",
    "time_change",
)
REFUSAL_GROUP = "refusal"

# Depth required to report MRR@10 / nDCG@10 without truncation.
DEEP_K = 10
SHALLOW_K = 5

RETRIEVAL_FIELDS = ("model_id", "question_id", "rank", "chunk_id")
METADATA_KEY = "chunk_id"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def split_multi(value: str) -> list[str]:
    """Split a pipe-separated contract field into trimmed, non-empty parts."""
    if value is None:
        return []
    return [part.strip() for part in str(value).split(MULTI_VALUE_SEPARATOR) if part.strip()]


# Filing text reaches the chunk files with typographic characters and spacing
# artifacts from HTML/PDF extraction that a hand-copied quote will not carry:
# curly quotes and dashes, non-breaking spaces, and a space before punctuation
# ("reputation ."). Fold all of it before comparing, or true quotes read as
# missing evidence.
TYPOGRAPHIC_CHARACTERS = {
    0x2018: "'",
    0x2019: "'",
    0x201A: "'",
    0x201B: "'",
    0x201C: '"',
    0x201D: '"',
    0x201E: '"',
    0x201F: '"',
    0x2013: "-",
    0x2014: "-",
    0x2015: "-",
    0x2212: "-",
    0x00A0: " ",
}
SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([.,;:!?)\]])")

# A contract passage may elide text between two excerpts of the same chunk.
ELLIPSIS = re.compile(r"\.{3,}|…")


def normalise_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.translate(TYPOGRAPHIC_CHARACTERS)
    text = " ".join(text.split())
    text = SPACE_BEFORE_PUNCTUATION.sub(r"\1", text)
    return text.casefold()


def passage_segments(passage: str) -> list[str]:
    """Split a quoted passage on its ellipses into the excerpts it asserts."""
    return [segment for segment in (normalise_text(p) for p in ELLIPSIS.split(passage)) if segment]


def passage_supported_by(passage: str, chunk_text: str) -> bool:
    """True when every excerpt of the passage appears in the chunk, in order.

    An ellipsis in a quotation means "text omitted here", so the excerpts either
    side must both be present and correctly ordered - but need not be adjacent.
    """
    haystack = normalise_text(chunk_text)
    cursor = 0
    for segment in passage_segments(passage):
        found = haystack.find(segment, cursor)
        if found == -1:
            return False
        cursor = found + len(segment)
    return True


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def load_questions(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"{path} contains no questions")
    return rows


def load_retrieval(path: Path) -> dict[str, dict[str, list[str]]]:
    """Load ranked retrieval results.

    Expected columns: model_id, question_id, rank, chunk_id (score optional).
    `rank` is 1-based. Returns model_id -> question_id -> chunk_ids ordered by
    rank.
    """
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [f for f in RETRIEVAL_FIELDS if f not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path} is missing required columns: {', '.join(missing)}")
        rows = list(reader)

    ranked: dict[str, dict[str, list[tuple[int, str]]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        try:
            rank = int(str(row["rank"]).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"non-integer rank {row['rank']!r} in {path}") from exc
        ranked[row["model_id"].strip()][row["question_id"].strip()].append(
            (rank, str(row["chunk_id"]).strip())
        )

    results: dict[str, dict[str, list[str]]] = {}
    for model_id, per_question in ranked.items():
        results[model_id] = {}
        for question_id, entries in per_question.items():
            entries.sort(key=lambda item: item[0])
            ranks = [rank for rank, _ in entries]
            expected = list(range(1, len(entries) + 1))
            if ranks != expected:
                raise ValueError(
                    f"{model_id}/{question_id}: ranks must be contiguous from 1, got {ranks}"
                )
            chunk_ids = [chunk_id for _, chunk_id in entries]
            if len(set(chunk_ids)) != len(chunk_ids):
                raise ValueError(f"{model_id}/{question_id}: duplicate chunk_id in results")
            results[model_id][question_id] = chunk_ids
    return results


def load_chunk_metadata(path: Path) -> dict[str, dict[str, str]]:
    """Load a chunk catalogue exported from the rag_eligible_10k_chunks view.

    Only `chunk_id` is mandatory. Columns that are present enable additional
    gates: doc_type, ticker, filing_year, section_code, and chunk text.
    """
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if METADATA_KEY not in (reader.fieldnames or []):
            raise ValueError(f"{path} is missing the {METADATA_KEY} column")
        catalogue = {str(row[METADATA_KEY]).strip(): row for row in reader}
    if not catalogue:
        raise ValueError(f"{path} contains no chunk metadata rows")
    return catalogue


# --------------------------------------------------------------------------
# Metrics (binary relevance)
# --------------------------------------------------------------------------


def recall_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    return len(relevant.intersection(retrieved[:k])) / len(relevant)


def hit_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    return 1.0 if relevant.intersection(retrieved[:k]) else 0.0


def reciprocal_rank_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    for position, chunk_id in enumerate(retrieved[:k], start=1):
        if chunk_id in relevant:
            return 1.0 / position
    return 0.0


def ndcg_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    gain = sum(
        1.0 / math.log2(position + 1)
        for position, chunk_id in enumerate(retrieved[:k], start=1)
        if chunk_id in relevant
    )
    ideal_hits = min(len(relevant), k)
    ideal = sum(1.0 / math.log2(position + 1) for position in range(1, ideal_hits + 1))
    if ideal == 0.0:
        return 0.0
    return gain / ideal


def mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def score_question(question: dict[str, str], retrieved: list[str]) -> dict[str, Any]:
    relevant = set(split_multi(question["supporting_chunk_ids"]))
    return {
        "question_id": question["question_id"],
        "question_group": question["question_group"],
        "relevant_chunk_count": len(relevant),
        "retrieved_depth": len(retrieved),
        "recall_at_5": recall_at_k(retrieved, relevant, SHALLOW_K),
        "hit_at_5": hit_at_k(retrieved, relevant, SHALLOW_K),
        "mrr_at_10": reciprocal_rank_at_k(retrieved, relevant, DEEP_K),
        "ndcg_at_10": ndcg_at_k(retrieved, relevant, DEEP_K),
        "mrr_at_5": reciprocal_rank_at_k(retrieved, relevant, SHALLOW_K),
        "ndcg_at_5": ndcg_at_k(retrieved, relevant, SHALLOW_K),
        "retrieved_chunk_ids": list(retrieved),
    }


METRIC_NAMES = ("recall_at_5", "hit_at_5", "mrr_at_10", "ndcg_at_10", "mrr_at_5", "ndcg_at_5")


def aggregate(per_question: list[dict[str, Any]]) -> dict[str, Any]:
    overall = {name: mean([row[name] for row in per_question]) for name in METRIC_NAMES}
    by_group: dict[str, dict[str, float | None]] = {}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in per_question:
        grouped[row["question_group"]].append(row)
    for group, rows in sorted(grouped.items()):
        by_group[group] = {name: mean([row[name] for row in rows]) for name in METRIC_NAMES}
    return {
        "question_count": len(per_question),
        "overall": overall,
        "by_group": by_group,
    }


# --------------------------------------------------------------------------
# Hard gates
# --------------------------------------------------------------------------

PASS = "PASS"
FAIL = "FAIL"
NOT_EVALUATED = "NOT_EVALUATED"


def gate_wrong_doc_type(
    questions: dict[str, dict[str, str]],
    retrieval: dict[str, list[str]],
    catalogue: dict[str, dict[str, str]] | None,
) -> dict[str, Any]:
    """wrong_doc_type_rate must be 0: any retrieved chunk from the wrong
    document type eliminates the model regardless of score."""
    if catalogue is None:
        return {
            "status": NOT_EVALUATED,
            "reason": "no chunk metadata supplied; cannot certify wrong_doc_type_rate = 0",
        }
    sample = next(iter(catalogue.values()))
    if "doc_type" not in sample:
        return {
            "status": NOT_EVALUATED,
            "reason": "chunk metadata has no doc_type column",
        }

    inspected = 0
    violations: list[dict[str, str]] = []
    unresolved: list[str] = []
    for question_id, retrieved in sorted(retrieval.items()):
        question = questions.get(question_id)
        if question is None:
            continue
        # Multi-source rows carry one doc type per source ("10-K|10-K"), so the
        # field is a set of acceptable types, not a single string.
        required = split_multi(question.get("required_doc_type", ""))
        if not required:
            continue
        allowed = {value.casefold() for value in required}
        for chunk_id in retrieved:
            record = catalogue.get(chunk_id)
            if record is None:
                unresolved.append(chunk_id)
                continue
            inspected += 1
            actual = (record.get("doc_type") or "").strip()
            if actual.casefold() not in allowed:
                violations.append(
                    {
                        "question_id": question_id,
                        "chunk_id": chunk_id,
                        "expected_doc_type": MULTI_VALUE_SEPARATOR.join(sorted(set(required))),
                        "actual_doc_type": actual,
                    }
                )

    if unresolved:
        return {
            "status": NOT_EVALUATED,
            "reason": (
                f"{len(unresolved)} retrieved chunk(s) absent from the metadata catalogue; "
                "doc type cannot be verified"
            ),
            "unresolved_chunk_ids": sorted(set(unresolved))[:20],
            "inspected_chunks": inspected,
        }

    rate = (len(violations) / inspected) if inspected else 0.0
    return {
        "status": PASS if not violations else FAIL,
        "wrong_doc_type_rate": rate,
        "inspected_chunks": inspected,
        "violations": violations[:20],
        "violation_count": len(violations),
    }


def gate_gold_integrity(
    questions: list[dict[str, str]],
    catalogue: dict[str, dict[str, str]] | None,
) -> dict[str, Any]:
    """Interpretation of "no company or year leakage": every supporting chunk
    named by the contract must actually belong to the ticker, filing year and
    section the contract claims for it.

    This is the check that catches a gold row pointing at a chunk from the
    wrong company or year - the defect class that has previously reached
    review in this project.
    """
    if catalogue is None:
        return {"status": NOT_EVALUATED, "reason": "no chunk metadata supplied"}
    sample = next(iter(catalogue.values()))
    available = [f for f in ("ticker", "filing_year", "section_code") if f in sample]
    if not available:
        return {
            "status": NOT_EVALUATED,
            "reason": "chunk metadata has none of ticker/filing_year/section_code",
        }

    contract_fields = {
        "ticker": "supporting_tickers",
        "filing_year": "supporting_filing_years",
        "section_code": "supporting_section_codes",
    }

    mismatches: list[dict[str, str]] = []
    misalignments: list[dict[str, Any]] = []
    unresolved: list[str] = []
    checked = 0
    for question in questions:
        if question["question_group"] == REFUSAL_GROUP:
            continue
        chunk_ids = split_multi(question["supporting_chunk_ids"])
        if not chunk_ids:
            continue
        records = []
        for chunk_id in chunk_ids:
            record = catalogue.get(chunk_id)
            if record is None:
                unresolved.append(chunk_id)
            records.append(record)

        for meta_field in available:
            claimed = split_multi(question.get(contract_fields[meta_field], ""))
            # These fields are positional: value i describes chunk i. A length
            # mismatch means the row cannot be checked at all, which is a
            # contract defect - report it rather than skipping the comparison.
            if len(claimed) != len(chunk_ids):
                misalignments.append(
                    {
                        "question_id": question["question_id"],
                        "field": contract_fields[meta_field],
                        "chunk_count": len(chunk_ids),
                        "value_count": len(claimed),
                    }
                )
                continue
            for index, chunk_id in enumerate(chunk_ids):
                record = records[index]
                if record is None:
                    continue
                checked += 1
                actual = (record.get(meta_field) or "").strip()
                if actual.casefold() != claimed[index].casefold():
                    mismatches.append(
                        {
                            "question_id": question["question_id"],
                            "chunk_id": chunk_id,
                            "field": meta_field,
                            "claimed": claimed[index],
                            "actual": actual,
                        }
                    )

    if unresolved:
        return {
            "status": NOT_EVALUATED,
            "reason": (
                f"{len(unresolved)} supporting chunk(s) absent from the metadata catalogue"
            ),
            "unresolved_chunk_ids": sorted(set(unresolved))[:20],
        }

    return {
        "status": PASS if not (mismatches or misalignments) else FAIL,
        "fields_checked": checked,
        "mismatches": mismatches[:20],
        "mismatch_count": len(mismatches),
        "misalignments": misalignments[:20],
        "misalignment_count": len(misalignments),
    }


def gate_evidence_present(
    questions: list[dict[str, str]],
    catalogue: dict[str, dict[str, str]] | None,
    text_field: str,
) -> dict[str, Any]:
    """Aziz's assigned validation: the supporting passage must genuinely appear
    in the chunk the contract cites as its evidence."""
    if catalogue is None:
        return {"status": NOT_EVALUATED, "reason": "no chunk metadata supplied"}
    sample = next(iter(catalogue.values()))
    if text_field not in sample:
        return {
            "status": NOT_EVALUATED,
            "reason": f"chunk metadata has no {text_field} column; passages cannot be verified",
        }

    missing: list[dict[str, str]] = []
    misalignments: list[dict[str, Any]] = []
    checked = 0
    for question in questions:
        if question["question_group"] == REFUSAL_GROUP:
            continue
        chunk_ids = split_multi(question["supporting_chunk_ids"])
        if not chunk_ids:
            continue
        passages = split_multi(question.get("supporting_passages", ""))
        # Passage i is the evidence for chunk i. If the counts disagree the row
        # cannot be verified, which is itself a defect worth reporting.
        if len(passages) != len(chunk_ids):
            misalignments.append(
                {
                    "question_id": question["question_id"],
                    "chunk_count": len(chunk_ids),
                    "passage_count": len(passages),
                }
            )
            continue
        for index, chunk_id in enumerate(chunk_ids):
            record = catalogue.get(chunk_id)
            if record is None:
                continue
            checked += 1
            if not passage_supported_by(passages[index], record.get(text_field, "")):
                missing.append({"question_id": question["question_id"], "chunk_id": chunk_id})

    if not checked and not misalignments:
        return {"status": NOT_EVALUATED, "reason": "no passage/chunk pairs available to check"}
    return {
        "status": PASS if not (missing or misalignments) else FAIL,
        "passages_checked": checked,
        "passages_not_found": missing[:20],
        "not_found_count": len(missing),
        "misalignments": misalignments[:20],
        "misalignment_count": len(misalignments),
    }


# --------------------------------------------------------------------------
# Decision rule
# --------------------------------------------------------------------------


def apply_decision_rule(
    models: dict[str, dict[str, Any]],
    decision_metric: str,
    per_group_threshold: float,
) -> dict[str, Any]:
    base = models.get(DEFAULT_MODEL_ID)
    challenger = models.get(CHALLENGER_MODEL_ID)
    if base is None or challenger is None:
        return {
            "status": NOT_EVALUATED,
            "reason": (
                f"decision rule needs both {DEFAULT_MODEL_ID} and {CHALLENGER_MODEL_ID}; "
                f"got {sorted(models)}"
            ),
        }
    if base["gates"]["overall"] != PASS or challenger["gates"]["overall"] != PASS:
        return {
            "status": NOT_EVALUATED,
            "reason": "hard gates must pass for both models before scores are compared",
            "base_gate_status": base["gates"]["overall"],
            "challenger_gate_status": challenger["gates"]["overall"],
        }

    base_overall = base["scores"]["overall"][decision_metric]
    challenger_overall = challenger["scores"]["overall"][decision_metric]
    overall_delta = challenger_overall - base_overall
    condition_overall = overall_delta >= MRR_IMPROVEMENT_THRESHOLD

    per_group: dict[str, Any] = {}
    improved_groups = []
    for group in SCORED_GROUPS:
        base_value = base["scores"]["by_group"].get(group, {}).get(decision_metric)
        challenger_value = challenger["scores"]["by_group"].get(group, {}).get(decision_metric)
        if base_value is None or challenger_value is None:
            per_group[group] = {"delta": None, "improved": None}
            continue
        delta = challenger_value - base_value
        improved = delta >= per_group_threshold
        per_group[group] = {
            "base": base_value,
            "challenger": challenger_value,
            "delta": delta,
            "improved": improved,
        }
        if improved:
            improved_groups.append(group)

    condition_groups = len(improved_groups) >= MINIMUM_IMPROVED_GROUPS
    challenger_wins = condition_overall and condition_groups

    return {
        "status": "DECIDED",
        "decision_metric": decision_metric,
        "winner": CHALLENGER_MODEL_ID if challenger_wins else DEFAULT_MODEL_ID,
        "default_model": DEFAULT_MODEL_ID,
        "challenger_model": CHALLENGER_MODEL_ID,
        "base_overall": base_overall,
        "challenger_overall": challenger_overall,
        "overall_delta": overall_delta,
        "condition_overall_improvement": {
            "required": f">= {MRR_IMPROVEMENT_THRESHOLD}",
            "actual": overall_delta,
            "met": condition_overall,
        },
        "condition_group_breadth": {
            "required": f">= {MINIMUM_IMPROVED_GROUPS} of {len(SCORED_GROUPS)} groups",
            "actual": len(improved_groups),
            "improved_groups": improved_groups,
            "per_group_threshold": per_group_threshold,
            "met": condition_groups,
        },
        "per_group": per_group,
    }


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------


def evaluate(
    questions: list[dict[str, str]],
    retrieval: dict[str, dict[str, list[str]]],
    catalogue: dict[str, dict[str, str]] | None,
    text_field: str,
) -> dict[str, Any]:
    by_id = {row["question_id"]: row for row in questions}
    scored_questions = [
        row
        for row in questions
        if row["question_group"] != REFUSAL_GROUP and split_multi(row["supporting_chunk_ids"])
    ]
    refusal_questions = [row for row in questions if row["question_group"] == REFUSAL_GROUP]

    warnings: list[str] = []
    gold_gate = gate_gold_integrity(questions, catalogue)
    evidence_gate = gate_evidence_present(questions, catalogue, text_field)

    models: dict[str, dict[str, Any]] = {}
    for model_id, per_question_results in sorted(retrieval.items()):
        missing = [q["question_id"] for q in scored_questions if q["question_id"] not in per_question_results]
        if missing:
            warnings.append(
                f"{model_id}: no retrieval results for {len(missing)} scored question(s): "
                f"{', '.join(missing[:5])}"
            )

        rows = [
            score_question(q, per_question_results[q["question_id"]])
            for q in scored_questions
            if q["question_id"] in per_question_results
        ]
        if not rows:
            warnings.append(f"{model_id}: no scorable questions; skipped")
            continue

        depths = {row["retrieved_depth"] for row in rows}
        min_depth = min(depths)
        truncated = min_depth < DEEP_K
        if truncated:
            warnings.append(
                f"{model_id}: retrieval depth is {min_depth}; mrr_at_10 and ndcg_at_10 are "
                f"computed over {min_depth} results and are NOT true @10 figures"
            )

        doc_type_gate = gate_wrong_doc_type(by_id, per_question_results, catalogue)
        gate_statuses = [doc_type_gate["status"], gold_gate["status"], evidence_gate["status"]]
        if FAIL in gate_statuses:
            overall_gate = FAIL
        elif NOT_EVALUATED in gate_statuses:
            overall_gate = NOT_EVALUATED
        else:
            overall_gate = PASS

        models[model_id] = {
            "model_id": model_id,
            "retrieval_depth_min": min_depth,
            "retrieval_depth_max": max(depths),
            "deep_metrics_truncated": truncated,
            "scores": aggregate(rows),
            "gates": {
                "overall": overall_gate,
                "wrong_doc_type": doc_type_gate,
                "gold_integrity": gold_gate,
                "evidence_present": evidence_gate,
            },
            "per_question": rows,
        }

    return {
        "generated_at": utc_now(),
        "scored_question_count": len(scored_questions),
        "refusal_question_count": len(refusal_questions),
        "refusal_note": (
            "Refusal questions have no supporting chunks and are excluded from retrieval "
            "scoring. They test answer-generation behaviour, not the retriever."
        ),
        "models": models,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def format_value(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.4f}"


def write_markdown(report: dict[str, Any], decision: dict[str, Any], path: Path) -> None:
    lines: list[str] = [
        "# 10-K RAG Retrieval Evaluation",
        "",
        f"Generated: {report['generated_at']}",
        "",
        f"- Scored questions: {report['scored_question_count']}",
        f"- Refusal questions (not retrieval-scored): {report['refusal_question_count']}",
        "",
    ]

    if report["warnings"]:
        lines += ["## Warnings", ""]
        lines += [f"- {warning}" for warning in report["warnings"]]
        lines.append("")

    lines += ["## Hard gates", "", "| Model | Overall | wrong_doc_type | gold_integrity | evidence_present |", "| --- | --- | --- | --- | --- |"]
    for model_id, model in report["models"].items():
        gates = model["gates"]
        lines.append(
            f"| {model_id} | {gates['overall']} | {gates['wrong_doc_type']['status']} | "
            f"{gates['gold_integrity']['status']} | {gates['evidence_present']['status']} |"
        )
    lines.append("")

    lines += ["## Overall scores", "", "| Model | Recall@5 | Hit@5 | MRR@10 | nDCG@10 |", "| --- | --- | --- | --- | --- |"]
    for model_id, model in report["models"].items():
        overall = model["scores"]["overall"]
        lines.append(
            f"| {model_id} | {format_value(overall['recall_at_5'])} | "
            f"{format_value(overall['hit_at_5'])} | {format_value(overall['mrr_at_10'])} | "
            f"{format_value(overall['ndcg_at_10'])} |"
        )
    lines.append("")

    for model_id, model in report["models"].items():
        lines += [
            f"### {model_id} by question group",
            "",
            "| Group | Recall@5 | Hit@5 | MRR@10 | nDCG@10 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for group, values in model["scores"]["by_group"].items():
            lines.append(
                f"| {group} | {format_value(values['recall_at_5'])} | "
                f"{format_value(values['hit_at_5'])} | {format_value(values['mrr_at_10'])} | "
                f"{format_value(values['ndcg_at_10'])} |"
            )
        lines.append("")

    lines += ["## Model selection decision", ""]
    if decision["status"] != "DECIDED":
        lines += [f"Status: {decision['status']} - {decision['reason']}", ""]
    else:
        lines += [
            f"Decision metric: `{decision['decision_metric']}`",
            "",
            f"- {DEFAULT_MODEL_ID}: {format_value(decision['base_overall'])}",
            f"- {CHALLENGER_MODEL_ID}: {format_value(decision['challenger_overall'])}",
            f"- Delta: {format_value(decision['overall_delta'])}",
            "",
            f"Condition 1 - improvement >= {MRR_IMPROVEMENT_THRESHOLD}: "
            f"{decision['condition_overall_improvement']['met']}",
            f"Condition 2 - holds in >= {MINIMUM_IMPROVED_GROUPS} of {len(SCORED_GROUPS)} groups: "
            f"{decision['condition_group_breadth']['met']} "
            f"({decision['condition_group_breadth']['actual']} group(s))",
            "",
            f"**Winner: {decision['winner']}**",
            "",
        ]

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_per_question_csv(report: dict[str, Any], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(
            [
                "model_id",
                "question_id",
                "question_group",
                "relevant_chunk_count",
                "retrieved_depth",
                "recall_at_5",
                "hit_at_5",
                "mrr_at_10",
                "ndcg_at_10",
                "retrieved_chunk_ids",
            ]
        )
        for model_id, model in report["models"].items():
            for row in model["per_question"]:
                writer.writerow(
                    [
                        model_id,
                        row["question_id"],
                        row["question_group"],
                        row["relevant_chunk_count"],
                        row["retrieved_depth"],
                        f"{row['recall_at_5']:.6f}",
                        f"{row['hit_at_5']:.6f}",
                        f"{row['mrr_at_10']:.6f}",
                        f"{row['ndcg_at_10']:.6f}",
                        MULTI_VALUE_SEPARATOR.join(row["retrieved_chunk_ids"]),
                    ]
                )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--questions", type=Path, default=QUESTIONS_PATH)
    parser.add_argument(
        "--retrieval",
        type=Path,
        required=True,
        help="CSV of ranked results: model_id, question_id, rank, chunk_id",
    )
    parser.add_argument(
        "--chunk-metadata",
        type=Path,
        default=None,
        help="CSV export of rag_eligible_10k_chunks; required for the hard gates",
    )
    parser.add_argument(
        "--text-field",
        default="chunk_text",
        help=(
            "metadata column holding chunk text, for passage verification. "
            "Defaults to chunk_text because the contract's supporting passages were "
            "quoted from the chunk files; embedding_text is a cleaned derivative and "
            "may not contain the passage verbatim"
        ),
    )
    parser.add_argument(
        "--decision-metric",
        default="mrr_at_10",
        choices=["mrr_at_10", "mrr_at_5"],
        help="metric used for the BGE Base vs BGE Large rule",
    )
    parser.add_argument(
        "--per-group-threshold",
        type=float,
        default=MRR_IMPROVEMENT_THRESHOLD,
        help=(
            "improvement a group must show to count toward the 4-of-6 condition; "
            "defaults to the same 0.03 as the overall condition"
        ),
    )
    parser.add_argument("--out", type=Path, default=Path("reports/rag_eval"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    questions = load_questions(args.questions)
    retrieval = load_retrieval(args.retrieval)
    catalogue = load_chunk_metadata(args.chunk_metadata) if args.chunk_metadata else None

    report = evaluate(questions, retrieval, catalogue, args.text_field)
    decision = apply_decision_rule(report["models"], args.decision_metric, args.per_group_threshold)
    report["decision"] = decision

    args.out.mkdir(parents=True, exist_ok=True)
    json_path = args.out / "rag_eval_report.json"
    md_path = args.out / "rag_eval_report.md"
    csv_path = args.out / "rag_eval_per_question.csv"

    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_markdown(report, decision, md_path)
    write_per_question_csv(report, csv_path)

    for warning in report["warnings"]:
        print(f"WARNING: {warning}", file=sys.stderr)

    for model_id, model in report["models"].items():
        print(f"{model_id}: gates={model['gates']['overall']}")
    print(f"decision: {decision.get('winner', decision['status'])}")
    print(f"wrote {json_path}, {md_path}, {csv_path}")

    # Exit 0 only when every model produced a fully gated result. A run whose
    # gates could not be evaluated has certified nothing, and must not read as
    # success to a caller that only checks the exit code.
    if not report["models"]:
        return 1
    return 0 if all(m["gates"]["overall"] == PASS for m in report["models"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
