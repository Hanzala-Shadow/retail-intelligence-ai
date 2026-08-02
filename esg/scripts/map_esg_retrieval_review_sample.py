#!/usr/bin/env python3
"""Map an earlier retrieval-review sample to the closest chunks in a candidate."""

from __future__ import annotations

import argparse
import csv
import difflib
import re
from collections import defaultdict
from pathlib import Path


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def resolve(path_text: str) -> Path:
    path = Path(path_text)
    if path.is_absolute():
        return path
    direct = Path.cwd() / path
    if direct.is_file():
        return direct
    parts = list(path.parts)
    if "reports" in parts:
        return Path.cwd().joinpath(*parts[parts.index("reports") :])
    return direct


def normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9%$]+", text.casefold()))


def jaccard(left: set[str], right: set[str]) -> float:
    return len(left & right) / max(len(left | right), 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prior-sample", type=Path, required=True)
    parser.add_argument("--prior-review", type=Path, required=True)
    parser.add_argument("--candidate-index", type=Path, required=True)
    parser.add_argument("--csv-out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    args = parser.parse_args()

    prior = read_csv(args.prior_sample)
    reviews = {row["sample_id"]: row for row in read_csv(args.prior_review)}
    candidates = read_csv(args.candidate_index)
    by_doc: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in candidates:
        by_doc[(row["ticker"], row["pdf_stem"])].append(row)

    mapped: list[dict[str, str]] = []
    for old in prior:
        old_text = resolve(old["chunk_file"]).read_text(encoding="utf-8")
        old_norm = normalized(old_text)
        old_words = words(old_norm)
        pool = by_doc[(old["ticker"], old["pdf_stem"])]
        shortlist = []
        for row in pool:
            text = resolve(row["chunk_file"]).read_text(encoding="utf-8")
            score = jaccard(old_words, words(text))
            shortlist.append((score, row, text))
        shortlist.sort(key=lambda item: item[0], reverse=True)
        best = max(
            shortlist[:12],
            key=lambda item: difflib.SequenceMatcher(
                None, old_norm, normalized(item[2]), autojunk=False
            ).ratio(),
        )
        token_score, row, current_text = best
        sequence_score = difflib.SequenceMatcher(
            None, old_norm, normalized(current_text), autojunk=False
        ).ratio()
        review = reviews.get(old["sample_id"], {})
        mapped.append(
            {
                "sample_id": old["sample_id"],
                "stratum": old["stratum"],
                "ticker": old["ticker"],
                "prior_grade": review.get("grade", ""),
                "prior_issue_codes": review.get("issue_codes", ""),
                "match_sequence_ratio": f"{sequence_score:.4f}",
                "match_word_jaccard": f"{token_score:.4f}",
                "chunk_id": row["chunk_id"],
                "include_in_esg_index": row["include_in_esg_index"],
                "rag_action": row["rag_action"],
                "quality_flags": row["quality_flags"],
                "physical_section_title": row.get("physical_section_title", ""),
                "subsection_context": row.get("subsection_context", ""),
                "table_context": row.get("table_context", ""),
                "chunk_file": row["chunk_file"],
            }
        )

    args.csv_out.parent.mkdir(parents=True, exist_ok=True)
    with args.csv_out.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(mapped[0]))
        writer.writeheader()
        writer.writerows(mapped)

    lines = ["# Mapped retrieval review sample", ""]
    for row in mapped:
        text = resolve(row["chunk_file"]).read_text(encoding="utf-8")
        lines.extend(
            [
                f"## {row['sample_id']}. {row['ticker']} ({row['stratum']})",
                "",
                f"Prior: {row['prior_grade']} — {row['prior_issue_codes']}",
                f"Match: {row['match_sequence_ratio']}; eligible: {row['include_in_esg_index']}",
                f"Title: {row['physical_section_title']}",
                f"Subsection: {row['subsection_context']}",
                f"Table context: {row['table_context']}",
                "",
                "```text",
                text,
                "```",
                "",
            ]
        )
    args.markdown_out.write_text("\n".join(lines), encoding="utf-8")
    print(f"mapped={len(mapped)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
