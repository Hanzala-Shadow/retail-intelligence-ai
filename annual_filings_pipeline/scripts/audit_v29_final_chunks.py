#!/usr/bin/env python3
"""Final material RAG-quality gates for the v2.15 chunk corpus."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.chunker_v2 import (
    AUDITOR_BLOCK_RE,
    AUDITOR_PROCEDURE_RE,
    financial_statement_boundary,
    grammatical_continuation_end,
    grammatical_continuation_start,
)

START_RE = re.compile(
    r"(?i)^\s*(?:and|or|but|with|of|to|from|which|that|including|"
    r"excluding|through|under|over|as)\b"
)
END_RE = re.compile(
    r"(?i)(?:,|\b(?:and|or|but|with|of|to|from|which|that|including|"
    r"excluding|through|under|over|as))\s*$"
)
SUBSTANTIVE_RE = re.compile(
    r"(?i)(?:"
    r"net\s+(?:sales|revenue|income)|"
    r"total\s+(?:assets|liabilities)|"
    r"foreign\s+currency|"
    r"restructuring|"
    r"impairment|"
    r"reportable\s+segment|"
    r"cash\s+flow|"
    r"derivative|"
    r"tariff|"
    r"credit\s+facility|"
    r"lease\s+(?:liabilit|obligation)|"
    r"notes?\s+to\s+(?:the\s+)?consolidated\s+financial"
    r")"
)


AUDITOR_OR_INDEX_RE = re.compile(
    r"(?i)(?:"
    r"report\s+of\s+independent\s+registered\s+public"
    r"\s+accounting\s+firm|"
    r"opinion\s+on\s+(?:the\s+)?(?:consolidated\s+)?"
    r"financial\s+statements|"
    r"opinion\s+on\s+internal\s+control|"
    r"description\s+of\s+the\s+matter|"
    r"critical\s+audit\s+matter|"
    r"how\s+we\s+addressed\s+the\s+matter|"
    r"auditing\s+management|"
    r"our\s+audit\s+procedures|"
    r"index\s+to\s+consolidated\s+financial\s+statements"
    r")"
)
NARRATIVE_TYPES = {"narrative", "list", "mixed_approved"}


def comparable_prose_boundary(
    left: dict[str, object] | None,
    right: dict[str, object] | None,
) -> bool:
    """Return true only for a possible split in contiguous running prose."""
    if left is None or right is None:
        return False
    if str(left["chunk_type"]) not in NARRATIVE_TYPES:
        return False
    if str(right["chunk_type"]) not in NARRATIVE_TYPES:
        return False
    if str(left["chunk_type"]) != str(right["chunk_type"]):
        return False
    if (
        str(left["subsection_heading"])
        != str(right["subsection_heading"])
    ):
        return False
    gap = (
        int(right["section_start_char"])
        - int(left["section_end_char"])
    )
    return 0 <= gap <= 8


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chunks-root", type=Path, required=True)
    parser.add_argument("--sections-root", type=Path, required=True)
    args = parser.parse_args()
    rows = [
        json.loads(line)
        for line in (args.chunks_root / "chunks.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    included = [row for row in rows if row["rag_action"] == "include"]
    excluded = [row for row in rows if row["rag_action"] == "exclude"]
    section_rows = {
        str(row["section_id"]): row
        for row in [
            json.loads(line)
            for line in (args.sections_root / "sections.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
    }
    section_texts: dict[str, str] = {}

    def section_text(section_id: str) -> str:
        if section_id not in section_texts:
            row = section_rows[section_id]
            path = Path(str(row["output_file"]))
            if not path.is_file():
                path = args.sections_root / "10k" / path.name
            section_texts[section_id] = path.read_text(encoding="utf-8")
        return section_texts[section_id]
    failures: list[str] = []

    by_section: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        by_section.setdefault(str(row["section_id"]), []).append(row)

    included_inside_auditor_region: set[str] = set()
    auditor_region_sections = {
        "Item_8",
        "Item_15",
        "Item_16",
        "Signatures",
    }
    for section_values in by_section.values():
        canonical_section = str(
            section_values[0]["canonical_section_code"]
        )
        if canonical_section not in auditor_region_sections:
            continue
        section_values.sort(key=lambda row: (
            int(row["section_start_char"]),
            int(row["section_end_char"]),
            int(row["chunk_index"]),
        ))
        auditor_region = False
        for row in section_values:
            flags = set(row["quality_flags"])
            if "auditor_opinion" in flags:
                auditor_region = True
            elif (
                auditor_region
                and (
                    "exhibit_index_non_rag" in flags
                    or "inherited_exhibit_index_non_rag" in flags
                )
            ):
                auditor_region = False
            elif (
                auditor_region
                and financial_statement_boundary(
                    str(row["chunk_type"]),
                    str(row["chunk_text"]),
                    str(row["subsection_heading"]),
                )
            ):
                auditor_region = False
            if auditor_region and row["rag_action"] == "include":
                included_inside_auditor_region.add(str(row["chunk_id"]))

    ids = [str(row["chunk_id"]) for row in rows]
    if len(ids) != len(set(ids)):
        failures.append("duplicate_chunk_ids")
    versions = {str(row["chunker_version"]) for row in rows}
    if versions != {"fy2325-chunker-v2.17"}:
        failures.append(f"versions={sorted(versions)}")
    hashes = {str(row["chunker_config_sha256"]) for row in rows}
    if len(hashes) != 1:
        failures.append(f"config_hashes={len(hashes)}")

    included_neighbors: dict[
        str,
        tuple[
            dict[str, object] | None,
            dict[str, object] | None,
        ],
    ] = {}
    for section_values in by_section.values():
        ordered = sorted(
            (
                row
                for row in section_values
                if row["rag_action"] == "include"
            ),
            key=lambda row: (
                int(row["section_start_char"]),
                int(row["section_end_char"]),
                int(row["chunk_index"]),
            ),
        )
        for neighbor_index, row in enumerate(ordered):
            included_neighbors[str(row["chunk_id"])] = (
                ordered[neighbor_index - 1]
                if neighbor_index
                else None,
                (
                    ordered[neighbor_index + 1]
                    if neighbor_index + 1 < len(ordered)
                    else None
                ),
            )

    started = time.monotonic()
    total_included = len(included)
    for index, row in enumerate(included, start=1):
        chunk_id = str(row["chunk_id"])
        canonical = str(row["canonical_section_code"])
        rag_section = str(row["rag_section_code"])
        value = str(row["chunk_text"])
        source = section_text(str(row["section_id"]))
        start = int(row["section_start_char"])
        end = int(row["section_end_char"])
        previous_row, following_row = included_neighbors[chunk_id]
        if int(row["token_count"]) < 50 or int(row["token_count"]) > 400:
            failures.append(f"included_content_limit:{chunk_id}")
        if int(row["embedding_token_count"]) > 512:
            failures.append(f"included_embedding_limit:{chunk_id}")
        if canonical in {"Item_16", "Signatures"} and rag_section != "Item_8":
            failures.append(f"included_late_nonfinancial:{chunk_id}")
        if rag_section in {"Exhibit_Index", "Signatures", "Auditor_Consent"}:
            failures.append(f"included_nonrag_section:{chunk_id}")
        if "[TABLE_START:" in row["embedding_text"] or (
            "[TABLE_END:" in row["embedding_text"]
        ):
            failures.append(f"table_marker:{chunk_id}")
        if (
            row["chunk_type"] in {"narrative", "list", "mixed_approved"}
            and comparable_prose_boundary(previous_row, row)
            and grammatical_continuation_start(source, start)
            and not row["continuation_from_previous"]
        ):
            failures.append(f"unhandled_grammatical_start:{chunk_id}")
        if (
            row["chunk_type"] in {"narrative", "list", "mixed_approved"}
            and comparable_prose_boundary(row, following_row)
            and grammatical_continuation_end(source, end)
            and not row["continues_to_next"]
        ):
            failures.append(f"unhandled_grammatical_end:{chunk_id}")
        if bool(row["continuation_from_previous"]) != (
            "Continuation context:" in str(row["embedding_text"])
        ):
            failures.append(f"backward_context_metadata:{chunk_id}")
        if bool(row["continues_to_next"]) != (
            "Forward continuation context:" in str(row["embedding_text"])
        ):
            failures.append(f"forward_context_metadata:{chunk_id}")
        if (
            AUDITOR_BLOCK_RE.search(value)
            or AUDITOR_BLOCK_RE.search(str(row["subsection_heading"]))
            or (
                canonical in {"Item_15", "Item_16", "Signatures"}
                and AUDITOR_PROCEDURE_RE.search(value)
            )
        ):
            failures.append(f"included_auditor_content:{chunk_id}")
        if chunk_id in included_inside_auditor_region:
            failures.append(f"included_auditor_region:{chunk_id}")
        if index % 1000 == 0 or index == total_included:
            elapsed = max(0.001, time.monotonic() - started)
            rate = index / elapsed
            eta = (total_included - index) / rate if rate else 0
            print(
                f"PROGRESS stage=rag-audit "
                f"{index}/{total_included} "
                f"({100.0 * index / total_included:.1f}%) "
                f"elapsed={time.strftime('%H:%M:%S', time.gmtime(elapsed))} "
                f"eta={time.strftime('%H:%M:%S', time.gmtime(eta))}",
                flush=True,
            )

    # Auditor reports and Critical Audit Matter discussions can span
    # several semantic chunks. Some middle chunks contain company-specific
    # financial terminology without repeating an auditor heading.
    auditor_context_ids = set()

    for index, row in enumerate(rows):
        if row["rag_action"] != "exclude":
            continue

        start = max(0, index - 2)
        end = min(len(rows), index + 3)

        for neighbor in rows[start:end]:
            if (
                neighbor["section_id"] == row["section_id"]
                and "auditor_opinion" in neighbor["quality_flags"]
            ):
                auditor_context_ids.add(row["chunk_id"])
                break

    suspicious_excluded = [
        row
        for row in excluded
        if (
            {
                "unclassified_signature_container",
                "form_10k_summary_non_rag",
            }
            & set(row["quality_flags"])
            and (
                SUBSTANTIVE_RE.search(str(row["chunk_text"]))
                or re.search(
                    r"(?i)notes?\s+to\s+(?:the\s+)?consolidated",
                    str(row["subsection_heading"]),
                )
            )
            and row["chunk_id"] not in auditor_context_ids
            and not re.search(
                r"(?i)(?:attached\s+as\s+)?exhibits?\s+101\b"
                r"|\binline\s+XBRL\b"
                r"|\bformatted\s+in\s+XBRL\b",
                str(row["subsection_heading"])
                + "\n"
                + str(row["chunk_text"])[:900],
            )
            and not AUDITOR_OR_INDEX_RE.search(
                str(row["subsection_heading"])
                + "\\n"
                + str(row["chunk_text"])[:900]
            )
            and int(row["token_count"]) >= 50
        )
    ]
    for row in suspicious_excluded:
        failures.append(
            f"substantive_late_exclusion:{row['chunk_id']}"
        )

    summary = {
        "chunks": len(rows),
        "included": len(included),
        "excluded": len(excluded),
        "versions": dict(
            Counter(str(row["chunker_version"]) for row in rows)
        ),
        "config_hashes": len(hashes),
        "rag_actions": dict(
            Counter(str(row["rag_action"]) for row in rows)
        ),
        "policy_flags": dict(
            Counter(
                flag
                for row in rows
                for flag in row["quality_flags"]
                if flag in {
                    "late_financial_content_routed_to_item_8",
                    "inherited_late_financial_region",
                    "exhibit_index_non_rag",
                    "signature_non_rag",
                    "auditor_consent_non_rag",
                    "form_10k_summary_non_rag",
                    "unclassified_signature_container",
                }
            )
        ),
        "backward_continuations": sum(
            bool(row["continuation_from_previous"]) for row in included
        ),
        "forward_continuations": sum(
            bool(row["continues_to_next"]) for row in included
        ),
        "suspicious_substantive_exclusions": len(suspicious_excluded),
        "failures": len(failures),
        "result": "PASS" if not failures else "REVIEW_REQUIRED",
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    for failure in failures[:100]:
        print("FAIL:", failure)
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
