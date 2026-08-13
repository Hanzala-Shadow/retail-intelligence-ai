#!/usr/bin/env python3
"""Recover substantive chunks excluded only by an auditor cross-reference."""

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
    AUDITOR_REGION_START_RE,
    BGE_MAX_TOKENS,
    BGE_MODEL,
    BGE_REVISION,
    CHUNKER_VERSION,
    ENCODING_NAME,
    FINANCIAL_STATEMENT_INDEX_RE,
    sha256_text,
)

EXCLUSION_FLAGS = {
    "below_meaningful_minimum",
    "replacement_character",
    "table_of_contents",
    "financial_statement_index_non_rag",
    "filing_boilerplate",
    "auditor_opinion",
    "exhibit_index_non_rag",
    "inherited_exhibit_index_non_rag",
    "signature_non_rag",
    "auditor_consent_non_rag",
    "unclassified_signature_container",
    "form_10k_summary_non_rag",
    "orphan_table",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--profiles",
        type=Path,
        default=Path("config/chunk_profiles_v2_frozen.json"),
    )
    parser.add_argument("--profile", default="A")
    args = parser.parse_args()

    staging = args.output_root.with_name(
        args.output_root.name + ".staging"
    )
    if args.output_root.exists() or staging.exists():
        raise FileExistsError(args.output_root)

    profiles = json.loads(
        args.profiles.read_text(encoding="utf-8")
    )
    profile = profiles[args.profile]
    config_hash = sha256_text(
        json.dumps(
            {
                "version": CHUNKER_VERSION,
                "encoding": ENCODING_NAME,
                "profile_name": args.profile,
                "profile": profile,
                "embedding_template": (
                    "v17-bounded-auditor-cross-reference-routing"
                ),
                "embedding_model": BGE_MODEL,
                "embedding_model_revision": BGE_REVISION,
                "embedding_max_tokens": BGE_MAX_TOKENS,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )

    staging.mkdir(parents=True)
    input_path = args.input_root / "chunks.jsonl"
    output_path = staging / "chunks.jsonl"
    actions = Counter()
    total = 0
    recovered = 0
    indexes = 0

    with input_path.open(encoding="utf-8") as source:
        with output_path.open("w", encoding="utf-8") as target:
            for line in source:
                if not line.strip():
                    continue
                row = json.loads(line)
                total += 1
                flags = list(row.get("quality_flags", []))
                text = str(row["chunk_text"])
                subsection = str(row["subsection_heading"])

                strict_auditor = bool(
                    AUDITOR_REGION_START_RE.search(text)
                    or AUDITOR_PROCEDURE_RE.search(text)
                    or AUDITOR_BLOCK_RE.search(subsection)
                )
                if (
                    "auditor_opinion" in flags
                    and not strict_auditor
                ):
                    flags = [
                        flag
                        for flag in flags
                        if flag != "auditor_opinion"
                    ]
                    recovered += 1

                if (
                    FINANCIAL_STATEMENT_INDEX_RE.search(subsection)
                    and "financial_statement_index_non_rag" not in flags
                ):
                    flags.append(
                        "financial_statement_index_non_rag"
                    )
                    indexes += 1

                excluded = bool(EXCLUSION_FLAGS & set(flags))
                row["quality_flags"] = flags
                row["rag_action"] = (
                    "exclude" if excluded else "include"
                )
                row["quality_status"] = (
                    "failed" if excluded else "passed"
                )
                row["chunker_version"] = CHUNKER_VERSION
                row["chunker_config_sha256"] = config_hash
                actions[row["rag_action"]] += 1
                target.write(
                    json.dumps(row, sort_keys=True) + "\n"
                )

                if total % 10000 == 0:
                    print(
                        f"PROGRESS stage=crossref-postprocess "
                        f"chunks={total} recovered={recovered}",
                        flush=True,
                    )

    summary = {
        "input_chunks": total,
        "output_chunks": total,
        "recovered_cross_reference_chunks": recovered,
        "financial_indexes_flagged": indexes,
        "chunker_version": CHUNKER_VERSION,
        "chunker_config_sha256": config_hash,
        "rag_action_counts": dict(actions),
        "method": "streaming_quality_only_v1",
    }
    (staging / "postprocess_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    staging.replace(args.output_root)
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
