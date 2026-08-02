#!/usr/bin/env python3
"""Build JSON and Markdown reports for an isolated ESG tier-QA run.

This repository has no canonical HTML renderer for tier QA. JSON is therefore
written only to ``.json`` and the human-readable report is Markdown. The path
checks below prevent raw JSON from being saved with an ``.html`` suffix.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


TIER_LABELS = {
    1: "Structural invariants",
    2: "Section distributions",
    3: "Chunk distributions",
    4: "Content validity",
    5: "Retrieval readiness",
    6: "Manual-review sampling",
}
STATUS_ORDER = ("PASS", "WARN", "FAIL", "SKIP")


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def validate_report_output_paths(json_output: Path, markdown_output: Path) -> None:
    """Reject misleading or overlapping report file names."""
    if json_output.suffix.lower() != ".json":
        raise ValueError(f"JSON report output must end in .json: {json_output}")
    if markdown_output.suffix.lower() not in {".md", ".markdown"}:
        raise ValueError(
            f"human-readable report output must end in .md or .markdown: "
            f"{markdown_output}"
        )
    if json_output.resolve() == markdown_output.resolve():
        raise ValueError("JSON and Markdown report outputs must be different files")


def _manual_summary(rows: list[dict[str, str]]) -> list[dict[str, int | str]]:
    grouped: dict[str, Counter] = {}
    for row in rows:
        reason = (row.get("sample_reason") or "unknown").strip()
        grouped.setdefault(reason, Counter())[row.get("judgment") or "UNKNOWN"] += 1
    return [
        {
            "sample_reason": reason.replace("_", " ").title(),
            "pass": counts["PASS"],
            "review": counts["REVIEW"],
            "fail": counts["FAIL"],
            "total": sum(counts.values()),
        }
        for reason, counts in sorted(grouped.items())
    ]


def build_report(
    tier_qa_dir: Path,
    sections_index: Path,
    chunks_index: Path,
) -> dict:
    """Build a report using only current QA and candidate index values."""
    generated_at = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    tier_results: dict[int, list[dict]] = {}
    tier_rows = []
    all_checks = []
    totals = Counter()
    for tier in range(1, 7):
        results = load_json(tier_qa_dir / f"qa_tier{tier}.json")
        if not isinstance(results, list):
            raise ValueError(f"Tier {tier} JSON must contain a list of checks")
        tier_results[tier] = results
        counts = Counter(str(result.get("status") or "UNKNOWN") for result in results)
        totals.update(counts)
        tier_rows.append(
            {
                "tier": tier,
                "name": TIER_LABELS[tier],
                **{status.lower(): counts[status] for status in STATUS_ORDER},
                "total": len(results),
            }
        )
        all_checks.extend({"tier": tier, **result} for result in results)

    manual_path = tier_qa_dir / "manual_section_quality_review.csv"
    manual_rows = read_csv(manual_path)
    sections = read_csv(sections_index)
    chunks = read_csv(chunks_index)
    documents = {(row["ticker"], row["pdf_stem"]) for row in sections}
    eligible_chunks = [row for row in chunks if row.get("include_in_esg_index") == "true"]
    held_chunks = [
        row
        for row in chunks
        if "section_held_by_manual_review" in (row.get("quality_flags") or "").split("|")
    ]
    held_sections = {
        (row["ticker"], row["pdf_stem"], row["section_instance_id"])
        for row in held_chunks
    }
    included_failures = [
        row
        for row in manual_rows
        if row.get("judgment") == "FAIL"
        and '"true"' in (row.get("include_in_esg_index_counts") or "")
    ]
    hard_failures = [
        {
            "tier": check["tier"],
            "check": check.get("check"),
            "title": check.get("title"),
            "headline": check.get("headline"),
        }
        for check in all_checks
        if check.get("status") == "FAIL"
    ]

    return {
        "report_version": 2,
        "generated_at": generated_at,
        "candidate": {
            "documents": len(documents),
            "sections": len(sections),
            "chunks": len(chunks),
            "eligible_chunks": len(eligible_chunks),
            "ineligible_chunks": len(chunks) - len(eligible_chunks),
            "held_sections": len(held_sections),
            "held_chunks": len(held_chunks),
            "held_chunks_eligible": sum(
                row.get("include_in_esg_index") == "true" for row in held_chunks
            ),
        },
        "status_totals": {status: totals[status] for status in STATUS_ORDER},
        "tiers": tier_rows,
        "hard_failures": hard_failures,
        "manual_review": {
            "rows": len(manual_rows),
            "judgments": dict(Counter(row.get("judgment") or "UNKNOWN" for row in manual_rows)),
            "summary": _manual_summary(manual_rows),
            "included_failures": [
                {
                    "ticker": row.get("ticker"),
                    "section_instance_id": row.get("section_instance_id"),
                    "chunk_count": int(row.get("chunk_count") or 0),
                    "issue_type": row.get("issue_type"),
                }
                for row in included_failures
            ],
            "included_failure_chunks": sum(
                int(row.get("chunk_count") or 0) for row in included_failures
            ),
        },
        "sources": {
            "tier_qa_dir": str(tier_qa_dir.resolve()),
            "sections_index": str(sections_index.resolve()),
            "chunks_index": str(chunks_index.resolve()),
            "manual_review": str(manual_path.resolve()),
        },
        "safety": {
            "promotion_performed": False,
            "embeddings_built": False,
            "vector_index_touched": False,
        },
    }


def render_markdown(report: dict) -> str:
    candidate = report["candidate"]
    totals = report["status_totals"]
    lines = [
        "# ESG candidate tier QA report",
        "",
        f"Generated: `{report['generated_at']}`",
        "",
        "This report covers an isolated candidate. It did not build embeddings, "
        "touch the vector index, or promote data.",
        "",
        "## Candidate counts",
        "",
        f"- Documents: {candidate['documents']:,}",
        f"- Sections: {candidate['sections']:,}",
        f"- Chunks: {candidate['chunks']:,}",
        f"- Eligible chunks: {candidate['eligible_chunks']:,}",
        f"- Held sections: {candidate['held_sections']:,}",
        f"- Held chunks: {candidate['held_chunks']:,}",
        f"- Held chunks still eligible: {candidate['held_chunks_eligible']:,}",
        "",
        "## Six-tier totals",
        "",
        f"- PASS: {totals['PASS']}",
        f"- WARN: {totals['WARN']}",
        f"- FAIL: {totals['FAIL']}",
        f"- SKIP: {totals['SKIP']}",
        "",
        "| Tier | Purpose | PASS | WARN | FAIL | SKIP |",
        "|---:|---|---:|---:|---:|---:|",
    ]
    for tier in report["tiers"]:
        lines.append(
            f"| {tier['tier']} | {tier['name']} | {tier['pass']} | {tier['warn']} | "
            f"{tier['fail']} | {tier['skip']} |"
        )

    lines.extend(["", "## Hard failures", ""])
    if report["hard_failures"]:
        for failure in report["hard_failures"]:
            lines.append(
                f"- Tier {failure['tier']} check {failure['check']}: "
                f"{failure['title']} — {failure['headline']}"
            )
    else:
        lines.append("- None")

    manual = report["manual_review"]
    lines.extend(
        [
            "",
            "## Manual review",
            "",
            f"- Reviewed rows: {manual['rows']}",
            f"- Failed chunks still eligible: {manual['included_failure_chunks']}",
            "",
            "## Safety result",
            "",
            "No live corpus, embedding store, manifest, dataset ID, or vector index was changed.",
            "",
        ]
    )
    return "\n".join(lines)


def validate_report_files(json_output: Path, markdown_output: Path) -> None:
    """Validate both syntax and format after writing."""
    validate_report_output_paths(json_output, markdown_output)
    loaded = load_json(json_output)
    if not isinstance(loaded, dict):
        raise ValueError(f"JSON report must contain an object: {json_output}")
    markdown = markdown_output.read_text(encoding="utf-8")
    if not markdown.lstrip().startswith("# "):
        raise ValueError(f"Markdown report is not human-readable Markdown: {markdown_output}")
    if markdown.lstrip().startswith(("{", "[")):
        raise ValueError(f"raw JSON cannot be used as a human-readable report: {markdown_output}")


def write_reports(
    report: dict,
    json_output: Path,
    markdown_output: Path,
) -> None:
    validate_report_output_paths(json_output, markdown_output)
    json_output.parent.mkdir(parents=True, exist_ok=True)
    markdown_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    markdown_output.write_text(render_markdown(report), encoding="utf-8")
    validate_report_files(json_output, markdown_output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier-qa-dir", type=Path, required=True)
    parser.add_argument("--sections-index", type=Path, required=True)
    parser.add_argument("--chunks-index", type=Path, required=True)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    args = parser.parse_args()

    report = build_report(args.tier_qa_dir, args.sections_index, args.chunks_index)
    write_reports(report, args.json_output, args.markdown_output)
    print(f"wrote JSON report to {args.json_output}")
    print(f"wrote Markdown report to {args.markdown_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
