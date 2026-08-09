#!/usr/bin/env python3
"""Independent gates against auditor-state leakage into RAG financial data."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.chunker_v2 import (
    AUDITOR_BLOCK_RE,
    AUDITOR_PROCEDURE_RE,
    AUDITOR_REGION_SECTIONS,
    AUDITOR_REGION_START_RE,
    financial_statement_boundary,
)


def jsonl(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chunks-root", type=Path, required=True)
    args = parser.parse_args()

    failures: list[str] = []
    counts = Counter()
    by_section: dict[str, list[dict[str, object]]] = {}

    for index, row in enumerate(
        jsonl(args.chunks_root / "chunks.jsonl"),
        start=1,
    ):
        counts["chunks"] += 1
        flags = set(row.get("quality_flags", []))
        if "auditor_opinion" in flags:
            counts["auditor_flagged"] += 1
        by_section.setdefault(str(row["section_id"]), []).append(row)
        if index % 10000 == 0:
            print(
                f"PROGRESS stage=auditor-boundary-scan "
                f"chunks={index} sections={len(by_section)}",
                flush=True,
            )

    for values in by_section.values():
        values.sort(
            key=lambda row: (
                int(row["section_start_char"]),
                int(row["section_end_char"]),
                int(row["chunk_index"]),
            )
        )
        auditor_region = False
        for row in values:
            flags = set(row.get("quality_flags", []))
            text = str(row["chunk_text"])
            subsection = str(row["subsection_heading"])
            canonical = str(row["canonical_section_code"])
            boundary = financial_statement_boundary(
                str(row["chunk_type"]),
                text,
                subsection,
            )
            direct_text = bool(
                AUDITOR_REGION_START_RE.search(text)
                or AUDITOR_PROCEDURE_RE.search(text)
            )
            direct_heading = bool(
                AUDITOR_BLOCK_RE.search(subsection)
            )
            auditor_flagged = "auditor_opinion" in flags

            if canonical not in AUDITOR_REGION_SECTIONS:
                if (
                    auditor_flagged
                    and not direct_text
                    and not direct_heading
                ):
                    failures.append(
                        "auditor_leaked_nonfinancial_section:"
                        + str(row["chunk_id"])
                    )
                auditor_region = False
                continue

            if boundary and not direct_text:
                auditor_region = False

            if direct_text or (
                direct_heading and not boundary
            ):
                auditor_region = True

            auditor_flagged = "auditor_opinion" in flags
            if (
                auditor_flagged
                and canonical not in AUDITOR_REGION_SECTIONS
                and not direct_text
                and not direct_heading
            ):
                failures.append(
                    "auditor_leaked_nonfinancial_section:"
                    + str(row["chunk_id"])
                )

            if (
                auditor_flagged
                and boundary
                and not direct_text
            ):
                failures.append(
                    "financial_boundary_excluded_as_auditor:"
                    + str(row["chunk_id"])
                )

            if (
                auditor_flagged
                and not auditor_region
                and not direct_text
                and not direct_heading
            ):
                failures.append(
                    "auditor_flag_outside_bounded_region:"
                    + str(row["chunk_id"])
                )

            if (
                row["rag_action"] == "include"
                and auditor_region
                and not boundary
            ):
                failures.append(
                    "included_inside_auditor_region:"
                    + str(row["chunk_id"])
                )

            if (
                "exhibit_index_non_rag" in flags
                or "inherited_exhibit_index_non_rag" in flags
                or "signature_non_rag" in flags
                or "auditor_consent_non_rag" in flags
            ):
                auditor_region = False

    summary = {
        "chunks": counts["chunks"],
        "auditor_flagged": counts["auditor_flagged"],
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
