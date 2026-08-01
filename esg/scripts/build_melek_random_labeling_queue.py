"""Create a blind, random chunk queue for Melek's labeling work.

The output deliberately contains no source metadata or model labels.  The
three reviewer columns are left blank for Melek to complete.
"""

from __future__ import annotations

import csv
import random
import secrets

import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)

import config

INDEX = config.ESG_CHUNKS_INDEX_ENRICHED_CSV
OUT = config.REPO_ROOT / "outputs/melek_random_labeling_queue_240.tsv"
FIELDS = ["item_id", "chunk_text", "human_label", "confidence", "reviewer_notes"]
TARGET = 240


def blind_item_ids(count: int) -> list[str]:
    ids: list[str] = []
    used: set[str] = set()
    while len(ids) < count:
        item_id = f"MLK-{len(ids) + 1:03d}-{secrets.token_hex(4).upper()}"
        if item_id not in used:
            ids.append(item_id)
            used.add(item_id)
    return ids


def main() -> None:
    with INDEX.open(encoding="utf-8-sig", newline="") as fh:
        index_rows = list(csv.DictReader(fh))

    candidates: list[str] = []
    for row in index_rows:
        rel_path = (row.get("chunk_file") or "").strip()
        path = REPO_ROOT / rel_path
        if not rel_path or not path.is_file():
            continue
        chunk_text = path.read_text(encoding="utf-8")
        if chunk_text.strip():
            candidates.append(chunk_text)

    if len(candidates) < TARGET:
        raise SystemExit(f"Only {len(candidates)} readable chunks; need {TARGET}.")

    selected = random.SystemRandom().sample(candidates, TARGET)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=FIELDS, delimiter="\t", lineterminator="\n"
        )
        writer.writeheader()
        for item_id, chunk_text in zip(blind_item_ids(TARGET), selected, strict=True):
            writer.writerow({
                "item_id": item_id,
                "chunk_text": chunk_text,
                "human_label": "",
                "confidence": "",
                "reviewer_notes": "",
            })

    print(f"Created {OUT} with {TARGET} randomly selected chunks.")


if __name__ == "__main__":
    main()
