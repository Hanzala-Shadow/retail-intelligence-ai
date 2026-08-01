#!/usr/bin/env python3
"""Compare an isolated rebuild with the accepted contained-tail candidate."""

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
from esg_year import extract_report_year  # noqa: E402


REGRESSION_HEADINGS = {
    "DELL": "BETTERING THE LIVES OF PEOPLE IN OUR SUPPLY CHAIN",
    "LOW": "CLIMATE CHANGE, ENERGY AND EMISSIONS",
    "SHOO": "BOARD OF DIRECTORS / RISK MANAGEMENT / CSR PROGRAM GOVERNANCE",
    "SVV": "NONPROFIT PARTNER SPOTLIGHTS",
    "ORLY": "Feeding Our Communities Partners",
    "TPR": "SOCIAL IMPACT COUNCIL",
}
NOISE_TERMS = (
    "Energy efficiency proceeds were allocated to Eligible Projects",
    "CATEGORY / STATE EMPLOYEES BRANDS",
    "FY24 DECKERS FOOTWEAR ENERGY USAGE BY MATERIAL CATEGORY GATE BREAKDOWN",
    "INTRODUCTION CLIMATE ACTION CIRCULAR ECONOMY",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def resolve_repo_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def normalized_chunk_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    marker = "/chunks/"
    if marker in normalized:
        return "chunks/" + normalized.split(marker, 1)[1]
    if normalized.startswith("chunks/"):
        return normalized
    return normalized


def normalize_row(row: dict[str, str]) -> dict[str, str]:
    value = dict(row)
    value["chunk_file"] = normalized_chunk_path(value.get("chunk_file") or "")
    return value


def structural_metrics(rows: list[dict[str, str]]) -> dict[str, int]:
    grouped: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[(row["ticker"], row["pdf_stem"], row["section_instance_id"])].append(row)
    contained = supersets = gaps = non_advancing = 0
    for section_rows in grouped.values():
        section_rows.sort(key=lambda row: int(row["chunk_index"]))
        for previous, current in zip(section_rows, section_rows[1:]):
            before = (int(previous["source_start_char"]), int(previous["source_end_char"]))
            after = (int(current["source_start_char"]), int(current["source_end_char"]))
            contained += int(after[0] >= before[0] and after[1] <= before[1])
            supersets += int(after[0] <= before[0] and after[1] >= before[1])
            gaps += int(after[0] > before[1])
            non_advancing += int(after[1] <= before[1])
    return {
        "contained_pairs": contained,
        "superset_pairs": supersets,
        "gaps": gaps,
        "non_advancing_boundaries": non_advancing,
    }


def heading_and_noise(rows: list[dict[str, str]]) -> tuple[dict[str, int], dict[str, int]]:
    headings = {
        heading: sum(
            row["ticker"] == ticker
            and heading.casefold() in (row.get("subsection_context") or "").casefold()
            for row in rows
        )
        for ticker, heading in REGRESSION_HEADINGS.items()
    }
    noise = {
        term: sum(
            term.casefold() in (row.get("subsection_context") or "").casefold()
            or term.casefold() in (row.get("physical_section_title") or "").casefold()
            for row in rows
        )
        for term in NOISE_TERMS
    }
    return headings, noise


def verify_chunk_files(
    rows: list[dict[str, str]], company_names: dict[str, str]
) -> dict[str, int]:
    missing = bad_hashes = suffix_failures = tail_hash_failures = 0
    for row in rows:
        chunk_path = resolve_repo_path(row["chunk_file"])
        if not chunk_path.is_file():
            missing += 1
            continue
        text = chunk_path.read_text(encoding="utf-8")
        text_hash = sha256_text(text)
        bad_hashes += int(text_hash != row["chunk_text_sha256"])
        year, status, span = extract_report_year(row["pdf_stem"])
        metadata = {
            **row,
            "company_name": company_names.get(row["canonical_ticker"].upper(), ""),
            "report_year": "" if year is None else str(year),
            "report_year_status": status,
            "report_year_span": span,
            "section_title_original": row.get("subsection_context") or "unknown",
        }
        embedding_text = esg_chunker.final_embedding_text(
            metadata, text, row.get("table_context") or ""
        )
        suffix_failures += int(not embedding_text.endswith(text))
        tail_hash_failures += int(
            sha256_text(embedding_text[-len(text) :]) != text_hash
        )
    return {
        "missing_files": missing,
        "bad_chunk_hashes": bad_hashes,
        "embedding_suffix_failures": suffix_failures,
        "embedding_tail_hash_failures": tail_hash_failures,
    }


def compare(
    accepted_root: Path,
    rebuilt_root: Path,
    hold_path: Path,
    company_manifest: Path,
) -> dict:
    accepted_sections_path = accepted_root / "esg_sections_index.csv"
    rebuilt_sections_path = rebuilt_root / "esg_sections_index.csv"
    accepted_chunks_path = accepted_root / "esg_chunks_index.csv"
    rebuilt_chunks_path = rebuilt_root / "esg_chunks_index.csv"
    accepted = read_csv(accepted_chunks_path)
    rebuilt = read_csv(rebuilt_chunks_path)
    accepted_by_id = {row["chunk_id"]: row for row in accepted}
    rebuilt_by_id = {row["chunk_id"]: row for row in rebuilt}
    common_ids = set(accepted_by_id) & set(rebuilt_by_id)

    differing_columns = Counter()
    normalized_mismatches = 0
    for chunk_id in common_ids:
        left = accepted_by_id[chunk_id]
        right = rebuilt_by_id[chunk_id]
        for column in set(left) | set(right):
            if left.get(column) != right.get(column):
                differing_columns[column] += 1
        normalized_mismatches += int(normalize_row(left) != normalize_row(right))

    company_names = {
        row["ticker"].strip().upper(): (row.get("company_name") or "").strip()
        for row in read_csv(company_manifest)
        if (row.get("ticker") or "").strip()
    }
    holds = esg_chunker.load_section_hold_registry(hold_path)
    held_keys = set(holds)

    def held_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
        return [
            row
            for row in rows
            if (row["ticker"], row["pdf_stem"], row["section_instance_id"])
            in held_keys
        ]

    accepted_held = held_rows(accepted)
    rebuilt_held = held_rows(rebuilt)
    rebuilt_held_by_key = Counter(
        (row["ticker"], row["pdf_stem"], row["section_instance_id"])
        for row in rebuilt_held
    )
    accepted_held_by_key = Counter(
        (row["ticker"], row["pdf_stem"], row["section_instance_id"])
        for row in accepted_held
    )
    hold_action_failures = 0
    for row in rebuilt_held:
        key = (row["ticker"], row["pdf_stem"], row["section_instance_id"])
        expected_action = (holds[key].get("rag_action") or "").strip()
        hold_action_failures += int(
            row.get("include_in_esg_index") != "false"
            or row.get("rag_action") != expected_action
            or "section_held_by_manual_review"
            not in (row.get("quality_flags") or "").split("|")
        )

    accepted_headings, accepted_noise = heading_and_noise(accepted)
    rebuilt_headings, rebuilt_noise = heading_and_noise(rebuilt)
    accepted_hashes = {
        chunk_id: row["chunk_text_sha256"] for chunk_id, row in accepted_by_id.items()
    }
    rebuilt_hashes = {
        chunk_id: row["chunk_text_sha256"] for chunk_id, row in rebuilt_by_id.items()
    }
    file_checks = verify_chunk_files(rebuilt, company_names)
    accepted_structure = structural_metrics(accepted)
    rebuilt_structure = structural_metrics(rebuilt)
    accepted_max_tokens = max(int(row["token_count"]) for row in accepted)
    rebuilt_max_tokens = max(int(row["token_count"]) for row in rebuilt)

    checks = {
        "section_index_byte_identical": (
            sha256_file(accepted_sections_path) == sha256_file(rebuilt_sections_path)
        ),
        "chunk_count_equal": len(accepted) == len(rebuilt),
        "eligible_chunk_count_equal": (
            sum(row["include_in_esg_index"] == "true" for row in accepted)
            == sum(row["include_in_esg_index"] == "true" for row in rebuilt)
        ),
        "chunk_id_sets_equal": set(accepted_by_id) == set(rebuilt_by_id),
        "chunk_order_equal": (
            [row["chunk_id"] for row in accepted]
            == [row["chunk_id"] for row in rebuilt]
        ),
        "chunk_hashes_equal": accepted_hashes == rebuilt_hashes,
        "normalized_indexes_equal": normalized_mismatches == 0,
        "chunk_files_present": file_checks["missing_files"] == 0,
        "chunk_file_hashes_valid": file_checks["bad_chunk_hashes"] == 0,
        "embedding_suffix_invariant": file_checks["embedding_suffix_failures"] == 0,
        "embedding_tail_hash_invariant": (
            file_checks["embedding_tail_hash_failures"] == 0
        ),
        "hold_chunk_counts_equal": accepted_held_by_key == rebuilt_held_by_key,
        "all_holds_present": set(rebuilt_held_by_key) == held_keys,
        "holds_applied_exactly": hold_action_failures == 0,
        "structural_metrics_equal": accepted_structure == rebuilt_structure,
        "contained_pairs_zero": rebuilt_structure["contained_pairs"] == 0,
        "superset_pairs_zero": rebuilt_structure["superset_pairs"] == 0,
        "gaps_zero": rebuilt_structure["gaps"] == 0,
        "non_advancing_boundaries_zero": (
            rebuilt_structure["non_advancing_boundaries"] == 0
        ),
        "maximum_token_count_equal": accepted_max_tokens == rebuilt_max_tokens,
        "maximum_token_count_within_limit": rebuilt_max_tokens <= esg_chunker.BGE_INPUT_LIMIT,
        "required_headings_equal": accepted_headings == rebuilt_headings,
        "required_headings_present": all(rebuilt_headings.values()),
        "known_noise_equal": accepted_noise == rebuilt_noise,
        "known_noise_zero": not any(rebuilt_noise.values()),
    }
    harmless_metadata_only = (
        not checks["normalized_indexes_equal"]
        and set(differing_columns) <= {"chunk_file"}
    ) or (
        checks["normalized_indexes_equal"]
        and set(differing_columns) <= {"chunk_file"}
    )
    semantic_match = all(checks.values())

    return {
        "comparison_version": 1,
        "accepted_root": str(accepted_root.resolve()),
        "rebuilt_root": str(rebuilt_root.resolve()),
        "accepted": {
            "sections_index_sha256": sha256_file(accepted_sections_path),
            "chunks_index_sha256": sha256_file(accepted_chunks_path),
            "sections": len(read_csv(accepted_sections_path)),
            "chunks": len(accepted),
            "eligible_chunks": sum(
                row["include_in_esg_index"] == "true" for row in accepted
            ),
            "maximum_token_count": accepted_max_tokens,
            "structure": accepted_structure,
        },
        "rebuilt": {
            "sections_index_sha256": sha256_file(rebuilt_sections_path),
            "chunks_index_sha256": sha256_file(rebuilt_chunks_path),
            "sections": len(read_csv(rebuilt_sections_path)),
            "chunks": len(rebuilt),
            "eligible_chunks": sum(
                row["include_in_esg_index"] == "true" for row in rebuilt
            ),
            "maximum_token_count": rebuilt_max_tokens,
            "structure": rebuilt_structure,
        },
        "checks": checks,
        "semantic_match": semantic_match,
        "chunks_index_byte_identical": (
            sha256_file(accepted_chunks_path) == sha256_file(rebuilt_chunks_path)
        ),
        "harmless_path_metadata_difference_only": harmless_metadata_only,
        "missing_chunk_ids": sorted(set(accepted_by_id) - set(rebuilt_by_id)),
        "extra_chunk_ids": sorted(set(rebuilt_by_id) - set(accepted_by_id)),
        "normalized_row_mismatches": normalized_mismatches,
        "differing_columns": dict(differing_columns),
        "file_checks": file_checks,
        "hold_chunk_counts": {
            "/".join(key): rebuilt_held_by_key[key] for key in sorted(held_keys)
        },
        "hold_action_failures": hold_action_failures,
        "required_heading_hits": rebuilt_headings,
        "known_noise_hits": rebuilt_noise,
    }


def render_markdown(report: dict) -> str:
    rebuilt = report["rebuilt"]
    lines = [
        "# ESG contained-tail isolated rebuild comparison",
        "",
        f"Semantic match: **{'PASS' if report['semantic_match'] else 'FAIL'}**",
        "",
        f"- Sections: {rebuilt['sections']:,}",
        f"- Chunks: {rebuilt['chunks']:,}",
        f"- Eligible chunks: {rebuilt['eligible_chunks']:,}",
        f"- Maximum token count: {rebuilt['maximum_token_count']}",
        f"- Chunk index byte-identical: {report['chunks_index_byte_identical']}",
        "",
        "The chunk index may differ in output-root path text. That is harmless only "
        "when all normalized rows, chunk IDs, and chunk hashes match.",
        "",
        "## Checks",
        "",
    ]
    lines.extend(
        f"- {'PASS' if passed else 'FAIL'}: {name.replace('_', ' ')}"
        for name, passed in report["checks"].items()
    )
    lines.extend(["", "## Held sections", ""])
    lines.extend(
        f"- `{key}`: {count} chunks"
        for key, count in report["hold_chunk_counts"].items()
    )
    lines.extend(["", "## Metadata differences", ""])
    if report["differing_columns"]:
        lines.extend(
            f"- `{column}`: {count} rows"
            for column, count in sorted(report["differing_columns"].items())
        )
    else:
        lines.append("- None")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accepted-root", type=Path, required=True)
    parser.add_argument("--rebuilt-root", type=Path, required=True)
    parser.add_argument("--section-hold", type=Path, required=True)
    parser.add_argument("--company-manifest", type=Path, required=True)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    args = parser.parse_args()

    report = compare(
        args.accepted_root,
        args.rebuilt_root,
        args.section_hold,
        args.company_manifest,
    )
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    args.markdown_out.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return 0 if report["semantic_match"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
