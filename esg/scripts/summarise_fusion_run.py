"""Report what a docling-fusion run actually produced.

A long run that ends with "0 failed" says almost nothing. FLEXSTEEL-2024 came
through the smoke test with no error at all while 98% of its words landed in no
region. What matters afterwards is not whether stages exited cleanly but how
much of the corpus is genuinely eligible for retrieval, and which documents are
being held back and why.

So this prints three things:
  - documents by quality flag, worst first, named
  - chunks by retrieval gate, so the drop from produced to indexable is visible
  - the documents contributing nothing to the index

Read-only. Takes the two indexes a run writes and prints; writes nothing.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path


def read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def bar(count: int, total: int, width: int = 28) -> str:
    filled = int(round(width * count / total)) if total else 0
    return "#" * filled + "." * (width - filled)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parse-index", type=Path, required=True)
    parser.add_argument("--chunks-index", type=Path, required=True)
    parser.add_argument(
        "--list-limit",
        type=int,
        default=12,
        help="how many document names to print per problem group",
    )
    args = parser.parse_args(argv)

    parse_rows = read_rows(args.parse_index)
    chunk_rows = read_rows(args.chunks_index)

    if not parse_rows:
        print(f"no parse index at {args.parse_index}", file=sys.stderr)
        return 1

    # ---- documents -------------------------------------------------------
    # Not parser_used: the bridge stamps docling_fusion on reused rows too, and
    # rightly so, since the text is fusion output either way. Only the reason
    # distinguishes a row built from nothing from one carried over.
    synthesised = sum(
        1
        for r in parse_rows
        if (r.get("parser_reason") or "").startswith("synthesised")
    )
    flagged: dict[str, list[str]] = defaultdict(list)
    clean = 0
    for row in parse_rows:
        flags = [f for f in (row.get("quality_flags") or "").split("|") if f]
        if not flags:
            clean += 1
        for flag in flags:
            flagged[flag].append(Path(row.get("pdf_file") or "?").stem)

    total_docs = len(parse_rows)
    print(f"documents           : {total_docs}")
    print(f"  synthesised row   : {synthesised}  (no production parse existed)")
    print(f"  no quality flag   : {clean}  ({clean / total_docs:.0%})")
    for flag, stems in sorted(flagged.items(), key=lambda kv: -len(kv[1])):
        print(f"  {flag:<22}: {len(stems)}")
        for stem in sorted(stems)[: args.list_limit]:
            print(f"      {stem}")
        if len(stems) > args.list_limit:
            print(f"      ... and {len(stems) - args.list_limit} more")

    if not chunk_rows:
        print("\nno chunks index -- nothing downstream to report")
        return 0

    # ---- chunks ----------------------------------------------------------
    total = len(chunk_rows)
    eligible = sum(1 for r in chunk_rows if r.get("include_in_esg_index") == "true")
    citable = sum(1 for r in chunk_rows if r.get("citation_ready") == "true")

    print(f"\nchunks produced     : {total}")
    print(f"  eligible to index : {eligible}  ({eligible / total:.0%})")
    print(f"  citation ready    : {citable}  ({citable / total:.0%})")

    print("\nby retrieval action")
    for action, n in Counter(r.get("rag_action", "?") for r in chunk_rows).most_common():
        print(f"  {action:<32} {bar(n, total)} {n:>6}")

    tiers = Counter(r.get("retrieval_tier", "?") for r in chunk_rows)
    if len(tiers) > 1:
        print("\nby retrieval tier")
        for tier, n in tiers.most_common():
            print(f"  {tier:<32} {bar(n, total)} {n:>6}")

    # ---- documents contributing nothing ----------------------------------
    per_doc: dict[str, list[dict]] = defaultdict(list)
    for row in chunk_rows:
        per_doc[row.get("pdf_stem") or row.get("ticker") or "?"].append(row)

    dead = sorted(
        stem
        for stem, rows in per_doc.items()
        if not any(r.get("include_in_esg_index") == "true" for r in rows)
    )
    print(f"\ndocuments in index  : {len(per_doc) - len(dead)} of {len(per_doc)}")
    if dead:
        print(f"contributing nothing: {len(dead)}")
        for stem in dead[: args.list_limit]:
            reason = per_doc[stem][0].get("doc_quality_status", "?")
            print(f"      {stem}  ({reason})")
        if len(dead) > args.list_limit:
            print(f"      ... and {len(dead) - args.list_limit} more")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
