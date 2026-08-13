"""Sample readable chunks to seed benchmark questions.

Writing questions from scratch means hunting through 682 reports for a passage
that decisively answers something. Seeding from a chunk inverts that: the
author reads a passage and writes the question it answers, so the gold label
is exact by construction.

The cost is bias. A question written while looking at a passage tends to reuse
its vocabulary, and retrieval then scores on lexical overlap rather than
meaning. The brief handles that with a paraphrase rule; this script's job is
to supply passages worth writing about.

Selection favours substance: enough tokens to contain a fact, a topical
section rather than front matter, and at most a couple per company so the set
stays spread.
"""

from __future__ import annotations

import argparse
import csv
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

TRUE_VALUES = {"1", "true", "yes", "y"}
# Front matter and boilerplate rarely contain a fact worth asking about.
EXCLUDED_SECTIONS = {"about_this_report", "appendix"}
MIN_TOKENS = 90
MAX_TOKENS = 400


def is_eligible(row: dict[str, str]) -> bool:
    return (row.get("include_in_esg_index") or "").strip().lower() in TRUE_VALUES


def body_of(embedding_text: str) -> str:
    """Strip the embedding header, leaving the passage itself."""
    return embedding_text.split("\n\n", 1)[1] if "\n\n" in embedding_text else ""


def is_substantive(body: str) -> bool:
    """Keep passages that state something, not lists of headings."""
    words = body.split()
    if len(words) < 40:
        return False
    # A passage with no sentence-like structure is usually a fragment table
    # or a caption run, which is hard to write a fair question about.
    if not re.search(r"[a-z]{3,}\s+[a-z]{3,}\s+[a-z]{3,}", body):
        return False
    return True


def has_figures(body: str) -> bool:
    """Flag passages carrying a quantity worth asking a precise question about.

    A bare year is not a figure. Percentages, magnitudes and units are.
    """
    if re.search(r"\d+(?:\.\d+)?\s*%|percent", body, re.I):
        return True
    if re.search(r"\d{1,3}(?:,\d{3})+", body):
        return True
    return bool(
        re.search(
            r"\d+(?:\.\d+)?\s*(?:tons?|tonnes?|MT|kg|lbs?|pounds?|GJ|MWh|kWh|"
            r"gallons?|liters?|litres?|million|billion|hours?|employees?|stores?)",
            body,
            re.I,
        )
    )


def year_of(source_id: str) -> str:
    match = re.search(r"(\d{4})(?:_\d{4})?$", source_id)
    return match.group(1) if match else ""


def company_of(embedding_text: str) -> str:
    for line in embedding_text.split("\n")[:6]:
        if line.startswith("Company:"):
            return line.split(":", 1)[1].strip()
    return ""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--count", type=int, default=60)
    parser.add_argument(
        "--per-company",
        type=int,
        default=2,
        help="Cap per company so one large reporter cannot dominate.",
    )
    parser.add_argument("--seed", type=int, default=20260811)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.index.is_file():
        print(f"ERROR: index not found: {args.index}", file=sys.stderr)
        return 2

    with args.index.open("r", encoding="utf-8-sig", newline="") as handle:
        candidates = []
        for row in csv.DictReader(handle):
            if not is_eligible(row):
                continue
            if row["section_code"] in EXCLUDED_SECTIONS:
                continue
            try:
                tokens = int(row["token_count"])
            except (TypeError, ValueError):
                continue
            if not MIN_TOKENS <= tokens <= MAX_TOKENS:
                continue
            body = body_of(row["embedding_text"])
            if not is_substantive(body):
                continue
            row["_body"] = body
            candidates.append(row)

    if not candidates:
        print("ERROR: no candidate chunks matched the filters.", file=sys.stderr)
        return 1

    rng = random.Random(args.seed)
    rng.shuffle(candidates)

    # Round-robin over companies, then topics, so the pack spans the corpus
    # instead of clustering on whoever writes the longest reports.
    by_company: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in candidates:
        by_company[row["canonical_ticker"]].append(row)

    # Shuffle the company order rather than walking it alphabetically. A
    # sorted walk that stops early takes every passage from the front of the
    # alphabet and none from the back, which is not a spread.
    tickers = sorted(by_company)
    rng.shuffle(tickers)

    picked: list[dict[str, str]] = []
    for depth in range(args.per_company):
        for ticker in tickers:
            if len(picked) >= args.count:
                break
            if depth < len(by_company[ticker]):
                picked.append(by_company[ticker][depth])
        if len(picked) >= args.count:
            break

    picked = picked[: args.count]
    picked.sort(key=lambda r: (r["canonical_ticker"], r["chunk_id"]))

    args.out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.out_dir / "seed_chunks.csv"
    md_path = args.out_dir / "seed_chunks.md"
    if (csv_path.exists() or md_path.exists()) and not args.force:
        print("ERROR: outputs exist. Pass --force.", file=sys.stderr)
        return 2

    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "seed_id",
                "chunk_id",
                "ticker",
                "company",
                "year",
                "topic",
                "has_figures",
                "passage",
            ]
        )
        for n, row in enumerate(picked, 1):
            writer.writerow(
                [
                    f"S{n:03d}",
                    row["chunk_id"],
                    row["canonical_ticker"],
                    company_of(row["embedding_text"]),
                    year_of(row["source_id"]),
                    row["section_code"],
                    "yes" if has_figures(row["_body"]) else "no",
                    row["_body"].strip(),
                ]
            )

    with md_path.open("w", encoding="utf-8") as handle:
        handle.write("# Seed passages for benchmark questions\n\n")
        handle.write(
            f"{len(picked)} passages from {len({r['canonical_ticker'] for r in picked})} "
            "companies. Read one, write the question it answers, and record the "
            "`seed_id` in your question row.\n\n"
            "**Paraphrase.** Do not reuse the passage's distinctive words, or the "
            "benchmark measures keyword overlap instead of retrieval.\n\n---\n\n"
        )
        for n, row in enumerate(picked, 1):
            figures = " · has figures" if has_figures(row["_body"]) else ""
            handle.write(
                f"## S{n:03d} — {row['canonical_ticker']} "
                f"{year_of(row['source_id'])} — {row['section_code']}{figures}\n\n"
            )
            handle.write(f"`{row['chunk_id']}`\n\n")
            handle.write("> " + row["_body"].strip().replace("\n", "\n> ") + "\n\n")

    companies = len({r["canonical_ticker"] for r in picked})
    topics = len({r["section_code"] for r in picked})
    print(f"Wrote {len(picked)} seed passages")
    print(f"  companies: {companies}")
    print(f"  topics:    {topics}")
    print(f"  {csv_path}")
    print(f"  {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
