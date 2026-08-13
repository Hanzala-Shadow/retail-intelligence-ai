"""Check a returned embedding run against the corpus that produced it.

Vectors come back from an external GPU run as an opaque array. This proves
they describe this corpus: that row *i* of the array is the chunk the index
says it is, that the text encoded was the text we hold, and that every vector
satisfies the contract the retrieval layer assumes.

Row order alone is not trusted. Each row carries the SHA-256 of the text that
produced it, and that hash is compared against the live chunk index.

Optionally samples nearest neighbours as a smoke test. That is not an
evaluation -- it catches gross breakage, not ranking quality.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import numpy as np

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

TRUE_VALUES = {"1", "true", "yes", "y"}
NORM_TOLERANCE = 1e-3


class Failures:
    def __init__(self) -> None:
        self.items: list[str] = []

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        if ok:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}" + (f" -- {detail}" if detail else ""))
            self.items.append(label)
        return ok


def is_eligible(row: dict[str, str]) -> bool:
    return (row.get("include_in_esg_index") or "").strip().lower() in TRUE_VALUES


def load_chunk_index(path: Path) -> dict[str, dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return {row["chunk_id"]: row for row in csv.DictReader(handle)}


def load_embedding_index(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vectors", type=Path, required=True, help="Returned .npy.")
    parser.add_argument(
        "--index", type=Path, required=True, help="Returned index CSV (row/chunk_id/sha)."
    )
    parser.add_argument(
        "--chunk-index",
        type=Path,
        required=True,
        help="The live chunk index the run was exported from.",
    )
    parser.add_argument(
        "--run-manifest", type=Path, default=None, help="Optional run manifest JSON."
    )
    parser.add_argument(
        "--include-excluded",
        action="store_true",
        help="Expect non-retrievable rows to be embedded too.",
    )
    parser.add_argument(
        "--sample-neighbours",
        type=int,
        default=0,
        help="Print nearest neighbours for N random chunks as a smoke test.",
    )
    parser.add_argument("--seed", type=int, default=20260811)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    fail = Failures()

    for path in (args.vectors, args.index, args.chunk_index):
        if not path.is_file():
            print(f"ERROR: missing {path}", file=sys.stderr)
            return 2

    vectors = np.load(args.vectors)
    emb_index = load_embedding_index(args.index)
    chunks = load_chunk_index(args.chunk_index)
    expected_ids = [
        cid
        for cid, row in chunks.items()
        if args.include_excluded or is_eligible(row)
    ]

    print(f"vectors      {args.vectors}  shape {vectors.shape} dtype {vectors.dtype}")
    print(f"index        {args.index}  rows {len(emb_index)}")
    print(f"chunk index  {args.chunk_index}  rows {len(chunks)}")
    print()

    print("Array contract")
    fail.check(vectors.ndim == 2, "array is 2-D", f"ndim={vectors.ndim}")
    fail.check(
        vectors.dtype == np.float32, "dtype is float32", f"dtype={vectors.dtype}"
    )
    finite = bool(np.isfinite(vectors).all())
    fail.check(finite, "all values finite")
    if finite:
        norms = np.linalg.norm(vectors, axis=1)
        worst = float(np.max(np.abs(norms - 1.0)))
        fail.check(
            worst <= NORM_TOLERANCE,
            f"vectors are unit norm (max deviation {worst:.2e})",
            f"tolerance {NORM_TOLERANCE}",
        )
        zero = int((norms < 1e-6).sum())
        fail.check(zero == 0, "no zero-length vectors", f"{zero} found")

    print()
    print("Alignment with the corpus")
    fail.check(
        vectors.shape[0] == len(emb_index),
        "vector rows match index rows",
        f"{vectors.shape[0]} vs {len(emb_index)}",
    )

    index_ids = [row["chunk_id"] for row in emb_index]
    fail.check(
        len(set(index_ids)) == len(index_ids),
        "no duplicate chunk_id in the returned index",
    )
    fail.check(
        [int(row["row"]) for row in emb_index] == list(range(len(emb_index))),
        "row numbers are contiguous and in order",
    )

    missing = sorted(set(expected_ids) - set(index_ids))
    extra = sorted(set(index_ids) - set(expected_ids))
    fail.check(
        not missing,
        f"every expected chunk was embedded ({len(expected_ids)} expected)",
        f"{len(missing)} missing, first {missing[0]}" if missing else "",
    )
    fail.check(
        not extra,
        "no unexpected chunks in the run",
        f"{len(extra)} extra, first {extra[0]}" if extra else "",
    )

    # The decisive check: the hash carried back must match the hash of the text
    # we hold. This is what makes row identity trustworthy rather than assumed.
    drift = [
        row["chunk_id"]
        for row in emb_index
        if row["chunk_id"] in chunks
        and row["embedding_text_sha256"] != chunks[row["chunk_id"]]["embedding_text_sha256"]
    ]
    fail.check(
        not drift,
        "embedded text matches the corpus text, row for row",
        f"{len(drift)} rows drifted, first {drift[0]}" if drift else "",
    )

    if args.run_manifest and args.run_manifest.is_file():
        manifest = json.loads(args.run_manifest.read_text(encoding="utf-8"))
        print()
        print("Run manifest")
        print(f"  model     {manifest.get('model_repo')}")
        print(f"  revision  {manifest.get('model_revision')}")
        print(f"  encoding  {manifest.get('passage_encoding')}")
        print(f"  gpu       {manifest.get('gpu')}")
        fail.check(
            manifest.get("rows") == vectors.shape[0],
            "manifest row count matches the array",
        )
        fail.check(
            manifest.get("embedding_dim") == vectors.shape[1],
            "manifest dimension matches the array",
        )

    if args.sample_neighbours and not fail.items:
        print()
        print(f"Nearest-neighbour smoke test ({args.sample_neighbours} samples)")
        rng = np.random.default_rng(args.seed)
        picks = rng.choice(vectors.shape[0], size=args.sample_neighbours, replace=False)
        for i in picks:
            # Vectors are unit norm, so the dot product is cosine similarity.
            sims = vectors @ vectors[i]
            sims[i] = -np.inf
            top = int(np.argmax(sims))
            src = chunks.get(index_ids[i], {})
            dst = chunks.get(index_ids[top], {})
            print(
                f"  {sims[top]:.3f}  {src.get('canonical_ticker','?')} "
                f"{src.get('section_code','?')}  ->  "
                f"{dst.get('canonical_ticker','?')} {dst.get('section_code','?')}"
            )

    print()
    if fail.items:
        print(f"FAILED {len(fail.items)} check(s): {', '.join(fail.items)}")
        return 1
    print("All checks passed. Vectors are aligned with the corpus.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
