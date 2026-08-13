#!/usr/bin/env python3
"""Build rank-blinded requirement-level candidate pools for benchmark qrels."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

DEFAULT_MODEL_ID = "bge_base_fixed__section_adaptive_v1"
BLIND_FIELDS = (
    "candidate_code", "question_id", "question_group", "question",
    "requirement_index", "requirement_claim", "required_ticker",
    "required_filing_year", "required_section", "candidate_ticker",
    "candidate_filing_year", "candidate_section", "candidate_text",
    "relevance_grade", "direct_evidence", "reviewer_id", "reviewer_notes",
)
SUMMARY_FIELDS = (
    "question_id", "question_group", "requirement_index", "requirement_claim",
    "required_ticker", "required_filing_year", "required_section",
    "candidate_count", "reviewed_count", "direct_count", "partial_count",
    "irrelevant_count", "uncertain_count", "direct_evidence_found",
    "expansion_required", "adjudication_status",
)
PROVENANCE_FIELDS = (
    "candidate_code", "question_id", "requirement_index", "chunk_id",
    "is_original_gold", "is_selected_winner_top5", "original_rank",
    "focused_rank", "profile_rank", "pool_depth",
)
GOLD_FIELDS = (
    "question_id", "requirement_index", "chunk_id", "ticker", "filing_year",
    "section_code", "original_gold_passage", "gold_valid_after_review",
    "gold_complete_after_review", "accepted_alternative_candidate_codes",
    "adjudicator_notes",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _parts(value: str) -> list[str]:
    return [part.strip() for part in str(value or "").split("|") if part.strip()]


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def candidate_code(question_id: str, requirement_index: int, chunk_id: str) -> str:
    payload = f"pooled-qrels-v1|{question_id}|{requirement_index}|{chunk_id}"
    return "CAND-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def load_chunk_metadata(path: Path, needed: set[str]) -> dict[str, dict[str, str]]:
    csv.field_size_limit(sys.maxsize)
    found = {}
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            chunk_id = str(row.get("chunk_id", "")).strip()
            if chunk_id in needed:
                found[chunk_id] = row
                if len(found) == len(needed):
                    break
    missing = sorted(needed - set(found))
    if missing:
        raise ValueError(f"metadata missing {len(missing)} pooled chunks: {missing[:10]}")
    return found


def route_matches_chunk(route: dict[str, Any], chunk: dict[str, str]) -> bool:
    subquery = route["subquery"]
    return (
        str(subquery["ticker"]) == str(chunk.get("ticker", ""))
        and int(subquery["filing_year"]) == int(chunk.get("filing_year", 0))
        and str(subquery["accession_number"]) == str(chunk.get("accession_number", ""))
        and str(subquery["section_code"]) == str(chunk.get("section_code", ""))
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--multiview-details", type=Path, required=True)
    parser.add_argument("--selected-retrieval", type=Path, required=True)
    parser.add_argument("--chunk-metadata", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--selected-model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--pool-depth-per-view", type=int, default=5)
    parser.add_argument("--expected-questions", type=int, default=50)
    parser.add_argument(
        "--requirements-filter", type=Path,
        help="optional CSV with question_id,requirement_index for stage-2 expansion",
    )
    args = parser.parse_args()
    if not 1 <= args.pool_depth_per_view <= 20:
        raise ValueError("pool depth per view must be between 1 and 20")

    outputs = {
        "blind": args.output_dir / "01_BLIND_pooled_requirement_candidates.csv",
        "summary": args.output_dir / "02_requirement_review_summary.csv",
        "provenance": args.output_dir / "PRIVATE_candidate_provenance_do_not_share.csv",
        "gold": args.output_dir / "POST_REVIEW_original_gold_reference.csv",
        "instructions": args.output_dir / "POOLED_ADJUDICATION_INSTRUCTIONS.md",
        "manifest": args.output_dir / "pooled_adjudication_manifest.json",
    }
    existing = [str(path) for path in outputs.values() if path.exists()]
    if existing:
        raise FileExistsError("refusing to overwrite: " + ", ".join(existing))

    questions = {
        row["question_id"]: row for row in _read_csv(args.questions)
        if row.get("question_group") != "refusal"
    }
    if len(questions) != args.expected_questions:
        raise ValueError(f"expected {args.expected_questions} questions; found {len(questions)}")
    allowed_requirements = None
    if args.requirements_filter:
        filter_rows = _read_csv(args.requirements_filter)
        allowed_requirements = {
            (row["question_id"], int(row["requirement_index"]))
            for row in filter_rows
        }
        if not allowed_requirements:
            raise ValueError("requirements filter is empty")
    details = json.loads(args.multiview_details.read_text(encoding="utf-8"))
    detail_questions = {row["question_id"]: row for row in details["questions"]}
    if set(detail_questions) != set(questions):
        raise ValueError("multiview details do not exactly match question IDs")

    selected_rows = [
        row for row in _read_csv(args.selected_retrieval)
        if row.get("model_id") == args.selected_model_id
    ]
    selected_by_question: dict[str, list[dict[str, str]]] = {}
    for row in selected_rows:
        selected_by_question.setdefault(row["question_id"], []).append(row)
    if set(selected_by_question) != set(questions):
        raise ValueError("selected retrieval does not exactly match question IDs")

    prepared = []
    needed: set[str] = set()
    for question_id, question in questions.items():
        strategy = detail_questions[question_id]["strategies"]["all_views_requirement_aware"]
        routes = strategy["routes"]
        route_candidates = []
        for requirement_index, route in enumerate(routes, start=1):
            if allowed_requirements is not None and (
                question_id, requirement_index
            ) not in allowed_requirements:
                continue
            origins: dict[str, dict[str, Any]] = {}
            for view in ("original", "focused", "profile"):
                ids = route["view_candidate_ids"].get(view, [])[:args.pool_depth_per_view]
                for rank, raw_id in enumerate(ids, start=1):
                    chunk_id = str(raw_id)
                    origins.setdefault(chunk_id, {})[f"{view}_rank"] = rank
                    needed.add(chunk_id)
            route_candidates.append({
                "requirement_index": requirement_index,
                "route": route,
                "origins": origins,
            })
        if not route_candidates:
            continue
        gold_ids = _parts(question.get("supporting_chunk_ids", ""))
        needed.update(gold_ids)
        winner_ids = [str(row["chunk_id"]) for row in selected_by_question[question_id]]
        needed.update(winner_ids)
        prepared.append({
            "question_id": question_id, "question": question,
            "route_candidates": route_candidates, "gold_ids": gold_ids,
            "winner_ids": winner_ids,
        })

    metadata = load_chunk_metadata(args.chunk_metadata, needed)
    blind_rows = []
    summary_rows = []
    provenance_rows = []
    gold_rows = []
    for item in prepared:
        question_id = item["question_id"]
        question = item["question"]
        for route_item in item["route_candidates"]:
            requirement_index = route_item["requirement_index"]
            route = route_item["route"]
            origins = route_item["origins"]
            for chunk_id in item["winner_ids"]:
                if route_matches_chunk(route, metadata[chunk_id]):
                    origins.setdefault(chunk_id, {})["winner_rank"] = next(
                        int(row["rank"]) for row in selected_by_question[question_id]
                        if str(row["chunk_id"]) == chunk_id
                    )
            matching_gold = []
            for gold_id in item["gold_ids"]:
                if route_matches_chunk(route, metadata[gold_id]):
                    origins.setdefault(gold_id, {})["is_original_gold"] = True
                    matching_gold.append(gold_id)
            subquery = route["subquery"]
            for chunk_id, source in sorted(origins.items(), key=lambda pair: int(pair[0])):
                chunk = metadata[chunk_id]
                code = candidate_code(question_id, requirement_index, chunk_id)
                blind_rows.append({
                    "candidate_code": code,
                    "question_id": question_id,
                    "question_group": question["question_group"],
                    "question": question["question"],
                    "requirement_index": requirement_index,
                    "requirement_claim": subquery["claim_key"],
                    "required_ticker": subquery["ticker"],
                    "required_filing_year": subquery["filing_year"],
                    "required_section": subquery["section_code"],
                    "candidate_ticker": chunk.get("ticker", ""),
                    "candidate_filing_year": chunk.get("filing_year", ""),
                    "candidate_section": chunk.get("section_code", ""),
                    "candidate_text": chunk.get("chunk_text", ""),
                    "relevance_grade": "", "direct_evidence": "",
                    "reviewer_id": "", "reviewer_notes": "",
                })
                provenance_rows.append({
                    "candidate_code": code, "question_id": question_id,
                    "requirement_index": requirement_index, "chunk_id": chunk_id,
                    "is_original_gold": "YES" if source.get("is_original_gold") else "NO",
                    "is_selected_winner_top5": "YES" if source.get("winner_rank") else "NO",
                    "original_rank": source.get("original_rank", ""),
                    "focused_rank": source.get("focused_rank", ""),
                    "profile_rank": source.get("profile_rank", ""),
                    "pool_depth": args.pool_depth_per_view,
                })
            summary_rows.append({
                "question_id": question_id, "question_group": question["question_group"],
                "requirement_index": requirement_index,
                "requirement_claim": subquery["claim_key"],
                "required_ticker": subquery["ticker"],
                "required_filing_year": subquery["filing_year"],
                "required_section": subquery["section_code"],
                "candidate_count": len(origins), "reviewed_count": "",
                "direct_count": "", "partial_count": "", "irrelevant_count": "",
                "uncertain_count": "", "direct_evidence_found": "",
                "expansion_required": "", "adjudication_status": "",
            })
            gold_passages = _parts(question.get("supporting_passages", ""))
            for gold_id in matching_gold:
                gold_position = item["gold_ids"].index(gold_id)
                chunk = metadata[gold_id]
                gold_rows.append({
                    "question_id": question_id, "requirement_index": requirement_index,
                    "chunk_id": gold_id, "ticker": chunk.get("ticker", ""),
                    "filing_year": chunk.get("filing_year", ""),
                    "section_code": chunk.get("section_code", ""),
                    "original_gold_passage": gold_passages[gold_position] if gold_position < len(gold_passages) else "",
                    "gold_valid_after_review": "", "gold_complete_after_review": "",
                    "accepted_alternative_candidate_codes": "", "adjudicator_notes": "",
                })

    # Stable hash ordering hides system rank and gold provenance from reviewers.
    blind_rows.sort(key=lambda row: hashlib.sha256(row["candidate_code"].encode()).hexdigest())
    args.output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(outputs["blind"], BLIND_FIELDS, blind_rows)
    _write_csv(outputs["summary"], SUMMARY_FIELDS, summary_rows)
    _write_csv(outputs["provenance"], PROVENANCE_FIELDS, provenance_rows)
    _write_csv(outputs["gold"], GOLD_FIELDS, gold_rows)
    instructions = """# Pooled Requirement Adjudication

1. Give reviewers only `01_BLIND_pooled_requirement_candidates.csv`.
2. Do not share the PRIVATE provenance or POST_REVIEW gold files until blind grading is complete.
3. Grade each candidate: `2` direct, `1` partial, `0` irrelevant, `U` uncertain.
4. A keyword match is not sufficient; the passage must substantively support the stated requirement.
5. Summarize every requirement in `02_requirement_review_summary.csv`.
6. If no grade-2 evidence is found, set `expansion_required=YES`; generate a separate depth-20 pool only for those requirements.
7. Two reviewers must resolve `U`, disagreements, and every proposed gold-label change.
8. Only after blind review, use the PRIVATE and POST_REVIEW files to construct multi-positive graded qrels.
"""
    outputs["instructions"].write_text(instructions, encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "questions": len(questions), "requirements": len(summary_rows),
        "blind_candidates": len(blind_rows),
        "pool_depth_per_view": args.pool_depth_per_view,
        "selected_model_id": args.selected_model_id,
        "rank_blinded": True, "gold_status_blinded": True,
        "system_identity_blinded": True,
        "questions_sha256": _sha256(args.questions),
        "multiview_details_sha256": _sha256(args.multiview_details),
        "selected_retrieval_sha256": _sha256(args.selected_retrieval),
        "requirements_filter_sha256": (
            _sha256(args.requirements_filter) if args.requirements_filter else None
        ),
        "outputs": {},
    }
    for name in ("blind", "summary", "provenance", "gold", "instructions"):
        manifest["outputs"][name] = {
            "path": str(outputs[name]), "sha256": _sha256(outputs[name])
        }
    outputs["manifest"].write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
