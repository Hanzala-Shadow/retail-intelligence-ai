"""Freeze a completed Terra recovery review into a durable safety-label file.

The review directories under ``reports/esg_recovery_candidate/`` are working
artefacts: they carry rendered PNGs, per-batch task files and the full page
text, and a clean run is free to discard them. The verdicts inside them are
not reproducible -- they came from a human-directed visual review that cost
real effort -- so they are lifted out into one curated CSV under
``data/00_reference/`` and registered with ``prepare_clean_run`` as CURATED.

Every row carries the three hashes the label depends on: the source PDF, the
rendered image the reviewer actually looked at, and the exact parser text they
compared it against. A regression test that finds any of them stale must fail
rather than quietly score the gate against a page that has since changed.

Read-only apart from the label file it writes. No API call, no vision, no gold.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)

import config

DEFAULT_REVIEWS = [
    config.REPORTS_DIR / "esg_recovery_candidate" / "terra_review_v1",
]
DEFAULT_OUT = config.REFERENCE_DIR / "esg_recovery_safety_labels.csv"

FIELDS = [
    "item_id",
    "review_version",
    "review_stratum",
    "ticker",
    "pdf_file",
    "pdf_stem",
    "page",
    "source_pdf",
    "source_sha256",
    "image_path",
    "image_sha256",
    "parsed_text_file",
    "page_map_file",
    "current_text_sha256",
    "expected_verdict",
    "issue_codes",
    "reviewer_confidence",
    "evidence",
]

#: The label file records two states. ``needs_review`` never becomes a label:
#: an undecided page cannot say anything about whether the gate is right.
VERDICT_MAP = {
    "safe_for_embedding": "safe",
    "unsafe_for_embedding": "unsafe",
}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def rows_for(review_dir: Path) -> list[dict[str, str]]:
    queue = {row["item_id"]: row for row in read_jsonl(review_dir / "queue.jsonl")}
    verdicts = {row["item_id"]: row for row in read_jsonl(review_dir / "terra_review.jsonl")}
    missing = sorted(set(queue) - set(verdicts))
    if missing:
        raise SystemExit(f"{review_dir.name}: no verdict for {missing}")

    rows: list[dict[str, str]] = []
    for item_id, page in queue.items():
        verdict = verdicts[item_id]
        expected = VERDICT_MAP.get(verdict["verdict"])
        if expected is None:
            print(f"  skipping {item_id}: verdict is {verdict['verdict']!r}")
            continue
        # The queue keeps the text itself; re-hash it so a queue written by an
        # older version of the sampler cannot carry a hash of different text.
        text_hash = sha256_text(page["current_text"])
        if text_hash != page.get("current_text_sha256"):
            raise SystemExit(
                f"{item_id}: queue text does not match its own recorded hash"
            )
        rows.append(
            {
                "item_id": item_id,
                "review_version": page["version"],
                "review_stratum": page["review_stratum"],
                "ticker": page["ticker"],
                "pdf_file": page["pdf_file"],
                "pdf_stem": page["pdf_stem"],
                "page": str(page["page"]),
                "source_pdf": page["source_pdf"],
                "source_sha256": page["source_sha256"],
                "image_path": page["image_path"],
                "image_sha256": page["image_sha256"],
                "parsed_text_file": page["parsed_text_file"],
                "page_map_file": page["page_map_file"],
                "current_text_sha256": text_hash,
                "expected_verdict": expected,
                "issue_codes": ";".join(verdict.get("issue_codes") or []),
                "reviewer_confidence": verdict.get("confidence", ""),
                "evidence": " ".join((verdict.get("evidence") or "").split()),
            }
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-dir", type=Path, action="append", dest="review_dirs")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    review_dirs = args.review_dirs or DEFAULT_REVIEWS
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for review_dir in review_dirs:
        print(f"reading {review_dir}")
        for row in rows_for(review_dir.resolve()):
            if row["item_id"] in seen:
                raise SystemExit(f"duplicate item_id across reviews: {row['item_id']}")
            seen.add(row["item_id"])
            rows.append(row)

    rows.sort(key=lambda row: row["item_id"])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    safe = sum(1 for row in rows if row["expected_verdict"] == "safe")
    print(f"wrote {len(rows)} labels ({safe} safe, {len(rows) - safe} unsafe) to {args.out}")


if __name__ == "__main__":
    main()
