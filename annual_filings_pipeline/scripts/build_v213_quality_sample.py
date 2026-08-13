#!/usr/bin/env python3
"""Build a deterministic final sample with correct continuation strata."""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
from collections import Counter
from pathlib import Path


QUOTAS = {
    "narrative": 100,
    "table": 100,
    "backward_continuation": 75,
    "forward_continuation": 75,
    "late_financial": 100,
    "excluded": 100,
    "list": 50,
}
LATE_FLAGS = {
    "late_financial_content_routed_to_item_8",
    "inherited_late_financial_region",
}


def category(row: dict[str, object]) -> str:
    flags = set(row["quality_flags"])
    if row["rag_action"] == "exclude":
        return "excluded"
    if flags & LATE_FLAGS:
        return "late_financial"
    if bool(row["continuation_from_previous"]):
        return "backward_continuation"
    if bool(row["continues_to_next"]):
        return "forward_continuation"
    if row["chunk_type"] in {
        "table", "table_continuation", "mixed_approved",
    }:
        return "table"
    if row["chunk_type"] == "list":
        return "list"
    return "narrative"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chunks-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--seed",
        default="fy2325-v213-final-quality-review-20260725",
    )
    args = parser.parse_args()

    if args.output_root.exists():
        raise FileExistsError(args.output_root)
    heaps = {name: [] for name in QUOTAS}
    scanned = 0

    with (args.chunks_root / "chunks.jsonl").open(
        "r", encoding="utf-8"
    ) as handle:
        for line in handle:
            if not line.strip():
                continue
            scanned += 1
            row = json.loads(line)
            group = category(row)
            score = int.from_bytes(
                hashlib.sha256(
                    f"{args.seed}:{row['chunk_id']}".encode()
                ).digest(),
                "big",
            )
            item = (-score, str(row["chunk_id"]), row)
            heapq.heappush(heaps[group], item)
            if len(heaps[group]) > QUOTAS[group]:
                heapq.heappop(heaps[group])
            if scanned % 10000 == 0:
                print(
                    f"PROGRESS stage=sample-scan chunks={scanned}",
                    flush=True,
                )

    selected = []
    for group, quota in QUOTAS.items():
        values = sorted(
            (-negative, chunk_id, row)
            for negative, chunk_id, row in heaps[group]
        )
        if len(values) != quota:
            raise RuntimeError(
                f"{group}: expected {quota}, found {len(values)}"
            )
        for _, _, row in values:
            row = dict(row)
            row["review_stratum"] = group
            selected.append(row)

    if len(selected) != sum(QUOTAS.values()):
        raise RuntimeError("sample size mismatch")
    if len({row["chunk_id"] for row in selected}) != len(selected):
        raise RuntimeError("duplicate sampled chunk IDs")

    selected.sort(key=lambda row: (
        row["review_stratum"],
        row["ticker"],
        int(row["coverage_year"]),
        row["chunk_id"],
    ))
    args.output_root.mkdir(parents=True)

    with (args.output_root / "sample_chunks.jsonl").open(
        "w", encoding="utf-8"
    ) as handle:
        for row in selected:
            handle.write(json.dumps(row, sort_keys=True) + "\n")

    with (args.output_root / "sample_review.txt").open(
        "w", encoding="utf-8"
    ) as handle:
        for index, row in enumerate(selected, start=1):
            handle.write("=" * 88 + "\n")
            handle.write(f"SAMPLE: {index}/{len(selected)}\n")
            handle.write(f"STRATUM: {row['review_stratum']}\n")
            handle.write(f"CHUNK: {row['chunk_id']}\n")
            handle.write(
                f"COMPANY: {row['ticker']} "
                f"YEAR: {row['coverage_year']}\n"
            )
            handle.write(
                f"SECTION: {row['canonical_section_code']} "
                f"RAG_SECTION: {row['rag_section_code']}\n"
            )
            handle.write(
                f"ACTION: {row['rag_action']} "
                f"TYPE: {row['chunk_type']}\n"
            )
            handle.write(
                f"TOKENS: {row['token_count']} "
                f"BGE: {row['embedding_token_count']}\n"
            )
            handle.write(
                "CONTINUATION_FROM_PREVIOUS: "
                f"{row['continuation_from_previous']}\n"
            )
            handle.write(
                f"CONTINUES_TO_NEXT: {row['continues_to_next']}\n"
            )
            handle.write(f"FLAGS: {row['quality_flags']}\n")
            handle.write(
                f"SUBSECTION: {row['subsection_heading']}\n\n"
            )
            handle.write(str(row["chunk_text"]))
            handle.write("\n\n")

    summary = {
        "source": str(args.chunks_root / "chunks.jsonl"),
        "source_chunks": scanned,
        "seed": args.seed,
        "sample_size": len(selected),
        "strata": dict(Counter(
            row["review_stratum"] for row in selected
        )),
        "actions": dict(Counter(
            row["rag_action"] for row in selected
        )),
        "types": dict(Counter(
            row["chunk_type"] for row in selected
        )),
        "years": dict(Counter(
            str(row["coverage_year"]) for row in selected
        )),
        "unique_tickers": len({row["ticker"] for row in selected}),
        "unique_sections": len({
            row["canonical_section_code"] for row in selected
        }),
    }
    (args.output_root / "sample_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
