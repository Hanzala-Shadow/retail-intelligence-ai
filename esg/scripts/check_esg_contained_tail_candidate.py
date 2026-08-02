#!/usr/bin/env python3
"""Compare a contained-tail candidate against the subsection-context candidate.

Answers the four questions that decide whether the candidate is safe to carry
forward: did the contained chunks go away, did anything else move, did the
manual-review holds land, and do the subsection-context guarantees still hold.

Writes a report under reports/. Touches no live corpus, manifest, or index.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "esg" / "src"))

import esg_chunker  # noqa: E402

csv.field_size_limit(10**9)

REGRESSION_HEADINGS = {
    "DELL": "BETTERING THE LIVES OF PEOPLE IN OUR SUPPLY CHAIN",
    "LOW": "CLIMATE CHANGE, ENERGY AND EMISSIONS",
    "SHOO": "BOARD OF DIRECTORS / RISK MANAGEMENT / CSR PROGRAM GOVERNANCE",
    "SVV": "NONPROFIT PARTNER SPOTLIGHTS",
    "ORLY": "Feeding Our Communities Partners",
    "TPR": "SOCIAL IMPACT COUNCIL",
}

NOISE_TERMS = [
    "Energy efficiency proceeds were allocated to Eligible Projects",
    "CATEGORY / STATE EMPLOYEES BRANDS",
    "FY24 DECKERS FOOTWEAR ENERGY USAGE BY MATERIAL CATEGORY GATE BREAKDOWN",
    "INTRODUCTION CLIMATE ACTION CIRCULAR ECONOMY",
]


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def by_section(rows: list[dict[str, str]]) -> dict[tuple[str, str], list[dict]]:
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        if not row.get("source_start_char"):
            continue
        grouped[(row["pdf_stem"], row["section_instance_id"])].append(row)
    for spans in grouped.values():
        spans.sort(key=lambda row: (int(row["source_start_char"]), int(row["source_end_char"])))
    return grouped


def containment(rows: list[dict[str, str]]) -> tuple[int, int, list[dict]]:
    subset = superset = 0
    examples: list[dict] = []
    for key, section_rows in by_section(rows).items():
        for previous, current in zip(section_rows, section_rows[1:]):
            before = (int(previous["source_start_char"]), int(previous["source_end_char"]))
            after = (int(current["source_start_char"]), int(current["source_end_char"]))
            if after[0] >= before[0] and after[1] <= before[1]:
                subset += 1
                if len(examples) < 10:
                    examples.append(
                        {
                            "pdf_stem": key[0],
                            "section_instance_id": key[1],
                            "spans": [list(before), list(after)],
                            "eligible": current["include_in_esg_index"],
                        }
                    )
            if after[0] <= before[0] and after[1] >= before[1]:
                superset += 1
    return subset, superset, examples


def tiling_defects(rows: list[dict[str, str]]) -> dict[str, int]:
    gaps = 0
    unordered = 0
    for section_rows in by_section(rows).values():
        for previous, current in zip(section_rows, section_rows[1:]):
            if int(current["source_start_char"]) > int(previous["source_end_char"]):
                gaps += 1
            if int(current["source_end_char"]) <= int(previous["source_end_char"]):
                unordered += 1
    return {"chunk_gaps": gaps, "non_advancing_boundaries": unordered}


def eligible_text_digest(rows: list[dict[str, str]]) -> dict[str, str]:
    """chunk_text hashes of index-eligible chunks, keyed by chunk id."""
    return {
        row["chunk_id"]: row["chunk_text_sha256"]
        for row in rows
        if row["include_in_esg_index"] == "true"
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-chunks", type=Path, required=True)
    parser.add_argument("--candidate-chunks", type=Path, required=True)
    parser.add_argument("--section-hold", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    baseline = read_rows(args.baseline_chunks)
    candidate = read_rows(args.candidate_chunks)

    base_subset, base_superset, base_examples = containment(baseline)
    cand_subset, cand_superset, cand_examples = containment(candidate)

    holds = esg_chunker.load_section_hold_registry(args.section_hold)
    held_keys = {(ticker, stem, section) for ticker, stem, section in holds}

    held_rows = [
        row
        for row in candidate
        if (row["ticker"], row["pdf_stem"], row["section_instance_id"]) in held_keys
    ]
    held_baseline_rows = [
        row
        for row in baseline
        if (row["ticker"], row["pdf_stem"], row["section_instance_id"]) in held_keys
    ]

    base_digest = eligible_text_digest(baseline)
    cand_digest = eligible_text_digest(candidate)

    heading_hits_exact = {}
    heading_hits_containing = {}
    for ticker, heading in REGRESSION_HEADINGS.items():
        exact = sum(
            1
            for row in candidate
            if row["ticker"] == ticker and row["subsection_context"] == heading
        )
        containing = sum(
            1
            for row in candidate
            if row["ticker"] == ticker
            and heading.casefold() in (row["subsection_context"] or "").casefold()
        )
        heading_hits_exact[heading] = exact
        heading_hits_containing[heading] = containing

    noise_hits = {
        term: sum(
            1
            for row in candidate
            if term.casefold() in (row["subsection_context"] or "").casefold()
            or term.casefold() in (row["physical_section_title"] or "").casefold()
        )
        for term in NOISE_TERMS
    }

    summary = {
        "baseline_chunks": len(baseline),
        "candidate_chunks": len(candidate),
        "chunk_delta": len(candidate) - len(baseline),
        "baseline_eligible": sum(r["include_in_esg_index"] == "true" for r in baseline),
        "candidate_eligible": sum(r["include_in_esg_index"] == "true" for r in candidate),
        "baseline_contained_pairs": base_subset,
        "candidate_contained_pairs": cand_subset,
        "baseline_superset_pairs": base_superset,
        "candidate_superset_pairs": cand_superset,
        "candidate_tiling": tiling_defects(candidate),
        "held_sections": len(held_keys),
        "held_chunks_baseline": len(held_baseline_rows),
        "held_chunks_candidate": len(held_rows),
        "held_chunks_baseline_eligible": sum(
            r["include_in_esg_index"] == "true" for r in held_baseline_rows
        ),
        "held_chunks_candidate_eligible": sum(
            r["include_in_esg_index"] == "true" for r in held_rows
        ),
        "held_rag_actions": dict(Counter(r["rag_action"] for r in held_rows)),
        "eligible_chunks_removed": len(set(base_digest) - set(cand_digest)),
        "eligible_chunks_added": len(set(cand_digest) - set(base_digest)),
        "eligible_chunks_text_changed": sum(
            1
            for chunk_id in set(base_digest) & set(cand_digest)
            if base_digest[chunk_id] != cand_digest[chunk_id]
        ),
        "regression_headings_exact": heading_hits_exact,
        "regression_headings_containing": heading_hits_containing,
        "noise_hits": noise_hits,
        "candidate_containment_examples": cand_examples,
        "baseline_containment_examples": base_examples,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(json.dumps({k: v for k, v in summary.items() if not k.endswith("examples")}, indent=2))

    failures = []
    if cand_subset:
        failures.append(f"{cand_subset} contained chunk pairs remain")
    if cand_superset:
        failures.append(f"{cand_superset} superset chunk pairs remain")
    if summary["candidate_tiling"]["chunk_gaps"]:
        failures.append("chunk gaps appeared")
    if summary["held_chunks_candidate_eligible"]:
        failures.append("held sections still have eligible chunks")
    if any(count == 0 for count in heading_hits_containing.values()):
        failures.append("a regression heading was lost")
    if any(noise_hits.values()):
        failures.append("a rejected noise pattern reappeared")

    print()
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("PASS: contained tails cleared, holds applied, headings and noise unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
